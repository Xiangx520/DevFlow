"""Deterministic scheduler, intake, effect and execution workers."""
import asyncio
import json
import logging
import time
from sqlalchemy import case, select, text, exists
from . import core
from .adapters import redact
from .config import settings
from .db import transaction
from .execution import DockerExecution
from .models import Artifact, Attempt, ExecutionSession, Project, Task, TaskInput, MessageOwnership, uid
from . import changes, effects, github_events, router, steering, triggers, feishu_receiver

log = logging.getLogger("devflow.worker")

async def run_execution(attempt_id, capability, driver=None):
    driver = driver or DockerExecution()
    container_id = None; process = None
    async def monitor():
        while True:
            await asyncio.sleep(3)
            with transaction() as db:
                task = core.heartbeat(db, attempt_id)
                attempt = core.get(db, Attempt, attempt_id); project = core.get(db, Project, task.project_id)
                limit = core.limits(project)
                if task.active_seconds >= limit["task_seconds"] or task.tokens >= limit["tokens"] or time.time() - attempt.created_at > limit["turn_seconds"]:
                    core.ask(db, task, "执行已达到预算上限", "先审阅已有进展")
                    return
                if process and process.returncode is None:
                    pending = list(db.scalars(select(TaskInput).where(TaskInput.task_id == task.id,
                        TaskInput.consumed_at.is_(None),
                        ~exists(select(MessageOwnership.task_id).where(
                            MessageOwnership.task_id == TaskInput.task_id,
                            MessageOwnership.trigger_id == TaskInput.trigger_id,
                            MessageOwnership.released_at.is_not(None))),
                        (TaskInput.consumed_by.is_(None) | (TaskInput.consumed_by != attempt_id))).order_by(TaskInput.created_at)))
                    for entry in pending:
                        msg = {"v":1,"id":entry.id,"type":"steer","kind":entry.kind,
                               "text":json.dumps(entry.payload,ensure_ascii=False)}
                        process.stdin.write((json.dumps(msg)+"\n").encode())
                        await process.stdin.drain()
                        # Transport acceptance is recorded on ack; not on write.
                        entry.consumed_by = attempt_id
    async def drain_stderr():
        while process:
            line = await process.stderr.readline()
            if not line: return
            log.warning("runner %s: %s", attempt_id, redact(line.decode(errors="replace"))[:1000])
    monitor_task = asyncio.create_task(monitor())
    stderr_task = None
    try:
        with transaction() as db:
            attempt, task = core.active_attempt(db, attempt_id)
            project = core.get(db, Project, task.project_id)
            session = core.get(db, ExecutionSession, task.session_id)
            pending = list(db.scalars(select(TaskInput).where(TaskInput.task_id == task.id, TaskInput.consumed_at.is_(None),
                        ~exists(select(MessageOwnership.task_id).where(
                            MessageOwnership.task_id == TaskInput.task_id,
                            MessageOwnership.trigger_id == TaskInput.trigger_id,
                            MessageOwnership.released_at.is_not(None))))))
            mandate = {"task_id":task.id,"goal":task.goal,"acceptance":task.acceptance,"scope":task.scope,
                       "authorization":task.authorization,"authorization_version":task.authorization_version,
                       "mandate":task.mandate,"policy_version":project.policy_version,
                       "spec_version":task.spec_version,
                       "branch":task.branch,"head_sha":task.head_sha,"pr_number":task.pr_number,
                       "handoff":session.handoff,
                       "inputs":[{"id":x.id,"kind":x.kind,"payload":x.payload} for x in pending]}
            input_ids = [x.id for x in pending]; session_id = session.id; task_id = task.id
        # Kill orphaned containers from expired attempts before reusing this task's workspace.
        for old in await asyncio.to_thread(driver.client.containers.list, all=True, filters={"label":"devflow.task="+task_id}):
            await asyncio.to_thread(driver.stop, old.id)
        prepared = await asyncio.to_thread(driver.prepare, task, project)
        with transaction() as db:
            _, current = core.active_attempt(db, attempt_id)
            if prepared and not current.base_sha: current.base_sha = prepared["base_sha"]
        container = await asyncio.to_thread(driver.start_container, task_id)
        container_id = container.id
        with transaction() as db:
            a, _ = core.active_attempt(db, attempt_id); a.container_id = container_id
        process = await driver.attach(container_id)
        stderr_task = asyncio.create_task(drain_stderr())
        command_id = uid()
        msg = {"v":1,"id":command_id,"type":"resume","task_id":task_id,"attempt_id":attempt_id,
               "session_id":session_id,"capability":capability,"api_url":settings().internal_url,
               "model":settings().llm_model,"mandate":mandate}
        process.stdin.write((json.dumps(msg)+"\n").encode()); await process.stdin.drain()
        while True:
            reading = asyncio.create_task(process.stdout.readline())
            done, _ = await asyncio.wait({reading,monitor_task},return_when=asyncio.FIRST_COMPLETED)
            if monitor_task in done:
                reading.cancel()
                if monitor_task.exception(): raise monitor_task.exception()
                break
            line = reading.result()
            if not line: raise RuntimeError("Pi process exited unexpectedly")
            payload = json.loads(line)
            if payload.get("v") != 1 or payload.get("attempt_id") != attempt_id:
                raise RuntimeError("invalid runner event envelope")
            with transaction() as db:
                a, t = core.active_attempt(db, attempt_id)
                if payload["type"] == "ack":
                    identifier = payload.get("command_id")
                    ids = input_ids if identifier == command_id else [identifier]
                    for value in ids:
                        entry = db.get(TaskInput, value)
                        if entry and entry.task_id == task_id:
                            entry.consumed_by = attempt_id
                elif payload["type"] == "inputs_consumed":
                    now = time.time()
                    for value in payload.get("input_ids", []):
                        entry = db.get(TaskInput, value)
                        if entry and entry.task_id == task_id:
                            entry.consumed_at = now; entry.read_at = now; entry.consumed_by = attempt_id
                            from . import control
                            control.acknowledge(db, entry.id)
                            from . import specs
                            specs.read(db, t, entry.payload or {})
                            if entry.trigger_id:
                                owner = db.get(MessageOwnership, (entry.trigger_id, t.id))
                                if owner is not None and owner.released_at is None:
                                    owner.read_at = now
                            if entry.kind == "steer" and (entry.payload or {}).get("kind") in {"spec_update"}:
                                steering.acknowledge(db, t, (entry.payload or {}).get("version"))
                elif payload["type"] == "report":
                    core.report(db, attempt_id, payload["data"], payload["id"])
                elif payload["type"] == "error":
                    raise RuntimeError("Pi error: " + str(payload.get("error", "")))
                elif payload["type"] == "settled":
                    break
            with transaction() as db:
                if core.get(db, Task, task_id).status != "running": break
    except core.Conflict:
        pass  # A user command, handoff or watchdog revoked this execution.
    except Exception as exc:
        with transaction() as db:
            a = core.get(db, Attempt, attempt_id); t = core.lock_task(db, a.task_id)
            if a.status == "running" and t.lease_owner == a.id:
                a.reason = redact(exc)
                previous = list(db.scalars(select(Attempt).where(Attempt.task_id == t.id)))
                core.revoke(db, t, "execution failure")
                if len([x for x in previous if x.reason]) >= 3:
                    core.ask(db, t, "执行环境连续失败", "检查容器及模型配置后重试", redact(exc))
                else:
                    core.transition(db, t, "queued", "retry interrupted executor")
    finally:
        monitor_task.cancel()
        if stderr_task: stderr_task.cancel()
        if container_id:
            try: await asyncio.to_thread(driver.stop, container_id)
            except Exception: log.exception("container cleanup failed")
        if process and process.returncode is None:
            process.kill(); await process.wait()

