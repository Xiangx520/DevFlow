"""Ticket lifecycle: create from a Mandate, subscribe Threads, deliver Inputs, resume and end.

A Ticket is DevFlow's `Task`; the Mandate states why the work may be done and how far it may go.
Only an accept from the router, an assessment or an explicit owner request creates one.
"""
import time
from sqlalchemy import select
from . import core, policy, ownership, router_state, thread_keys as keys
from .db import transaction
from .models import Project, Task, Thread, Trigger, MessageOwnership

TERMINAL = {"completed", "cancelled"}


def create(db, project, *triggers, title, goal, acceptance=None, scope="implement",
           mandate=None, issue_number=None, pr_number=None, priority=0, kind="accept"):
    """Create a ticket for an accepted trigger. Idempotent by issue number for replay safety."""
    trigger = triggers[0] if triggers else None
    if issue_number:
        existing = db.scalar(select(Task).where(Task.project_id == project.id,
                                                Task.issue_number == issue_number,
                                                Task.status.not_in(list(TERMINAL))))
        if existing and not any((owner := db.get(MessageOwnership, (item.id, existing.id))) is not None
                                and owner.released_at is not None for item in triggers):
            return existing
    task = core.create_task(db, project.id, title, goal, acceptance or [], scope,
                            issue_number, priority, mandate=mandate, pr_number=pr_number, reuse_issue=False)
    task.mandate = mandate or {"strength": "explicit", "policy_version": project.policy_version,
                               "basis": {"via": "owner"}, "scope": scope,
                               "actor": (trigger.actor if trigger else "owner")}
    from . import specs
    revision = specs.record_policy(db, project)
    task.mandate = {**task.mandate, "policy_snapshot": revision.body,
                    "rules_snapshot": policy.enabled_rules(revision.body)}
    task.spec_version = project.spec_version
    for item in triggers:
        add_input(db, task, kind="trigger", trigger_id=item.id, wake=False,
                  payload={"text": item.text, "source": item.source, "kind": item.kind,
                           "trigger_id": item.id, "actor": item.actor, "url": item.url,
                           "channel": item.channel or {}})
        for key in (item.thread_keys or []):
            subscribe(db, task, key)
            own = _own_thread(item)
            if own:
                _follow(db, task, key, own=True)
    if trigger is not None:
        origin_ids = {item.id for item in triggers}
        source_threads = {key for item in triggers for key in (item.thread_keys or [])}
        history = [item for item in db.scalars(select(Trigger).where(Trigger.project_id == project.id)
                   .order_by(Trigger.at.desc()).limit(100))
                   if item.id not in origin_ids and source_threads.intersection(item.thread_keys or [])][:20]
        for item in reversed(history):
            add_input(db, task, kind="trigger", trigger_id=item.id, wake=False,
                      payload={"text": item.text, "source": item.source, "actor": item.actor,
                               "url": item.url, "channel": item.channel, "context": True})
    db.flush()
    if trigger is not None:
        core.event(db, project.id, "ticket.created",
                   {"task_id": task.id, "trigger_id": trigger.id, "strength": task.mandate.get("strength")},
                   task.id, "ticket:" + task.id)
    return task


def _own_thread(trigger):
    channel = trigger.channel or {}
    if trigger.source == "github":
        return bool(channel.get("is_pull"))
    return False


def add_input(db, task, kind="text", payload=None, trigger_id=None, wake=True):
    """Add an Input once and wake a resumable ended task under its Mandate."""
    if task.status in TERMINAL:
        trigger = db.get(Trigger, trigger_id) if trigger_id else None
        own_thread = trigger is not None and trigger.human and any(
            (thread := db.get(Thread, key)) is not None and thread.project_id == task.project_id
            and task.id in (thread.subscriptions or []) and task.id not in (thread.context or [])
            for key in (trigger.thread_keys or []))
        if not (own_thread and wake and kind == "trigger" and not (payload or {}).get("context") and task.ended_by != "human" and resume(db, task)):
            return None
    if trigger_id and not (payload or {}).get("context"):
        if not ownership.acquire(db, task, trigger_id):
            return None
    row = core.input_task(db, task.id, payload or {}, wake=False, kind=kind, trigger_id=trigger_id)
    if wake and task.status in {"waiting_external", "ready_for_review", "failed"}:
        core.transition(db, task, "queued", "new input")
    return row


def wake(db, task):
    """Resume an ended ticket when a person's message waits for it; no new authorization."""
    if task.status not in TERMINAL or task.ended_by == "human":
        return False
    return resume(db, task)


def resume(db, task):
    project = core.get(db, Project, task.project_id)
    if not policy.revalidate(project, task.mandate or {}):
        return False
    core.transition(db, task, "queued", "human follow-up resumes ticket")
    core.event(db, task.project_id, "ticket.resumed", {"task_id": task.id}, task.id,
               "resume:" + str(time.time()))
    router_state.notice(db, task.project_id, "ticket_resumed", {"task_id": task.id})
    return True


