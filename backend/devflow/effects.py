"""Leased, retryable side effects with reconciliation before publishing."""
import asyncio
import time
from sqlalchemy import select, or_
from . import core
from .adapters import ExternalError, GitHub, feishu, redact
from .config import settings
from .db import transaction
from .models import Artifact, Effect, Event, FeishuBinding, FeishuMessage, Project, Task, uid, view
from .feishu_replies import reply as feishu_reply

async def check_ci(github, repository, sha):
    # Fine-grained PATs support Actions and commit statuses, not the Checks API.
    runs = await github.request("GET", f"/repos/{repository}/actions/runs",
                                params={"head_sha": sha, "per_page": 100})
    statuses = await github.request("GET", f"/repos/{repository}/commits/{sha}/status")
    rows = runs.get("workflow_runs", [])
    # Never approve a truncated list or absence of configured CI.
    if runs.get("total_count", 0) > len(rows): return "pending"
    latest = {}
    for row in rows:
        if row.get("head_sha") != sha:
            continue
        key = (row.get("workflow_id", row.get("path", row["id"])), row.get("event"))
        rank = (row["id"], row.get("run_attempt", 1))
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, row)
    combined = [row.get("conclusion") if row.get("status") == "completed" else "pending"
                for _, row in latest.values()]
    if statuses.get("total_count", 0): combined.append(statuses["state"])
    if not combined: return "missing"
    if any(x in {"failure", "error", "timed_out", "action_required", "cancelled"} for x in combined): return "failure"
    if any(x not in {"success", "neutral", "skipped"} for x in combined): return "pending"
    return "success"

