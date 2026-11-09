"""Trigger intake, thread correlation, subscription fan-out and Eval queuing.

A Source stores an immutable Trigger. The host filters it by the Source's scope, fans it out to the
tasks that subscribe to its Threads, and queues it for an Eval only when no ticket's worker decides
it. This is the deterministic router: no model runs when a hardcoded rule already settles the route.
"""
import time
import uuid as _uuid
from sqlalchemy import select
from . import core, policy, router_state, thread_keys as keys
from .db import transaction
from .models import Decision, Project, Task, Thread, Trigger, MessageOwnership, Inbox, uid

REQUEUES = 2
TERMINAL = {"completed", "cancelled"}


def receive(db, project, data):
    """Admit one event. `data` is a plain dict; returns stored | duplicate | filtered."""
    project = core.lock_project(db, project.id)
    policy.ensure_version(project)
    thread_keys = list(dict.fromkeys(keys.normalize(project.id, key) for key in (data.get("thread_keys") or []) if key))
    if not thread_keys:
        raise core.Conflict("a trigger must belong to at least one thread")
    dedupe = data.get("dedupe_key") or ("trigger:" + uid())
    # Receipt identity is scoped even for clients that reuse their delivery key.
    dedupe = project.id + ":" + dedupe
    if db.scalar(select(Trigger.id).where(Trigger.dedupe_key == dedupe)):
        return "duplicate"
    at = float(data.get("at") or time.time())
    row = Trigger(id=data.get("id") or uid(), project_id=project.id, source=data["source"],
                  kind=data.get("kind") or "event", external_id=str(data.get("external_id") or ""),
                  dedupe_key=dedupe, actor=str(data.get("actor") or ""),
                  human=bool(data.get("human", True)), addressed=data.get("addressed"),
                  thread_keys=thread_keys, text=(data.get("text") or "")[:60000],
                  url=data.get("url") or "", channel=data.get("channel") or {},
                  payload=data.get("payload") or {}, at=at, disposition={})
    db.add(row)
    db.flush()
    scoped = policy.scope(project, row)
    if not scoped["listened"]:
        row.disposition = {"filtered": scoped["reason"], "at": time.time()}
        return "filtered"
    if row.human and not row.addressed and not scoped["observed"]:
        # A scope set to mentions only never receives unaddressed discussion; it is not a candidate.
        row.disposition = {"filtered": "unaddressed outside receive-all scope", "at": time.time()}
        return "filtered"

    fact_only = bool(data.get("context_only")) or row.kind.startswith("ci.")
    reached = {}
    threads = []
    for key in thread_keys:
        thread = db.get(Thread, key)
        if thread is not None and thread.project_id != project.id:
            raise core.Conflict("thread belongs to another project")
        if thread is None:
            thread = Thread(key=key, project_id=project.id, source=row.source, pending=[],
                            evaluating={}, subscriptions=[], context=[], expired=0, last_at=at)
            db.add(thread)
        if not thread.last_at or at > thread.last_at:
            thread.last_at = at
        thread.updated_at = time.time()
        threads.append(thread)
        for task_id in (thread.subscriptions or []):
            task = db.get(Task, task_id)
            if task is None or task.project_id != project.id:
                continue
            context_only = task_id in (thread.context or [])
            # A person's message on the ticket's own thread resumes an ended task unless a person stopped it;
            # a context-only thread and bots/CI never resume one.
            resumes = row.human and not fact_only and not context_only and task.ended_by != "human"
            if task.status in TERMINAL and not resumes:
                continue
            prior = reached.get(task_id)
            reached[task_id] = (task, context_only and (prior[1] if prior else True))

    from . import control
    requested = control.requested(row.text) if row.human else None
    if requested and policy.allowed_for(policy.normalize(project.policy), row.source, row.actor):
        targets = [task for task, only in reached.values() if not only]
        if not targets:
            from .router import _correlate
            target = _correlate(db, project, row)
            targets = [target] if target else []
        if targets:
            for target in targets:
                control.submit(db, target, requested, {"text": row.text}, "owner")
            row.disposition = {"control": requested, "targets": [task.id for task in targets]}
            core.event(db, project.id, "trigger.control", {"trigger_id": row.id, "kind": requested})
            router_state.notice(db, project.id, "control",
                                {"trigger_id": row.id, "command": requested,
                                 "tickets": [task.id for task in targets], "actor": row.actor})
            return "stored"
    fanout = list(reached)
    context = [task_id for task_id, (_, only) in reached.items() if only]
    row.disposition = {"fanout": fanout, **({"context": context} if context else {}), "at": time.time()}
    if row.human and not fact_only:
        for task_id, (task, only) in reached.items():
            _add_input(db, task, row, context=only)
    else:
        # Bot and CI messages are context for subscribed tickets; they grant no authority and never resume one.
        for task_id, (task, only) in reached.items():
            _add_input(db, task, row, context=True, automatic=True)
    owners = [task_id for task_id, (task, only) in reached.items() if row.human and not fact_only and not only]
    if owners:
        # A person's follow-up belongs to its Executor; the Router only needs to see that it happened.
        router_state.notice(db, project.id, "follow_up",
                            {"trigger_id": row.id, "thread_keys": thread_keys, "tickets": owners,
                             "actor": row.actor, "addressed": row.addressed,
                             "text": (row.text or "")[:500]})
    if fact_only:
        # A CI fact is context and audit, never a candidate for new work.
        core.event(db, project.id, "trigger.received",
                   {"trigger_id": row.id, "source": row.source, "kind": row.kind,
                    "threads": thread_keys, "fanout": fanout}, None, "trigger:" + row.id)
        return "stored"
    if not owners:
        verdict = policy.gate(project, row, {"subscriptions": threads[0].subscriptions,
                                             "context": threads[0].context}, _tasks_on(db, threads[0]))
        if verdict["admissible"] and verdict["strength"] == "explicit":
            # Explicit requests are routed by the deterministic rules or the router, never assessed.
            core.enqueue(db, project.id, "explicit_candidate", {"trigger_id": row.id},
                         None, "route:" + row.id)
        else:
            _queue_for_eval(db, row, threads[0])
    core.event(db, project.id, "trigger.received",
               {"trigger_id": row.id, "source": row.source, "kind": row.kind,
                "threads": thread_keys, "fanout": fanout}, None, "trigger:" + row.id)
    return "stored"