def subscribe(db, task, thread_key, context=False):
    thread_key = keys.normalize(task.project_id, thread_key)
    thread = db.get(Thread, thread_key)
    if thread is not None and thread.project_id != task.project_id:
        raise core.Conflict("thread belongs to another project")
    if thread is None:
        source_key = thread_key.removeprefix(task.project_id + ":")
        thread = Thread(key=thread_key, project_id=task.project_id, source=source_key.split(":", 1)[0],
                        pending=[], evaluating={}, subscriptions=[], context=[], expired=0, last_at=0)
        db.add(thread)
    task_ids = list(thread.subscriptions or [])
    if task.id not in task_ids:
        task_ids.append(task.id)
    thread.subscriptions = task_ids
    if context:
        thread.context = list(dict.fromkeys(list(thread.context or []) + [task.id]))
    else:
        thread.context = [item for item in (thread.context or []) if item != task.id]
    thread.updated_at = time.time()
    db.flush()


def follow_for_context(db, task, thread_key):
    if task.status in TERMINAL:
        raise core.Conflict("cannot follow a thread for an ended ticket")
    subscribe(db, task, thread_key, context=True)
    router_state.notice(db, task.project_id, "thread_followed",
                        {"task_id": task.id, "thread": keys.normalize(task.project_id, thread_key)})


def mark_unrelated(db, task, thread_key, reason=""):
    """Drop this ticket's subscription to an attached thread and hand back owned messages."""
    if task.status in TERMINAL or task.ended_by == "human":
        raise core.Conflict("cannot change an ended ticket's threads")
    thread_key = keys.normalize(task.project_id, thread_key)
    origin = []
    if task.issue_number:
        origin.append(f"{task.project_id}:github:issue:{task.issue_number}")
    if task.pr_number:
        origin.append(f"{task.project_id}:github:pr:{task.pr_number}")
    if thread_key in origin:
        raise core.Conflict("cannot drop the thread that started the ticket")
    follows = [dict(item) for item in (task.follows or [])]
    if any(item.get("thread") == thread_key and item.get("own") for item in follows):
        raise core.Conflict("cannot drop a thread this ticket opened")
    thread = db.get(Thread, thread_key)
    if thread is None or thread.project_id != task.project_id:
        raise core.Missing("thread not found")
    if task.id not in (thread.subscriptions or []):
        return False
    thread.subscriptions = [item for item in (thread.subscriptions or []) if item != task.id]
    thread.context = [item for item in (thread.context or []) if item != task.id]
    task.follows = [item for item in follows if item.get("thread") != thread_key]
    released = []
    for owner in list(db.scalars(select(MessageOwnership).where(
            MessageOwnership.task_id == task.id, MessageOwnership.released_at.is_(None)))):
        trigger = db.get(Trigger, owner.trigger_id)
        if trigger is not None and thread_key in (trigger.thread_keys or []):
            ownership.release(db, task, trigger.id, reason or "thread marked unrelated")
            released.append(trigger.id)
    thread.updated_at = time.time()
    router_state.notice(db, task.project_id, "thread_unrelated",
                        {"task_id": task.id, "thread": thread_key,
                         "reason": (reason or "")[:300], "released": released})
    return True


def _follow(db, task, thread_key, own=False):
    thread_key = keys.normalize(task.project_id, thread_key)
    follows = [dict(item) for item in (task.follows or [])]
    found = next((item for item in follows if item.get("thread") == thread_key), None)
    if found:
        found["own"] = bool(found.get("own") or own)
    else:
        follows.append({"thread": thread_key, "own": own})
    task.follows = follows


def attach(db, task, trigger, note=None):
    """Subscribe the ticket to the trigger's threads and deliver the trigger as an Input."""
    if trigger.project_id != task.project_id:
        raise core.Conflict("cross-project attachment")
    owner = db.get(MessageOwnership, (trigger.id, task.id))
    if owner is not None and owner.released_at is not None:
        raise core.Conflict("message already released by this task")
    if task.ended_by == "human":
        raise core.Conflict("ticket was stopped by a person")
    project = core.get(db, Project, task.project_id)
    if task.status in TERMINAL and not policy.revalidate(project, task.mandate or {}):
        raise core.Conflict("mandate revoked")
    for key in (trigger.thread_keys or []):
        subscribe(db, task, key)
    payload = {"text": trigger.text, "source": trigger.source, "kind": trigger.kind,
               "trigger_id": trigger.id, "actor": trigger.actor, "url": trigger.url,
               "note": note or "", "attached": True}
    add_input(db, task, kind="trigger", payload=payload, trigger_id=trigger.id, wake=True)
    return task


