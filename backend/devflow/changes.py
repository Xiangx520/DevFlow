"""Deterministic domain reactions to Source events.

Sources only produce Triggers and enqueue `source_event` facts. This module owns the domain side:
CI repair decisions, PR head changes, PR closure, reopen notices and answers to an Attention.
It never talks to GitHub or reads Source payloads beyond the facts the Source recorded.
"""
import time
from sqlalchemy import select
from . import core, policy, steering, tickets
from .adapters import redact
from .db import transaction
from .models import Artifact, Attention, Event, Inbox, Project, Task


def enqueue(db, project_id, kind, payload, task_id=None, key=None):
    return core.enqueue(db, project_id, "source_event", {"kind": kind, **payload}, task_id, key)


def _ci(db, project, task, payload):
    ci = payload.get("ci") or {}
    kind = payload.get("event_kind") or "workflow_run"
    sha = ci.get("head_sha")
    conclusion = ci.get("conclusion")
    ci_key = payload.get("ci_key")
    if not ci_key or db.scalar(select(Event.id).where(Event.dedupe_key == ci_key)):
        return
    db.add(Artifact(task_id=task.id, kind="ci", sha=sha, verified=False,
                    payload={"id": ci.get("id"), "conclusion": conclusion, "url": ci.get("html_url")}))
    core.event(db, project.id, "github.ci", {"sha": sha, "conclusion": conclusion}, task.id, key=ci_key)
    if task.status == "paused":
        core.input_task(db, task.id, {"source": "ci", "sha": sha, "conclusion": conclusion}, wake=False)
    elif conclusion in {"failure", "timed_out", "action_required", "cancelled"}:
        if conclusion == "action_required":
            core.ask(db, task, "CI 需要外部审批", "先在 GitHub 批准工作流运行")
        else:
            if task.status in {"queued", "running"}:
                core.input_task(db, task.id, {"source": "ci_context", "sha": sha,
                                              "conclusion": conclusion}, wake=False)
            elif task.ci_repairs >= core.limits(project)["ci_repairs"]:
                core.ask(db, task, "CI 自动修复次数已达上限", "查看失败日志后决定下一步")
            elif (task.status not in core.TERMINAL
                  and task.status not in {"waiting_human", "failed"}
                  and task.report_state not in {"blocked", "failed"}):
                task.ci_repairs += 1
                steering.steer(db, task,
                               "Read the current failed CI job logs, diagnose and fix within the original mandate.",
                               kind="correction", source="watchdog")
                if task.report_state not in {"blocked", "failed"}:
                    core.transition(db, task, "queued", "current CI repair")
    elif conclusion in {"success", "neutral", "skipped"} and task.status not in {"waiting_human", "running"}:
        core.effect(db, project.id, "check_ci", f"ci-check:{task.id}:{sha}:{ci.get('id')}",
                    {"sha": sha}, task.id)


def _answer(db, project, payload):
    task = db.get(Task, payload.get("task_id"))
    body = (payload.get("body") or "").strip()
    sender = payload.get("sender") or ""
    if task is None or task.status in core.TERMINAL or not body:
        return
    if not policy.allowed_for(policy.normalize(project.policy), "github", sender):
        return
    unresolved = list(db.scalars(select(Attention).where(Attention.task_id == task.id,
                                                         Attention.answer.is_(None))))
    if len(unresolved) == 1:
        core.answer(db, task.id, unresolved[0].id, body)


def apply(db, job):
    project = core.get(db, Project, job.project_id)
    kind = (job.payload or {}).get("kind")
    if kind == "ci_completed":
        task = db.get(Task, job.payload.get("task_id"))
        if task is not None:
            _ci(db, project, task, job.payload)
    elif kind == "pr_head_changed":
        task = db.get(Task, job.payload.get("task_id"))
        sha = job.payload.get("sha")
        if task is not None and sha and task.head_sha != sha and task.status not in core.TERMINAL:
            task.head_sha = sha
            core.revoke(db, task, "PR head changed")
    elif kind == "pr_closed":
        tickets.thread_closed(db, project, job.payload["thread_key"], job.payload["outcome"])
    elif kind == "pr_reopened":
        core.event(db, project.id, "task.reopen_requires_resume",
                   {"pr_number": job.payload.get("pr_number")}, None,
                   "source-reopen:" + job.id)
    elif kind == "attention_answer":
        _answer(db, project, job.payload)
    else:
        raise core.Conflict("unknown source event")


def run_pending(db, limit=100):
    """Apply pending source events in one transaction (tests and deterministic replay)."""
    jobs = list(db.scalars(select(Inbox).where(Inbox.kind == "source_event",
                                                Inbox.status == "pending",
                                                Inbox.available_at <= time.time())
                           .order_by(Inbox.created_at).limit(limit)))
    for job in jobs:
        try:
            apply(db, job)
            job.status = "done"
        except Exception:
            job.attempts += 1
            job.status = "unavailable" if job.attempts >= 3 else "pending"
            job.available_at = time.time() + 2 ** job.attempts
            raise
    return len(jobs)


async def process_one():
    """Claim and apply one source event; failures stay on the Inbox for retry."""
    with transaction() as db:
        job = db.scalar(select(Inbox).where(Inbox.kind == "source_event",
                                            Inbox.status == "pending",
                                            Inbox.available_at <= time.time())
                        .order_by(Inbox.created_at).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return False
        project = db.get(Project, job.project_id)
        if project is not None and project.paused:
            job.status = "pending"
            job.available_at = time.time() + 5
            return True
        job.status = "running"
        job.attempts += 1
        try:
            apply(db, job)
            job.status = "done"
        except Exception as exc:
            job.status = "unavailable" if job.attempts >= 3 else "pending"
            job.available_at = time.time() + 2 ** job.attempts
            core.event(db, job.project_id, "source.effect_error",
                       {"job_id": job.id, "error": redact(exc)[:500]}, job.task_id,
                       "source-error:" + job.id + ":" + str(job.attempts))
        return True
