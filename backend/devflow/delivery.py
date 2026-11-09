"""Deterministic current-evidence delivery; no approval agent exists."""
import hashlib
import json
from sqlalchemy import select
from . import core, policy, specs, steering, tickets
from .models import Artifact, Project, ReviewRecord, TaskInput, Trigger


def snapshot(db, task):
    project = core.get(db, Project, task.project_id)
    evidence = list(db.scalars(select(Artifact).where(Artifact.task_id == task.id, Artifact.kind != "ci_verified")
                              .order_by(Artifact.created_at.desc(), Artifact.id).limit(32)))
    inputs = list(db.scalars(select(TaskInput).where(TaskInput.task_id == task.id).order_by(TaskInput.created_at)))
    return {"goal": task.goal, "acceptance": task.acceptance, "scope": task.scope,
            "head_sha": task.head_sha, "authorization": task.authorization,
            "authorization_version": task.authorization_version, "mandate": task.mandate,
            "policy_version": project.policy_version, "input_version": task.input_version,
            "specifications": [{"id": row.id, "version": row.version, "text": row.text,
                                "task_id": row.task_id} for row in specs.applicable(db, task)],
            "original_inputs": [{"id": row.id, "kind": row.kind, "payload": row.payload} for row in inputs],
            "artifacts": [{"id": e.id, "kind": e.kind, "sha": e.sha,
                           "verified": e.verified, "payload": e.payload} for e in evidence]}


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def valid_review(db, task):
    digest = fingerprint(snapshot(db, task))
    return db.scalar(select(ReviewRecord).where(ReviewRecord.task_id == task.id,
        ReviewRecord.fingerprint == digest, ReviewRecord.aligned.is_(True))
        .order_by(ReviewRecord.created_at.desc()).limit(1))


def eligible(db, task, *, require_wait=True):
    project = core.get(db, Project, task.project_id)
    if task.status in core.TERMINAL | {"paused", "failed", "waiting_human"}:
        return False
    if require_wait and (task.status != "waiting_external" or task.wait_reason != "supervision"):
        return False
    if not steering.can_publish(db, task, project) or not valid_review(db, task):
        return False
    if task.scope == "implement" and not db.scalar(select(Artifact.id).where(
            Artifact.task_id == task.id, Artifact.kind == "verification", Artifact.sha == task.head_sha,
            Artifact.verified.is_(True))):
        return False
    return True


def coordinate(db, task):
    if not eligible(db, task):
        return False
    if task.scope == "analysis":
        tickets.end(db, task, by="executor", outcome="completed", reason="analysis independently reviewed")
        core.notify(db, task, "completed", "分析结果已交付：" + task.title, str(task.input_version))
    else:
        review = valid_review(db, task)
        core.effect(db, task.project_id, "publish", f"publish:{task.id}:{task.head_sha}:{review.id}",
                    {"sha": task.head_sha, "review_id": review.id}, task.id)
    return True
