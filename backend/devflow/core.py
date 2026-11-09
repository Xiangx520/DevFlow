"""Deterministic commands: the only authority for task state and external effects."""
import hashlib
import json
import re
import secrets
import time
from sqlalchemy import select
from . import router_state
from .models import (Artifact, Attention, Attempt, Dependency, Effect, Event, ExecutionSession,
                     Finding, Inbox, Project, Task, TaskInput, uid, view)

TERMINAL = {"completed", "cancelled"}
DEFAULT_LIMITS = {"turn_seconds": 1800, "task_seconds": 7200, "tokens": 500000, "ci_repairs": 3}

class Conflict(ValueError): pass
class Missing(LookupError): pass

def get(db, model, identifier):
    value = db.get(model, identifier)
    if value is None: raise Missing(f"{model.__name__} not found")
    return value

def lock_project(db, identifier):
    project = db.scalar(select(Project).where(Project.id == identifier).with_for_update())
    if project is None: raise Missing("Project not found")
    return project

def lock_task(db, identifier):
    task = get(db, Task, identifier)
    lock_project(db, task.project_id)
    db.refresh(task)
    return task

def event(db, project_id, kind, payload, task_id=None, key=None, attempt_id=None, cause_id=None):
    key = key or uid()
    old = db.scalar(select(Event).where(Event.dedupe_key == key))
    if old: return old
    row = Event(project_id=project_id, task_id=task_id, kind=kind, payload=payload,
                dedupe_key=key, attempt_id=attempt_id, cause_id=cause_id)
    db.add(row); db.flush()
    return row

def enqueue(db, project_id, kind, payload, task_id=None, key=None):
    key = key or uid()
    old = db.scalar(select(Inbox).where(Inbox.dedupe_key == key))
    if old: return old
    row = Inbox(project_id=project_id, task_id=task_id, kind=kind, payload=payload, dedupe_key=key)
    db.add(row); db.flush()
    return row

def effect(db, project_id, kind, key, payload, task_id=None):
    old = db.scalar(select(Effect).where(Effect.key == key))
    if old: return old
    row = Effect(project_id=project_id, task_id=task_id, kind=kind, key=key, payload=payload)
    db.add(row); db.flush()
    return row

def notify(db, task, purpose, text, suffix=""):
    project = get(db, Project, task.project_id)
    effect(db, task.project_id, "notification", f"notice:{task.id}:{purpose}:{suffix}",
           {"purpose": purpose, "text": text, "github_number": task.pr_number or task.issue_number,
            "feishu": bool(project.config.get("notify_feishu", True))}, task.id)

def transition(db, task, status, reason=""):
    before = task.status
    task.status = status; task.updated_at = time.time()
    task.wait_reason = reason
    task.waiting_since = time.time() if status.startswith("waiting_") else None
    if status in TERMINAL and not task.ended_at:
        task.ended_at = time.time()
        task.ended_by = task.ended_by or "system"
    event(db, task.project_id, "task.state", {"from": before, "to": status, "reason": reason}, task.id)

