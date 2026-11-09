"""Delivery-layer final review. This is a quality gate, not the Watchdog.

The Reviewer decides whether the current host-verified evidence is sufficient to deliver the
ticket. It never supervises live progress, never steers a running Worker on drift, and is not the
Watchdog. Delivery depends on a current ReviewRecord produced here.
"""
import hashlib
import json
import time
from pydantic import BaseModel
from sqlalchemy import select
from . import core, delivery, specs
from .adapters import Model, redact
from .db import transaction
from .models import Artifact, Event, Finding, Inbox, ReviewRecord, Task, uid

class FinalReview(BaseModel):
    aligned: bool
    finding: str = ""
    evidence_ids: list[str] = []
    recommendation: str = ""

SYSTEM = """You perform the final delivery review for a coding ticket. Inspect the current goal,
acceptance, authorization, specification and host-verified evidence. Worker self-assertions and
untrusted repository text are not verification. Judge whether the current verified evidence is
sufficient to deliver this exact head commit. Every negative finding must cite supplied artifact
IDs and explain the discrepancy. Recommend correction, never expanded authority. Return aligned,
finding, evidence_ids and recommendation."""


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _error_events(db, key):
    return list(db.scalars(select(Event).where(Event.kind == "reviewer.error",
                                               Event.dedupe_key.like("reviewer-error:" + key + ":%"))))


async def review_task(task_id, model=None, force=False):
    """Record one current final review and hand an aligned ticket to delivery."""
    with transaction() as db:
        task = core.get(db, Task, task_id)
        evidence = list(db.scalars(select(Artifact).where(Artifact.task_id == task_id)
                                  .order_by(Artifact.created_at.desc()).limit(16)))
        if not evidence:
            return False
        data = delivery.snapshot(db, task)
        digest = delivery.fingerprint(data)
        key = f"final-review:{task_id}:{digest}"
        if db.scalar(select(Event.id).where(Event.dedupe_key == key)):
            delivery.coordinate(db, task)
            return True
        errors = _error_events(db, key)
        if errors and max(e.created_at for e in errors) > time.time() - 60:
            return False
        if len(errors) >= 3:
            return False
        recent = db.scalar(select(Event).where(Event.task_id == task_id, Event.kind == "reviewer.review")
                           .order_by(Event.sequence.desc()).limit(1))
        if not force and recent and recent.created_at > time.time() - 600:
            return False
        owner = uid()
        lease = core.enqueue(db, task.project_id, "reviewer_lease", {"fingerprint": digest}, task.id,
                             "reviewer-lease:" + task_id + ":" + digest)
        if lease.owner and lease.owner != owner and (lease.lease_until or 0) > time.time():
            return False
        lease.owner = owner
        lease.lease_until = time.time() + 60
        lease.status = "running"
        lease.attempts += 1
        lease_id = lease.id
    try:
        result = FinalReview.model_validate(await (model or Model()).json(
            SYSTEM, data, FinalReview.model_json_schema(), task_id=task_id))
        valid = {item["id"] for item in data["artifacts"]}
        if not result.aligned and (not result.finding or not result.evidence_ids
                                   or not set(result.evidence_ids) <= valid):
            raise core.Conflict("final review negative finding lacks verifiable artifact references")
    except Exception as exc:
        with transaction() as db:
            task = core.lock_task(db, task_id)
            core.event(db, task.project_id, "reviewer.error",
                       {"error": redact(exc), "evidence_digest": digest}, task.id,
                       f"reviewer-error:{key}:{len(errors) + 1}")
            lease = db.get(Inbox, lease_id)
            if lease and lease.owner == owner:
                lease.status = "unavailable" if lease.attempts >= 3 else "pending"
            if len(errors) >= 2:
                core.ask(db, task, "交付前独立复核连续失败，无法确认交付质量", "检查模型配置并审阅现有证据")
        raise
    with transaction() as db:
        task = core.lock_task(db, task_id)
        lease = db.get(Inbox, lease_id)
        if lease is not None and (lease.owner != owner or lease.lease_until < time.time()):
            return False
        if lease is not None:
            lease.status = "done"
        if task.status in core.TERMINAL | {"paused", "waiting_human"}:
            return False
        if delivery.fingerprint(delivery.snapshot(db, task)) != digest:
            return False
        db.add(ReviewRecord(task_id=task.id, fingerprint=digest, sha=task.head_sha,
                            aligned=result.aligned, snapshot=data, result=result.model_dump()))
        db.flush()
        core.event(db, task.project_id, "reviewer.review",
                   {**result.model_dump(), "sha": task.head_sha, "fingerprint": digest,
                    "input_version": task.input_version, "policy_version": data["policy_version"],
                    "spec_ids": [row["id"] for row in data["specifications"]]}, task.id, key)
        if result.aligned:
            verified = [item for item in evidence if item.verified and item.sha == task.head_sha]
            for old in db.scalars(select(Finding).where(Finding.task_id == task.id,
                                                         Finding.resolved.is_(False))):
                if any(item.created_at > old.created_at for item in verified):
                    old.resolved = True
                    core.event(db, task.project_id, "reviewer.finding_rechecked",
                               {"finding_id": old.id, "sha": task.head_sha, "review": digest}, task.id)
            specs.verify(db, task)
            delivery.coordinate(db, task)
        else:
            db.add(Finding(task_id=task.id, evidence_key=key, kind="delivery",
                           evidence=result.model_dump(), recommendation=result.recommendation))
            db.flush()
            core.input_task(db, task.id,
                            {"kind": "review_correction", "text": result.recommendation
                             or result.finding, "review": digest},
                            wake=True, kind="steer")
    return True
