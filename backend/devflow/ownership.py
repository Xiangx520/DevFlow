"""Durable ownership independent of thread subscription and input transport."""
import time
from sqlalchemy import select
from .models import MessageOwnership, Thread, Trigger


def acquire(db, task, trigger_id):
    from . import core
    core.lock_project(db, task.project_id)
    trigger = db.get(Trigger, trigger_id)
    if trigger is None or trigger.project_id != task.project_id:
        raise core.Conflict("message belongs to another project")
    row = db.get(MessageOwnership, (trigger_id, task.id))
    if row is None:
        row = MessageOwnership(trigger_id=trigger_id, task_id=task.id)
        db.add(row)
        db.flush()
    return row.released_at is None


def release(db, task, trigger_id, reason):
    from . import core
    core.lock_project(db, task.project_id)
    trigger = db.get(Trigger, trigger_id)
    if trigger is None or trigger.project_id != task.project_id:
        raise core.Conflict("message belongs to another project")
    row = db.get(MessageOwnership, (trigger_id, task.id))
    if row is None or row.released_at is not None:
        return False
    row.released_at = time.time()
    row.release_reason = reason
    db.flush()
    owners = list(db.scalars(select(MessageOwnership).where(MessageOwnership.trigger_id == trigger_id)))
    if all(owner.released_at is not None for owner in owners):
        trigger = db.get(Trigger, trigger_id)
        thread = db.get(Thread, trigger.thread_keys[0])
        if thread and thread.project_id == task.project_id:
            disposition = dict(trigger.disposition or {})
            disposition.pop("decision", None)
            trigger.disposition = {**disposition, "released": True}
            thread.pending = list(dict.fromkeys(list(thread.pending or []) + [trigger_id]))
            thread.updated_at = time.time()
    return True


def process_read(db, task):
    """A structured ready report settles inputs actually read in this execution round."""
    for row in db.scalars(select(MessageOwnership).where(
            MessageOwnership.task_id == task.id, MessageOwnership.released_at.is_(None),
            MessageOwnership.read_at.is_not(None), MessageOwnership.processed_at.is_(None))):
        row.processed_at = time.time()