async def schedule_one():
    with transaction() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(734327001)"))
        running = list(db.scalars(select(Task.id).where(Task.status == "running", Task.lease_until > time.time())))
        # A person's follow-up may use one slot beyond the normal concurrency limit.
        if len(running) >= settings().max_concurrency + 1: return None
        human_waiting = exists(select(MessageOwnership.task_id).where(
            MessageOwnership.task_id == Task.id,
            MessageOwnership.released_at.is_(None),
            MessageOwnership.read_at.is_(None)))
        paused = list(db.scalars(select(Project.id).where(Project.paused.is_(True))))
        query = select(Task).where(Task.status == "queued")
        if paused:
            query = query.where(Task.project_id.not_in(paused))
        if len(running) >= settings().max_concurrency:
            query = query.where(human_waiting)
        priority = case((human_waiting, 0), else_=1)
        for task in db.scalars(query.order_by(priority, Task.priority.desc(), Task.created_at)
                               .with_for_update(skip_locked=True)):
            claimed = core.claim_task(db, task.id, uid())
            if claimed: return claimed[0].id, claimed[1]
    return None

async def repeat(name, action, delay):
    while True:
        try: await action()
        except Exception as exc: log.error("%s: %s",name,redact(exc))
        await asyncio.sleep(delay)

async def evaluate_evals():
    with transaction() as db:
        for project in db.scalars(select(Project)):
            core.lock_project(db, project.id)
            triggers.evaluate_due(db, project)


async def main():
    active = set()
    async def launch():
        result = await schedule_one()
        if result:
            job = asyncio.create_task(run_execution(*result))
            active.add(job); job.add_done_callback(active.discard)
    await asyncio.gather(
        repeat("intake",github_events.process_one,settings().poll_seconds),
        repeat("source-effects",changes.process_one,settings().poll_seconds),
        repeat("evals",evaluate_evals,settings().poll_seconds),
        repeat("router",router.route_one,settings().poll_seconds),
        repeat("effects-1",lambda: effects.process_one(exclude={"notification", "feishu_reply"}),settings().poll_seconds),
        repeat("effects-2",lambda: effects.process_one(exclude={"notification", "feishu_reply"}),settings().poll_seconds),
        repeat("notifications",lambda: effects.process_one(include={"notification", "feishu_reply"}),settings().poll_seconds),
        repeat("scheduler",launch,settings().poll_seconds),
        repeat("feishu-history",feishu_receiver.reconcile,settings().webhook_poll_seconds),
        repeat("reconcile",github_events.reconcile,settings().webhook_poll_seconds))
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