def end(db, task, by="executor", outcome="stopped", reason="", at=None):
    if task.status in TERMINAL:
        return task
    for owner in list(db.scalars(select(MessageOwnership).where(
            MessageOwnership.task_id == task.id, MessageOwnership.released_at.is_(None),
            MessageOwnership.read_at.is_(None), MessageOwnership.processed_at.is_(None)))):
        ownership.release(db, task, owner.trigger_id, "task ended before reading")
    task.ended_by = by
    task.outcome = outcome
    task.ended_at = at or time.time()
    status = "completed" if outcome in {"completed", "merged"} else "cancelled"
    core.transition(db, task, status, reason or outcome)
    core.revoke(db, task, "ticket ended")
    core.event(db, task.project_id, "ticket.ended",
               {"task_id": task.id, "by": by, "outcome": outcome, "reason": reason}, task.id,
               "end:" + task.id + ":" + str(task.ended_at))
    router_state.notice(db, task.project_id, "ticket_ended",
                        {"task_id": task.id, "by": by, "outcome": outcome, "reason": reason[:300]})
    return task


def thread_closed(db, project, thread_key, outcome):
    """Record closure; settle only when this task has finished its accepted work."""
    thread_key = keys.normalize(project.id, thread_key)
    number = None
    if thread_key.startswith("github:pr:") or ":github:pr:" in thread_key:
        try:
            number = int(thread_key.rsplit(":", 1)[1])
        except ValueError:
            number = None
    for task in db.scalars(select(Task).where(Task.project_id == project.id,
                                              Task.status.not_in(list(TERMINAL)))):
        follows = [dict(item) for item in (task.follows or [])]
        match = [item for item in follows if item.get("thread") == thread_key]
        if not match and number is not None and task.pr_number == number:
            match = [{"thread": thread_key, "own": task.pr_number == number}]
        if not match:
            continue
        for item in follows:
            if item.get("thread") == thread_key:
                item["closed"] = outcome
        if not any(item.get("thread") == thread_key for item in follows):
            follows.append({"thread": thread_key, "own": task.pr_number == number, "closed": outcome})
        task.follows = follows
        settle_closed(db, task)


def settle_closed(db, task):
    follows = task.follows or []
    own = [item for item in follows if item.get("own")]
    counted = own or follows
    if not counted or not all(item.get("closed") for item in counted):
        return False
    pending = db.scalar(select(MessageOwnership.task_id).where(
        MessageOwnership.task_id == task.id, MessageOwnership.released_at.is_(None),
        MessageOwnership.processed_at.is_(None)).limit(1))
    if task.status == "running" or pending or task.report_state in {"blocked", "failed"}:
        return False
    outcome = "merged" if any(item.get("closed") == "merged" for item in counted) else "closed"
    end(db, task, by="executor", outcome=outcome, reason="pull request " + outcome)
    return True


def stage(task):
    """Derive the ticket stage from stored state, as super-agent's stage table does."""
    if task.status in TERMINAL:
        return "ended"
    if task.status == "waiting_human":
        return "person/answer"
    if task.status == "ready_for_review":
        return "person/review"
    if task.status == "paused":
        return "paused"
    if task.report_state == "blocked":
        return "person/blocked"
    if task.status == "failed" or task.report_state == "failed":
        return "person/failed"
    if task.status == "waiting_external" and task.wait_reason == "supervision":
        return "person/review"
    if task.status == "waiting_external":
        return "idle/waiting"
    if task.status == "running":
        return "working"
    return "working/queued"


def request_status(db, task):
    if task.status != "waiting_external":
        return False
    add_input(db, task, kind="text",
              payload={"text": "Report the current ticket status. Do not resume implementation or perform external actions.",
                       "status_only": True})
    return True


def handback(db, task, trigger_id, reason):
    """A worker says a message is beyond this ticket; let it reach a fresh Eval."""
    trigger = db.get(Trigger, trigger_id)
    if trigger is None:
        raise core.Missing("trigger not found")
    thread_key = (trigger.thread_keys or [None])[0]
    if not thread_key:
        return False
    thread = db.get(Thread, thread_key)
    if trigger.project_id != task.project_id or thread is None or task.id not in (thread.subscriptions or []):
        return False
    disposition = dict(trigger.disposition or {})
    owner = db.get(MessageOwnership, (trigger.id, task.id))
    if owner is None:
        return False
    handbacks = list(disposition.get("handbacks") or [])
    if not any(item.get("ticket_id") == task.id for item in handbacks):
        handbacks.append({"ticket_id": task.id, "reason": reason})
    disposition["handbacks"] = handbacks
    trigger.disposition = disposition
    ownership.release(db, task, trigger_id, reason)
    router_state.notice(db, task.project_id, "handback",
                        {"task_id": task.id, "trigger_id": trigger_id, "reason": reason[:300]})
    return True