def _add_input(db, task, trigger, context=False, automatic=False):
    from . import tickets
    payload = {"text": trigger.text, "source": trigger.source, "kind": trigger.kind,
               "trigger_id": trigger.id, "actor": trigger.actor, "url": trigger.url,
               "context": context, "automatic": automatic}
    tickets.add_input(db, task, kind="trigger", trigger_id=trigger.id, payload=payload,
                      wake=not automatic)


def _queue_for_eval(db, trigger, thread):
    thread.pending = list(thread.pending or []) + [trigger.id]
    thread.updated_at = time.time()
    db.flush()


def evaluate_due(db, project=None):
    """Start Evals for threads whose debounce window closed, and expire overdue ones."""
    now = time.time()
    query = select(Thread)
    if project is not None:
        query = query.where(Thread.project_id == project.id)
    started = []
    for thread in db.scalars(query):
        if thread.evaluating and thread.evaluating.get("id"):
            _expire(db, thread, now)
            continue
        if not (thread.pending or []):
            continue
        project_row = db.get(Project, thread.project_id)
        if project_row.paused:
            continue
        policy.ensure_version(project_row)
        debounce = int((project_row.config or {}).get("discussion_debounce_seconds",
                                                      20 if thread.source == "feishu" else 0))
        if thread.source != "feishu":
            debounce = 0
        pending_times = [row.created_at for value in thread.pending
                         if (row := db.get(Trigger, value)) is not None]
        oldest = min(pending_times, default=now)
        if thread.last_at and now - thread.last_at < debounce and now - oldest < 60:
            continue
        started.append(_start(db, project_row, thread, now))
    return [item for item in started if item]


