"""Bounded project health summary for the API and the Watchdog."""
from sqlalchemy import func, select
from . import core
from .models import Attention, Decision, Effect, Finding, Project, Task, Thread


def summary(db, project_id):
    project = core.get(db, Project, project_id)

    def count(model, *criteria):
        return db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0

    threads = list(db.scalars(select(Thread).where(Thread.project_id == project_id)))
    unresolved_attentions = db.scalar(
        select(func.count()).select_from(Attention).join(Task, Attention.task_id == Task.id)
        .where(Task.project_id == project_id, Attention.answer.is_(None))) or 0
    unresolved_findings = db.scalar(
        select(func.count()).select_from(Finding).join(Task, Finding.task_id == Task.id)
        .where(Task.project_id == project_id, Finding.resolved.is_(False))) or 0
    return {
        "project_id": project.id,
        "paused": bool(project.paused),
        "policy_version": project.policy_version,
        "spec_version": project.spec_version,
        "source_last_poll": project.last_poll,
        "open_tickets": count(Task, Task.project_id == project_id,
                              Task.status.not_in(core.TERMINAL)),
        "running_tickets": count(Task, Task.project_id == project_id, Task.status == "running"),
        "waiting_human": count(Task, Task.project_id == project_id, Task.status == "waiting_human"),
        "running_evals": sum(1 for thread in threads if (thread.evaluating or {}).get("id")),
        "pending_triggers": sum(len(thread.pending or []) for thread in threads),
        "pending_effects": count(Effect, Effect.project_id == project_id,
                                 Effect.status.in_(["pending", "running"])),
        "failed_effects": count(Effect, Effect.project_id == project_id, Effect.status == "failed"),
        "unresolved_attentions": unresolved_attentions,
        "unresolved_findings": unresolved_findings,
        "unavailable_decisions": count(Decision, Decision.project_id == project_id,
                                       Decision.kind == "unavailable"),
    }
