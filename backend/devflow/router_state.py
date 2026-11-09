"""Project-level Router memory.

The Router is more than a per-event function: it keeps a durable, bounded view of recent decisions,
human follow-ups and worker reports. This module is the only writer of RouterState.
"""
import time
from sqlalchemy import select
from .models import Project, RouterState, Task, uid

MEMORY_LIMIT = 200
NOTICE_LIMIT = 200
TERMINAL = {"completed", "cancelled"}


def get(db, project_id):
    return db.get(RouterState, project_id)


def ensure(db, project_id):
    row = db.get(RouterState, project_id)
    if row is None:
        row = RouterState(project_id=project_id, summary="", memory=[], notices=[])
        db.add(row)
        db.flush()
    return row


def record(db, project_id, kind, payload):
    """Append one compact fact to the Router's durable memory."""
    row = ensure(db, project_id)
    entry = {"at": time.time(), "kind": kind, **payload}
    row.memory = (list(row.memory or []) + [entry])[-MEMORY_LIMIT:]
    row.updated_at = time.time()
    return entry


def notice(db, project_id, kind, payload):
    """Queue a fact the Router should see on its next turn (follow-up, report, end, handback)."""
    row = ensure(db, project_id)
    entry = {"id": uid(), "at": time.time(), "kind": kind, **payload}
    row.notices = (list(row.notices or []) + [entry])[-NOTICE_LIMIT:]
    row.updated_at = time.time()
    return entry


def snapshot(db, project_id, limit=20):
    row = get(db, project_id)
    if row is None:
        return {"summary": "", "memory": [], "notices": [], "lease_until": 0}
    return {"summary": row.summary,
            "memory": list(row.memory or [])[-limit:],
            "notices": list(row.notices or [])[-limit:],
            "lease_until": row.lease_until or 0}


def summary(db, project_id):
    row = get(db, project_id)
    if row is not None and row.summary:
        return row.summary
    open_tasks = list(db.scalars(select(Task).where(Task.project_id == project_id,
                                                    Task.status.not_in(TERMINAL))))
    if not open_tasks:
        return "暂无进行中的工作。"
    return f"{len(open_tasks)} 个进行中的工单；最近决策与后续消息保存在 Router 记忆中。"