async def execute(effect_id, owner, github=None, driver=None):
    github = github or GitHub()
    with transaction() as db:
        e = core.get(db, Effect, effect_id)
        project = core.get(db, Project, e.project_id)
        task = db.get(Task, e.task_id) if e.task_id else None
        kind = e.kind; payload = e.payload
        if e.owner != owner or e.status in {"sent", "dropped"}: return
        if task and task.status in core.TERMINAL | {"paused", "failed", "waiting_human"} and kind != "notification":
            e.status = "dropped"; e.result = {"reason": "task not authorized to continue"}; return
    def write_guard():
        with transaction() as db:
            current_effect = core.get(db, Effect, effect_id)
            current = core.lock_task(db, current_effect.task_id) if current_effect.task_id else None
            current_project = core.get(db, Project, current_effect.project_id)
            if current_effect.owner != owner:
                raise core.Conflict("external write lease lost")
            if current:
                from . import policy, delivery
                if current.ended_by == "human" or (current.mandate and not policy.revalidate(current_project, current.mandate)):
                    raise core.Conflict("external write authorization revoked")
                if kind == "publish" and not delivery.eligible(db, current):
                    raise core.Conflict("publication evidence changed")
    if hasattr(github, "write_guard"):
        github.write_guard = write_guard
    if kind in {"verify", "publish"} and driver is None:
        from .execution import DockerExecution
        driver = DockerExecution()
    if kind == "verify":
        if task.status != "waiting_external" or task.wait_reason != "host verification":
            with transaction() as db: core.get(db, Effect, effect_id).status = "dropped"
            return
        verification_started = time.time()
        try:
            result = await asyncio.to_thread(driver.verify, task, project)
        finally:
            with transaction() as db:
                current = core.lock_task(db, task.id)
                if core.get(db, Effect, effect_id).owner == owner:
                    current.active_seconds += time.time() - verification_started
        with transaction() as db:
            e = core.get(db, Effect, effect_id); task = core.lock_task(db, e.task_id)
            if e.owner != owner: return
            if task.status != "waiting_external" or task.wait_reason != "host verification":
                e.status = "dropped"; return
            task.head_sha = result["sha"]
            db.add(Artifact(task_id=task.id, kind="verification", sha=result["sha"], verified=result["passed"],
                            payload=result))
            core.event(db, project.id, "verification.result", result, task.id)
            if task.active_seconds >= core.limits(project)["task_seconds"]:
                core.ask(db, task, "验证阶段达到累计预算上限", "先审阅现有结果")
            elif result["passed"]:
                task.wait_reason = "supervision"
                core.effect(db, project.id, "final_review", f"review:{task.id}:{result['sha']}", {}, task.id)
            else:
                failures = list(db.scalars(select(Artifact).where(Artifact.task_id == task.id,
                    Artifact.kind == "verification", Artifact.verified.is_(False))))
                if len(failures) >= 3:
                    core.ask(db, task, "本地验证连续失败三次，是否调整方案？", "先查看验证记录")
                else:
                    core.input_task(db, task.id, {"source": "host_verification", "result": result})
            e.status = "sent"; e.result = {"sha": result["sha"]}
    elif kind == "assessment":
        from .assessment import evaluate
        done = await evaluate(payload["eval_id"])
        with transaction() as db:
            e = core.get(db, Effect, effect_id)
            from .assessment import _thread_for
            active = _thread_for(db, payload["eval_id"]) is not None
            e.status = "sent" if done or not active else "pending"
            if not done and active:
                e.available_at = time.time() + 30
                e.tries = 0
    elif kind in {"final_review", "semantic_review"}:
        from .reviewer import review_task
        reviewed = await review_task(task.id, force=True)
        with transaction() as db:
            e = core.get(db, Effect, effect_id)
            e.status = "sent" if reviewed else "pending"
            if not reviewed:
                e.available_at = time.time() + 60
                e.tries = 0
    elif kind == "publish":
        # Fence before each external operation; never hold a database lock over network I/O.
        with transaction() as db:
            e = core.get(db, Effect, effect_id); task = core.lock_task(db, e.task_id)
            if e.owner != owner or task.status != "waiting_external" or task.head_sha != payload["sha"] or task.wait_reason != "supervision":
                e.status = "dropped"; return
            valid = db.scalar(select(Artifact.id).where(Artifact.task_id == task.id, Artifact.kind == "verification",
                Artifact.verified.is_(True), Artifact.sha == task.head_sha))
            if not valid: raise core.Conflict("current verification missing")
            from . import delivery
            if not delivery.eligible(db, task):
                e.status = "dropped"; e.result = {"reason": "current supervision or authorization missing"}; return
            from . import steering
            if not steering.can_publish(db, task, project):
                core.input_task(db, task.id, {"kind": "spec_update", "text": "A newer project spec is pending; re-read it before publishing.",
                                               "version": project.spec_version}, wake=True, kind="steer")
                e.status = "pending"; e.available_at = time.time() + 30; e.owner = None; e.lease_until = 0
                return
        write_guard()
        await asyncio.to_thread(driver.push, task, project)
        with transaction() as db:
            e = core.get(db, Effect, effect_id); task = core.lock_task(db, e.task_id)
            project = core.get(db, Project, task.project_id)
            from . import delivery
            if e.owner != owner or task.status != "waiting_external" or task.head_sha != payload["sha"] or not delivery.eligible(db, task):
                e.status = "dropped"
                e.result = {"branch_pushed": True, "reason":"task changed during publication"}
                core.event(db, project.id, "publication.interrupted", e.result, task.id)
                return
        body = f"## 目标\n{task.goal}\n\n## 验收\n" + "\n".join("- " + x for x in task.acceptance)
        body += f"\n\n本地验证已通过，提交：{task.head_sha}。CI 状态另行核验。"
        result = await github.pull_request(project, task, body)
        with transaction() as db:
            e = core.get(db, Effect, effect_id); task = core.lock_task(db, e.task_id)
            if e.owner != owner: return
            task.pr_number = result["number"]
            from . import tickets
            thread_key = f"{project.id}:github:pr:{task.pr_number}"
            tickets.subscribe(db, task, thread_key)
            tickets._follow(db, task, thread_key, own=True)
            core.event(db, project.id, "github.pr", {"number": task.pr_number, "sha": payload["sha"]}, task.id)
            if task.status == "waiting_external" and task.head_sha == payload["sha"] and task.wait_reason == "supervision":
                task.wait_reason = "CI pending"; task.waiting_since = time.time()
                core.effect(db, project.id, "check_ci", f"ci-check:{task.id}:{task.head_sha}:publish",
                            {"sha": task.head_sha}, task.id)
            else:
                core.notify(db, task, "publication_after_stop",
                    f"{task.title}：任务状态已变化，在途发布已返回 PR #{task.pr_number}，请审阅是否保留。",
                    str(task.pr_number))
            e.status = "sent"; e.result = {"number": task.pr_number}
    elif kind == "check_ci":
        result = await check_ci(github, project.repository, payload["sha"])
        with transaction() as db:
            e = core.get(db, Effect, effect_id); task = core.lock_task(db, e.task_id)
            if task.head_sha != payload["sha"] or task.status != "waiting_external":
                e.status = "dropped"; return
            if result == "success":
                verified = db.scalar(select(Artifact.id).where(Artifact.task_id == task.id,
                    Artifact.kind == "verification", Artifact.sha == task.head_sha, Artifact.verified.is_(True)))
                from . import delivery
                reviewed = delivery.valid_review(db, task)
                if not verified or not reviewed:
                    core.input_task(db, task.id, {"source":"ci","sha":task.head_sha,"conclusion":"success",
                        "instruction":"Current commit lacks host verification or independent review; inspect and submit for verification."})
                    e.status = "sent"
                    return
                from . import steering
                if not steering.can_publish(db, task, project):
                    core.input_task(db, task.id, {"kind": "spec_update", "text": "A newer project spec is pending; re-read it before delivery.",
                                                   "version": project.spec_version}, wake=True, kind="steer")
                    e.status = "sent"
                    return
                db.add(Artifact(task_id=task.id, kind="ci_verified", sha=task.head_sha, verified=True,
                                payload={"status": result}))
                core.transition(db, task, "ready_for_review", "current commit local tests and CI passed")
                core.notify(db, task, "review", f"待审阅：{task.title}\n提交：{task.head_sha}\n"
                    f"https://github.com/{project.repository}/pull/{task.pr_number}", task.head_sha)
                e.status = "sent"
            elif result == "failure":
                if task.ci_repairs >= core.limits(project)["ci_repairs"]:
                    core.ask(db, task, "CI 修复次数已达上限", "检查失败日志后决定")
                else:
                    task.ci_repairs += 1
                    core.input_task(db, task.id, {"source": "ci", "sha": task.head_sha,
                        "instruction": "Read current failed CI job logs and repair."})
                e.status = "sent"
            else:
                task.wait_reason = "CI not configured" if result == "missing" else "CI pending"
                e.status = "pending"; e.available_at = time.time() + 60
                e.tries = 0; e.owner = None; e.lease_until = 0
    elif kind == "feishu_reply":
        with transaction() as db:
            e = core.get(db, Effect, effect_id)
            if e.owner != owner: return
            binding = db.get(FeishuBinding, (payload["app_id"], payload["chat_id"]))
            source = db.get(FeishuMessage, payload["message_id"])
            if (not binding or binding.project_id != project.id or payload["app_id"] != settings().feishu_app_id
                    or not source or source.chat_id != binding.chat_id or source.sender_id not in binding.allowed_open_ids):
                e.status = "dropped"; e.result = {"reason":"reply binding no longer authorized"}; return
            if e.tries > 1 and time.time() - e.created_at >= 3500:
                e.status = "failed"; e.result = {"reason":"delivery uncertain; deduplication window expired"}
                core.event(db, project.id, "effect.failed", {"effect_id":e.id,"kind":e.kind})
                return
        result = await feishu_reply(payload["message_id"], payload["text"], effect_id)
        with transaction() as db:
            e = core.get(db, Effect, effect_id)
            if e.owner != owner: return
            e.status = "sent"; e.result = result
            core.event(db, project.id, "feishu.reply_sent", {"effect_id":e.id, **result})
    elif kind == "notification":
        with transaction() as db: e = core.get(db, Effect, effect_id); receipts = dict(e.result)
        if payload.get("github_number") and "github" not in receipts:
            result = await github.comment(project.repository, payload["github_number"], payload["text"], effect_id)
            receipts["github"] = {"id": result["id"]}
            with transaction() as db: core.get(db, Effect, effect_id).result = receipts
        if payload.get("feishu") and "feishu" not in receipts:
            result = await feishu(payload["text"] + f"\n任务：{settings().public_url}/api/v1/tasks/{task.id}")
            receipts["feishu"] = result
        with transaction() as db:
            e = core.get(db, Effect, effect_id); e.status = "sent"; e.result = receipts
    else:
        raise core.Conflict("unknown effect")

