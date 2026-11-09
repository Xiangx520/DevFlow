"""The control plane: steer a live session directly, broadcast a spec, and deliver policy changes.

A person, the supervisor or the router can write into a running Attempt through a typed Input. The
worker turns an unconsumed Input into a `steer` message on the Pi session's stdin. This is the
watchdog's hand: it does not need a management model round-trip to correct or constrain work.
"""
import time
from sqlalchemy import select
from . import core, policy
from .models import Project, Task, Thread, Trigger

STEER_KINDS = {"correction", "constraint", "spec_update", "note", "stop", "status"}
TERMINAL = {"completed", "cancelled"}


def steer(db, task, text, *, kind="note", source="owner", version=None, require_ack=False):
    if kind not in STEER_KINDS:
        raise core.Conflict("unknown steer kind")
    if task.status in TERMINAL:
        raise core.Conflict("cannot steer a terminal ticket")
    payload = {"kind": kind, "text": text, "source": source}
    if version is not None:
        payload["version"] = int(version)
    if require_ack:
        payload["require_ack"] = True
    from . import control
    from .models import TaskInput
    record = control.submit(db, task, kind, payload, source)
    return db.get(TaskInput, record.input_id)



def acknowledge(db, task, version):
    """The session took a spec Input into a step; a later ready report may publish under it."""
    if version is None:
        return
    version = int(version)
    if version > (task.spec_version or 0):
        task.spec_version = version
        core.event(db, task.project_id, "ticket.spec_ack",
                   {"task_id": task.id, "version": version}, task.id,
                   "spec-ack:" + task.id + ":" + str(version))


def spec_update(db, project, text, version=None, task_ids=None):
    """Bump the project spec and send it to live sessions. Returns (version, delivered count)."""
    from . import specs
    return specs.update(db, project, text, version, task_ids)



def policy_changed(db, project, old_policy):
    """Deliver a policy change to affected live tickets and requeue in-flight Evals."""
    new_version = policy.version(project.policy)
    old_version = project.policy_version
    project.policy_version = new_version
    from . import specs
    specs.record_policy(db, project)
    delivered = 0
    for task in db.scalars(select(Task).where(Task.project_id == project.id,
                                              Task.status.not_in(list(TERMINAL)))):
        try:
            effect = policy.policy_effect(project, task, old_policy)
        except Exception:
            effect = None
        if not effect:
            continue
        payload = {"kind": "policy-changed", "from": old_version, "to": new_version,
                   "effect": effect}
        core.input_task(db, task.id, payload, wake=True, kind="policy-changed")
        core.event(db, project.id, "policy.effect", {"task_id": task.id, **payload}, task.id,
                   "policy-effect:" + task.id + ":" + new_version)
        delivered += 1
    requeued = 0
    for thread in db.scalars(select(Thread).where(Thread.project_id == project.id)):
        evaluating = thread.evaluating or {}
        if not evaluating.get("id"):
            continue
        ids = evaluating.get("trigger_ids") or []
        thread.pending = list(dict.fromkeys(ids + list(thread.pending or [])))
        thread.evaluating = {}
        thread.expired = 0
        for trigger_id in ids:
            trigger = db.get(Trigger, trigger_id)
            if trigger:
                trigger.disposition = {**(trigger.disposition or {}), "policy_changed": True}
        requeued += 1
    core.event(db, project.id, "policy.changed",
               {"from": old_version, "to": new_version, "tickets": delivered,
                "evals": requeued}, None, "policy:" + project.id + ":" + new_version)
    return new_version, delivered, requeued


def can_publish(db, task, project):
    from . import specs
    if not specs.complete(db, task):
        return False
    if task.ended_by == "human":
        return False
    if task.mandate and not policy.revalidate(project, task.mandate):
        return False
    return True
