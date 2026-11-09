"""Immutable policy/specification bodies and separate per-task receipts."""
import time
from sqlalchemy import select, or_, func
from .models import PolicyRevision, SpecRevision, SpecReceipt, Task
from . import core, policy


def record_policy(db, project):
    policy.ensure_version(project)
    key = (project.id, project.policy_version)
    row = db.get(PolicyRevision, key)
    if row is None:
        row = PolicyRevision(project_id=project.id, version=project.policy_version,
                             body=policy.normalize(project.policy))
        db.add(row); db.flush()
    return row


def applicable(db, task):
    return list(db.scalars(select(SpecRevision).where(SpecRevision.project_id == task.project_id,
        or_(SpecRevision.task_id.is_(None), SpecRevision.task_id == task.id)).order_by(SpecRevision.version)))


def deliver(db, task, revision):
    row = db.get(SpecReceipt, (revision.id, task.id))
    if row is None:
        row = SpecReceipt(spec_id=revision.id, task_id=task.id)
        db.add(row); db.flush()
        core.input_task(db, task.id, {"kind": "spec_update", "text": revision.text,
            "version": revision.version, "spec_id": revision.id, "task_scoped": bool(revision.task_id)},
            wake=False, kind="steer")
    return row


def load_new(db, task):
    for revision in applicable(db, task):
        deliver(db, task, revision)


def update(db, project, text, version=None, task_ids=None):
    core.lock_project(db, project.id)
    latest = db.scalar(select(func.max(SpecRevision.version)).where(SpecRevision.project_id == project.id)) or 0
    version = int(version) if version is not None else latest + 1
    if version <= latest:
        raise core.Conflict("spec version must increase")
    targets = []
    if task_ids is not None:
        if not task_ids:
            raise core.Conflict("task_ids cannot be empty")
        for task_id in dict.fromkeys(task_ids):
            task = core.get(db, Task, task_id)
            if task.project_id != project.id:
                raise core.Conflict("task belongs to another project")
            targets.append(task)
    revisions = []
    if task_ids is None:
        row = SpecRevision(project_id=project.id, version=version, text=text)
        db.add(row); db.flush(); revisions.append(row)
        project.spec_version = version
        targets = list(db.scalars(select(Task).where(Task.project_id == project.id,
                                Task.status.not_in(list(core.TERMINAL)))))
    else:
        # Each immutable revision has one applicability target, with globally unique revision numbers.
        for offset, task in enumerate(targets):
            row = SpecRevision(project_id=project.id, task_id=task.id, version=version+offset, text=text)
            db.add(row); db.flush(); revisions.append(row)
    for task in targets:
        for revision in revisions:
            if revision.task_id is None or revision.task_id == task.id:
                deliver(db, task, revision)
    core.event(db, project.id, "project.spec_changed", {"version": version,
               "task_ids": task_ids, "delivered": len(targets)})
    return version, len(targets)


def read(db, task, payload):
    identifier = payload.get("spec_id")
    row = db.get(SpecReceipt, (identifier, task.id)) if identifier else None
    if row:
        row.read_at = time.time()


def applied(db, task):
    for revision in applicable(db, task):
        row = db.get(SpecReceipt, (revision.id, task.id))
        if row and row.read_at:
            row.applied_at = time.time()


def verify(db, task):
    for revision in applicable(db, task):
        row = db.get(SpecReceipt, (revision.id, task.id))
        if row and row.applied_at:
            row.verified_at = time.time()


def complete(db, task):
    return all((row := db.get(SpecReceipt, (revision.id, task.id))) is not None
               and row.applied_at and row.verified_at for revision in applicable(db, task))
