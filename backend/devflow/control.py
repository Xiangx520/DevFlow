"""Watchdog control plane. Persist intent before any capability/state mutation."""
import time
from sqlalchemy import select
from . import core
from .models import Control, TaskInput, uid

IMMEDIATE = {"stop", "pause", "resume", "retry", "priority", "replace_session", "authorize"}
INPUTS = {"correction", "constraint", "spec_update", "note", "status"}


def submit(db, task, kind, payload=None, source="owner"):
    if kind not in IMMEDIATE | INPUTS:
        raise core.Conflict("unknown control")
    if source not in {"owner", "watchdog", "router"}:
        raise core.Conflict("unknown control authority")
    if source == "watchdog" and kind not in INPUTS | {"pause", "stop", "replace_session"}:
        raise core.Conflict("watchdog action outside limited authority")
    if source == "router" and kind not in {"stop", "pause", "status", "correction", "constraint", "replace_session"}:
        raise core.Conflict("router cannot change authorization")
    task = core.lock_task(db, task.id)
    row = Control(task_id=task.id, kind=kind, source=source, payload=payload or {})
    db.add(row); db.flush()
    deliver(db, task, row)
    core.event(db, task.project_id, "watchdog.control", {"control_id": row.id, "kind": kind,
               "source": source}, task.id, "control:" + row.id)
    return row


def deliver(db, task, row):
    if row.status in {"applied", "delivered", "read"}:
        return row
    row.owner = uid(); row.lease_until = time.time() + 30; row.attempts += 1
    row.status = "running"
    if row.kind in IMMEDIATE:
        if row.kind == "stop":
            from . import tickets
            tickets.end(db, task, by="human" if row.source != "watchdog" else "watchdog",
                        outcome="stopped", reason=row.payload.get("text", "stop"))
        else:
            core.command(db, task.id, row.kind, priority=row.payload.get("priority"),
                         authorization=row.payload.get("authorization"))
        row.applied_at = time.time(); row.status = "applied"
    else:
        if task.status in core.TERMINAL:
            raise core.Conflict("cannot control ended task")
        entry = core.input_task(db, task.id, {**row.payload, "kind": row.kind,
                 "source": row.source, "control_id": row.id}, wake=False, kind="steer")
        row.input_id = entry.id; row.status = "delivered"
    return row


def acknowledge(db, input_id):
    row = db.scalar(select(Control).where(Control.input_id == input_id))
    if row is not None:
        row.read_at = time.time(); row.status = "read"


def requested(text):
    """Exact user control commands bypass semantic routing."""
    value = text.strip().lower().rstrip(".!。！")
    groups = {"stop": {"stop", "/stop", "stop this", "stop task", "停止", "停止任务"},
              "pause": {"pause", "/pause", "暂停", "暂停任务"},
              "status": {"status", "/status", "状态", "查询状态"},
              "replace_session": {"/replace_session", "替换执行会话"}}
    return next((kind for kind, values in groups.items() if value in values), None)