async def process_one(github=None, driver=None, include=None, exclude=None):
    owner = uid(); now = time.time()
    with transaction() as db:
        query = select(Effect).where(
            or_(Effect.status == "pending", (Effect.status == "running") & (Effect.lease_until < now)),
            Effect.available_at <= now)
        if include: query = query.where(Effect.kind.in_(include))
        if exclude: query = query.where(Effect.kind.not_in(exclude))
        e = db.scalar(query.order_by(Effect.created_at).with_for_update(skip_locked=True).limit(1))
        if not e: return False
        project = db.get(Project, e.project_id)
        if project is not None and project.paused:
            e.available_at = now + 5
            return True
        e.status = "running"; e.owner = owner; e.lease_until = now + (60 if e.kind == "feishu_reply" else 2000); e.tries += 1
        identifier = e.id
    try:
        await execute(identifier, owner, github, driver)
    except Exception as exc:
        with transaction() as db:
            e = core.get(db, Effect, identifier)
            if e.owner != owner: return True
            e.status = "failed" if e.tries >= 5 else "pending"
            e.available_at = time.time() + max(min(2 ** e.tries, 60), getattr(exc, "retry_after", 0))
            e.result = {**e.result, "error": redact(exc), "delivery_may_have_succeeded": True}
            if e.status == "failed":
                core.event(db, e.project_id, "effect.failed", {"effect_id": e.id, "kind": e.kind}, e.task_id)
                if e.task_id and e.kind != "notification":
                    core.ask(db, core.get(db, Task, e.task_id), "外部操作连续失败", "检查配置和执行记录后重试")
    return True