def new_project(db, repository, policy, config, default_branch="main"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise Conflict("repository must be owner/name")
    if not policy.get("allowed_actors"): raise Conflict("allowed_actors is required")
    from . import policy as policy_module
    try:
        normalized = policy_module.normalize(policy)
    except ValueError as exc:
        raise Conflict(str(exc))
    if not config.get("verify_commands"): raise Conflict("configure at least one verification command")
    p = Project(repository=repository, policy=normalized, config=config,
                default_branch=default_branch, policy_version=policy_module.version(normalized),
                spec_version=0)
    db.add(p); db.flush()
    from . import specs
    specs.record_policy(db, p)
    return p

def create_task(db, project_id, title, goal, acceptance, scope="implement",
                issue_number=None, priority=0, authorization=None, mandate=None, pr_number=None, reuse_issue=True):
    project = lock_project(db, project_id)
    if scope not in {"analysis", "implement"}: raise Conflict("unsupported scope")
    if not title.strip() or not goal.strip(): raise Conflict("title and goal are required")
    if issue_number and reuse_issue:
        existing = db.scalar(select(Task).where(Task.project_id == project_id,
                    Task.issue_number == issue_number, Task.status.not_in(["completed", "cancelled"])))
        if existing: return existing
    from . import specs, policy as policy_module
    revision = specs.record_policy(db, project)
    if mandate is None:
        actor = (revision.body["source_allowed"]["api"] or revision.body["allowed_actors"])[0]
        mandate = {"strength": "explicit", "actor": actor, "scope": scope,
                   "policy_version": project.policy_version, "policy_snapshot": revision.body,
                   "basis": {"source": "api", "via": "owner"}}
    t = Task(id=uid(), project_id=project_id, title=title, goal=goal, acceptance=acceptance,
             scope=scope, issue_number=issue_number, pr_number=pr_number, priority=priority,
             authorization=authorization or {"source": "owner_api", "scope": scope},
             mandate=mandate or {}, follows=[], spec_version=project.spec_version,
             report_state="")
    t.branch = "devflow/" + t.id
    db.add(t); db.flush()
    s = ExecutionSession(task_id=t.id, handoff={})
    db.add(s); db.flush(); t.session_id = s.id
    from . import specs
    specs.load_new(db, t)
    event(db, project_id, "task.created", {"title": title, "scope": scope}, t.id)
    notify(db, t, "accepted", f"已接单：{title}。目标：{goal[:300]}")
    return t

def add_dependency(db, task_id, prerequisite_id):
    task = lock_task(db, task_id); prerequisite = get(db, Task, prerequisite_id)
    if prerequisite.project_id != task.project_id: raise Conflict("cross-project dependencies unsupported")
    if task_id == prerequisite_id: raise Conflict("dependency cycle")
    edges = list(db.scalars(select(Dependency)))
    graph = {}
    for e in edges: graph.setdefault(e.task_id, []).append(e.prerequisite_id)
    pending = [prerequisite_id]; seen = set()
    while pending:
        node = pending.pop()
        if node == task_id: raise Conflict("dependency cycle")
        if node in seen: continue
        seen.add(node); pending.extend(graph.get(node, []))
    if db.get(Dependency, (task_id, prerequisite_id)) is None:
        db.add(Dependency(task_id=task_id, prerequisite_id=prerequisite_id))
        event(db, task.project_id, "task.dependency", {"prerequisite_id": prerequisite_id}, task_id)
    # A newly unmet dependency must stop already running work too.
    if prerequisite.status != "completed" and task.status in {"running", "ready_for_review"}:
        transition(db, task, "queued", "waiting for prerequisite")
        revoke(db, task, "dependency changed")

def input_task(db, task_id, payload, wake=True, kind="text", trigger_id=None):
    t = lock_task(db, task_id)
    if trigger_id:
        existing = db.scalar(select(TaskInput).where(TaskInput.task_id == task_id,
                                                     TaskInput.trigger_id == trigger_id))
        if existing: return existing
    row = TaskInput(task_id=task_id, kind=kind, trigger_id=trigger_id, payload=payload)
    db.add(row); db.flush()
    t.input_version += 1
    event(db, t.project_id, "task.input", {"input_id": row.id, "kind": kind, **payload}, task_id)
    if wake and t.status in {"waiting_external", "ready_for_review", "failed"}:
        transition(db, t, "queued", "new input")
    return row

def revoke(db, task, reason):
    task.lease_owner = None; task.lease_until = 0
    for a in db.scalars(select(Attempt).where(Attempt.task_id == task.id, Attempt.status == "running")):
        elapsed = max(0, min(time.time() - a.last_heartbeat, 90))
        task.active_seconds += elapsed
        a.status = "interrupted"; a.finished_at = time.time(); a.reason = reason

def command(db, task_id, action, *, priority=None, authorization=None):
    task = lock_task(db, task_id)
    if action == "pause":
        if task.status in TERMINAL: raise Conflict("terminal task cannot be paused")
        transition(db, task, "paused", "user paused"); revoke(db, task, "paused")
    elif action == "cancel":
        from . import tickets
        tickets.end(db, task, by="human", outcome="stopped", reason="user cancelled")
    elif action in {"resume", "retry"}:
        if task.ended_by == "human": raise Conflict("human stop revoked mandate; create a new authorized task")
        if task.status == "completed": raise Conflict("completed task cannot resume")
        if db.scalar(select(Attention.id).where(Attention.task_id == task.id, Attention.answer.is_(None))):
            raise Conflict("answer unresolved attention first")
        from . import policy
        project = get(db, Project, task.project_id)
        if task.mandate and not policy.revalidate(project, task.mandate):
            raise Conflict("mandate no longer holds under current policy")
        transition(db, task, "queued", "explicit resume")
    elif action == "priority":
        if priority is None: raise Conflict("priority required")
        task.priority = priority
        event(db, task.project_id, "task.priority", {"priority": priority}, task.id)
    elif action == "replace_session":
        revoke(db, task, "executor replaced")
        old = get(db, ExecutionSession, task.session_id)
        old.state = "replaced"
        artifacts = list(db.scalars(select(Artifact).where(Artifact.task_id == task.id).order_by(Artifact.created_at.desc()).limit(8)))
        s = ExecutionSession(task_id=task.id, predecessor_id=old.id,
            handoff={"goal": task.goal, "acceptance": task.acceptance, "branch": task.branch,
                     "head_sha": task.head_sha, "artifacts": [view(x) for x in artifacts]})
        db.add(s); db.flush(); task.session_id = s.id
        if task.status not in TERMINAL | {"paused", "waiting_human"}:
            transition(db, task, "queued", "new executor with preserved workspace")
    elif action == "authorize":
        if not authorization: raise Conflict("authorization required")
        task.authorization = {**task.authorization, **authorization}; task.authorization_version += 1
        input_task(db, task.id, {"source": "owner", "authorization_updated": task.authorization})
    else: raise Conflict("unknown command")
    return task

def ask(db, task, question, recommendation="", impact=""):
    existing = db.scalar(select(Attention).where(Attention.task_id == task.id, Attention.question == question,
                                               Attention.answer.is_(None)))
    if existing: return existing
    a = Attention(task_id=task.id, question=question, recommendation=recommendation, impact=impact)
    db.add(a); db.flush()
    transition(db, task, "waiting_human", question); revoke(db, task, "human decision")
    notify(db, task, "attention", f"需要决定：{question}\n建议：{recommendation}\n影响：{impact}", a.id)
    router_state.notice(db, task.project_id, "attention",
                        {"task_id": task.id, "attention_id": a.id, "question": question[:400]})
    return a

def answer(db, task_id, attention_id, text):
    task = lock_task(db, task_id); a = get(db, Attention, attention_id)
    if a.task_id != task_id: raise Missing("Attention not in task")
    if task.status in TERMINAL: raise Conflict("resume task before answering")
    if a.answer is not None: raise Conflict("attention already answered")
    a.answer = text; a.resolved_at = time.time(); db.flush()
    router_state.notice(db, task.project_id, "attention_answered",
                        {"task_id": task.id, "attention_id": a.id, "text": text[:400]})
    input_task(db, task_id, {"source": "human_answer", "attention_id": a.id, "text": text}, wake=False)
    unresolved = db.scalar(select(Attention.id).where(Attention.task_id == task_id, Attention.answer.is_(None)))
    if not unresolved and task.status != "paused": transition(db, task, "queued", "human answered")
    return a

def limits(project):
    return {**DEFAULT_LIMITS, **project.config.get("limits", {})}

def can_run(db, task):
    project = get(db, Project, task.project_id); cap = limits(project)
    from . import policy
    if task.ended_by == "human" or (task.mandate and not policy.revalidate(project, task.mandate)):
        return False
    if task.tokens >= cap["tokens"] or task.active_seconds >= cap["task_seconds"]:
        ask(db, task, "任务已达到累计预算，需要调整预算还是停止？", "先审阅现有结果", "恢复不会重置累计消耗")
        return False
    if db.scalar(select(Effect.id).where(Effect.task_id == task.id, Effect.status == "running",
                                        Effect.kind.in_(["verify", "publish"]))):
        return False
    for dep in db.scalars(select(Dependency).where(Dependency.task_id == task.id)):
        if get(db, Task, dep.prerequisite_id).status != "completed": return False
    # Conservative default: one running implementation per project, avoiding unknown file overlap.
    other = db.scalar(select(Task.id).where(Task.project_id == task.project_id, Task.status == "running",
                                           Task.id != task.id, Task.lease_until > time.time()))
    return other is None

def claim_task(db, task_id, owner):
    task = lock_task(db, task_id)
    project = get(db, Project, task.project_id)
    if project.paused or task.status != "queued" or not can_run(db, task): return None
    token = secrets.token_urlsafe(32)
    a = Attempt(task_id=task.id, session_id=task.session_id, owner=owner,
                runner_token_hash=hashlib.sha256(token.encode()).hexdigest())
    db.add(a); db.flush()
    task.lease_owner = a.id; task.lease_until = time.time() + 90
    transition(db, task, "running")
    return a, token

def active_attempt(db, attempt_id, token=None):
    a = get(db, Attempt, attempt_id); task = lock_task(db, a.task_id)
    if a.status != "running" or task.status != "running" or task.lease_owner != a.id or task.lease_until <= time.time():
        raise Conflict("execution lease is no longer valid")
    if token is not None and not secrets.compare_digest(a.runner_token_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise Conflict("invalid runner capability")
    from . import policy
    project = get(db, Project, task.project_id)
    if task.ended_by == "human" or (task.mandate and not policy.revalidate(project, task.mandate)):
        raise Conflict("mandate revoked")
    return a, task

def heartbeat(db, attempt_id):
    a, task = active_attempt(db, attempt_id)
    now = time.time()
    task.active_seconds += max(0, now - a.last_heartbeat)
    a.last_heartbeat = now; task.lease_until = now + 90
    return task

def report(db, attempt_id, data, event_id):
    a, task = active_attempt(db, attempt_id)
    key = f"runner:{attempt_id}:{event_id}"
    if db.scalar(select(Event.id).where(Event.dedupe_key == key)): return
    kind = data["kind"]
    event(db, task.project_id, f"execution.{kind}", data, task.id, key, a.id)
    router_state.notice(db, task.project_id, "worker_report",
                        {"task_id": task.id, "attempt_id": a.id, "report": kind,
                         "summary": str(data.get("summary") or data.get("reason")
                                        or data.get("question") or "")[:400]})
    if kind == "progress":
        task.report_state = "progress"
        db.add(Artifact(task_id=task.id, attempt_id=a.id, kind="progress", sha=task.head_sha, payload=data))
        if data.get("scope_expansion"):
            enqueue(db, task.project_id, "scope_expansion", data, task.id, f"scope:{key}")
    elif kind == "question":
        task.report_state = "waiting"
        ask(db, task, data["question"], data.get("recommendation", ""), data.get("impact", ""))
    elif kind == "waiting":
        task.report_state = "waiting"
        transition(db, task, "waiting_external", data["reason"]); revoke(db, task, "external wait")
    elif kind == "ready":
        from . import ownership, tickets
        ownership.process_read(db, task)
        from . import specs
        specs.applied(db, task)
        # A runner assertion is evidence to inspect, never a verified test result.
        task.report_state = "ready"
        db.add(Artifact(task_id=task.id, attempt_id=a.id, kind="proposal", sha=data.get("sha"), payload=data))
        transition(db, task, "waiting_external", "host verification")
        revoke(db, task, "proposal submitted")
        if task.scope == "analysis":
            task.wait_reason = "supervision"
            effect(db, task.project_id, "final_review", f"analysis-review:{a.id}:{event_id}", {}, task.id)
        else:
            effect(db, task.project_id, "verify", f"verify:{a.id}:{event_id}", {"attempt_id": a.id}, task.id)
        tickets.settle_closed(db, task)
    elif kind == "blocked":
        # A blocked worker stops automatic input until a person's message arrives; its tools stay available.
        task.report_state = "blocked"
        transition(db, task, "waiting_external", "blocked: " + str(data.get("reason", "")))
        revoke(db, task, "blocked")
    elif kind == "failed":
        task.report_state = "failed"
        transition(db, task, "failed", "executor failed")
        revoke(db, task, "executor failed")
    elif kind == "dropped":
        from . import tickets
        task.report_state = "dropped"
        tickets.end(db, task, by="executor", outcome="dropped", reason=str(data.get("reason", "")))
    elif kind == "handback":
        # The worker says a message asked for more than this ticket; let it reach a fresh Eval.
        from . import tickets
        if data.get("trigger_id"):
            tickets.handback(db, task, data["trigger_id"], str(data.get("reason", "")))
    elif kind == "tool_failure":
        enqueue(db, task.project_id, "tool_failure", data, task.id, f"failure:{key}")
    elif kind == "ended":
        task.report_state = "failed"
        ask(db, task, "执行已结束但没有提交结构化结果，是否继续？", "检查执行记录后重试")
    else: raise Conflict("unknown report kind")