def _start(db, project, thread, now):
    pending = [trigger_id for trigger_id in (thread.pending or []) if (item := db.get(Trigger, trigger_id)) is not None and item.project_id == project.id]
    if not pending:
        thread.pending = []
        return None
    # Drop triggers that already originated a ticket; never let a replay create a duplicate.
    accepted = _accepted_triggers(db, thread.key)
    consumed = [trigger_id for trigger_id in pending if trigger_id in accepted]
    live_pending = [trigger_id for trigger_id in pending if trigger_id not in accepted]
    if consumed:
        _record(db, project, thread, {"id": "ignore:" + thread.key + ":" + str(now),
                                      "trigger_ids": consumed, "primary": consumed[0],
                                      "strength": "inferred", "policy_version": project.policy_version},
                "ignore", {"started_ticket": accepted[consumed[0]]})
    if not live_pending:
        thread.pending = []
        return None
    strongest = None
    for trigger_id in live_pending:
        trigger = db.get(Trigger, trigger_id)
        result = policy.gate(project, trigger, {"subscriptions": thread.subscriptions,
                                                "context": thread.context}, _tasks_on(db, thread))
        if not result["admissible"]:
            continue
        if (strongest is None or policy.STRENGTH[result["strength"]] > policy.STRENGTH[strongest[1]["strength"]]
                or (result["strength"] == strongest[1]["strength"] and trigger.at >= strongest[0].at)):
            strongest = (trigger, result)
    if strongest is None:
        _record(db, project, thread, {"id": "ignore:" + thread.key + ":" + str(now),
                                      "trigger_ids": live_pending, "primary": live_pending[0],
                                      "strength": "inferred", "policy_version": project.policy_version},
                "ignore", {"reason": "no_admissible"})
        thread.pending = []
        return None
    trigger, result = strongest
    if result["strength"] == "explicit" and (trigger.disposition or {}).get("released"):
        job = core.enqueue(db, project.id, "explicit_candidate", {"trigger_id": trigger.id},
                           None, "route:" + trigger.id)
        job.status = "pending"; job.available_at = now
        thread.pending = [identifier for identifier in live_pending if identifier != trigger.id]
        return job.id
    eval_id = "eval:" + thread.key + ":" + str(_uuid.uuid4())
    timeout = int((project.config or {}).get("eval_timeout_seconds", 120))
    thread.evaluating = {"id": eval_id, "thread_key": thread.key, "trigger_ids": live_pending,
                         "primary": trigger.id, "strength": result["strength"],
                         "actor": trigger.actor, "basis": result["basis"],
                         "policy_version": project.policy_version, "started_at": now,
                         "deadline": now + timeout, "delivered": False}
    thread.pending = []
    for trigger_id in live_pending:
        row = db.get(Trigger, trigger_id)
        if row:
            row.disposition = {**(row.disposition or {}), "eval": eval_id}
    core.effect(db, project.id, "assessment", "assessment:" + eval_id,
                {"eval_id": eval_id}, None)
    core.event(db, project.id, "eval.started", {"eval_id": eval_id, "thread": thread.key,
                                                "trigger_ids": live_pending,
                                                "strength": result["strength"]})
    return eval_id


def _expire(db, thread, now):
    evaluating = thread.evaluating
    if not evaluating.get("delivered") or evaluating.get("deadline", 0) > now:
        return
    expired = int(thread.expired or 0) + 1
    project = db.get(Project, thread.project_id)
    if expired > REQUEUES:
        _record(db, project, thread, evaluating, "unavailable", {"reason": "timeout"})
        thread.evaluating = {}
        thread.expired = 0
        return
    ids = evaluating.get("trigger_ids") or []
    thread.pending = list(dict.fromkeys(ids + list(thread.pending or [])))
    thread.evaluating = {}
    thread.expired = expired


def _accepted_triggers(db, thread_key):
    accepted = {}
    for decision in db.scalars(select(Decision).where(Decision.thread_key == thread_key,
                                                      Decision.kind == "accept")):
        ticket_id = (decision.result or {}).get("ticket_id")
        if not ticket_id:
            continue
        for trigger_id in (decision.trigger_ids or []):
            owner = db.get(MessageOwnership, (trigger_id, ticket_id))
            if owner is None or owner.released_at is None:
                accepted.setdefault(trigger_id, ticket_id)
    return accepted


def _tasks_on(db, thread):
    values = []
    for task_id in (thread.subscriptions or []):
        task = db.get(Task, task_id)
        if task is not None and task.project_id == thread.project_id:
            values.append(task)
    return values


def decision(db, project, thread, evaluation, kind, reason=None, *, mandate=None,
             ticket_id=None, audit=None):
    """Record an Eval Decision. `reason` may be a string or a dict."""
    return _record(db, project, thread, evaluation, kind, reason or {},
                   mandate=mandate, ticket_id=ticket_id, audit=audit or {})


def _record(db, project, thread, evaluation, kind, reason=None, *, mandate=None,
            ticket_id=None, audit=None):
    trigger_ids = [value for value in (evaluation.get("trigger_ids") or [evaluation.get("primary")]) if value]
    row = Decision(project_id=project.id, trigger_id=evaluation.get("primary") or "",
                   kind=kind, thread_key=thread.key if thread else evaluation.get("thread_key"),
                   trigger_ids=trigger_ids, strength=evaluation.get("strength"),
                   mandate=mandate or {}, audit=audit or {},
                   action={"name": "eval", "decision": kind}, result={"reason": reason or {},
                                                                      **({"ticket_id": ticket_id} if ticket_id else {})},
                   rationale=str(reason or ""))
    db.add(row)
    db.flush()
    for trigger_id in trigger_ids:
        trigger = db.get(Trigger, trigger_id)
        if trigger:
            trigger.disposition = {**(trigger.disposition or {}), "decision": row.id}
    core.event(db, project.id, "decision.recorded",
               {"decision_id": row.id, "kind": kind,
                "thread": evaluation.get("thread_key")}, ticket_id, "decision:" + row.id)
    router_state.record(db, project.id, "decision",
                        {"decision_id": row.id, "decision": kind,
                         "thread": evaluation.get("thread_key"), "ticket_id": ticket_id,
                         "strength": evaluation.get("strength"),
                         "trigger_ids": trigger_ids,
                         "reason": str(reason or "")[:300]})
    if thread is not None:
        if thread.evaluating.get("id") == evaluation.get("id"):
            thread.expired = 0
        thread.evaluating = {} if thread.evaluating.get("id") == evaluation.get("id") else thread.evaluating
    return row


def requeue(db, project, trigger_id):
    trigger = db.get(Trigger, trigger_id)
    if trigger is None or trigger.project_id != project.id:
        raise core.Missing("trigger not found")
    if trigger.id in _accepted_triggers(db, trigger.thread_keys[0]):
        return False
    thread = db.get(Thread, trigger.thread_keys[0])
    job = db.scalar(select(Inbox).where(
        Inbox.dedupe_key == "route:" + trigger.id))
    if job is not None and job.status == "unavailable":
        job.status = "pending"; job.attempts = 0; job.available_at = time.time()
        router_state.notice(db, project.id, "trigger_requeued", {"trigger_id": trigger.id})
        return True
    if thread and trigger.id in (thread.evaluating or {}).get("trigger_ids", []):
        return False
    if thread is None:
        thread = Thread(key=trigger.thread_keys[0], project_id=project.id, source=trigger.source,
                        pending=[], evaluating={}, subscriptions=[], context=[], expired=0,
                        last_at=trigger.at)
        db.add(thread)
    thread.pending = list(dict.fromkeys(list(thread.pending or []) + [trigger.id]))
    thread.expired = 0
    trigger.disposition = {**(trigger.disposition or {}), "requeued": True}
    router_state.notice(db, project.id, "trigger_requeued", {"trigger_id": trigger.id})
    return True


def candidates(db, project, limit=100):
    rows = list(db.scalars(select(Trigger).where(Trigger.project_id == project.id)
                           .order_by(Trigger.created_at.desc()).limit(limit)))
    return [row for row in rows if not (row.disposition or {}).get("decision")]


def api_request(db, project, text, *, conversation_id=None, task=None, scope="implement", identity=None):
    from . import tickets
    identifier = uid()
    thread_key = keys.make(project.id, "api", "session" if conversation_id else "task",
                           conversation_id or (task.id if task else identifier))
    if task is not None:
        if task.project_id != project.id:
            raise core.Conflict("task belongs to another project")
        tickets.subscribe(db, task, thread_key)
    actors = policy.normalize(project.policy)
    actor = (actors["source_allowed"]["api"] or actors["allowed_actors"])[0]
    receive(db, project, {"id": identifier, "source": "api", "kind": "message.created",
        "actor": actor, "human": True, "addressed": "direct", "thread_keys": [thread_key],
        "text": text, "external_id": identity or identifier, "dedupe_key": "api:" + (identity or identifier),
        "channel": {"conversation_id": conversation_id, "task_id": task.id if task else None, "scope": scope}})
    return db.get(Trigger, identifier)
