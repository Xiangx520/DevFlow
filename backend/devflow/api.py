"""Versioned public API and narrowly scoped runner capabilities."""
import asyncio
import hashlib
import json
import re
import secrets
import time
from typing import Literal
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from . import core, health, policy as policy_module, router, router_state, steering, tickets, triggers, watcher
from .adapters import GitHub, redact
from .config import settings
from .db import transaction
from .github_events import intake, signature
from .models import (Artifact, Attention, Attempt, Conversation, Decision, Dependency, Effect,
                     Event, ExecutionSession, Finding, Inbox, Message, Project, Receipt, Task,
                     Thread, Trigger, TaskInput, MessageOwnership, view)

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
class Policy(Strict):
    allowed_actors: list[str] = Field(min_length=1)
    agent_login: str = ""
    mode: str = "observe"
    bots: list[str] = []
    rules: list[dict] = []
    admission: dict = {}
    source_scopes: dict = {}
    source_allowed: dict = {}
class PolicyPatch(Strict):
    allowed_actors: list[str] | None = None
    agent_login: str | None = None
    mode: str | None = None
    bots: list[str] | None = None
    rules: list[dict] | None = None
    admission: dict | None = None
    source_scopes: dict | None = None
    source_allowed: dict | None = None
class ExecutionConfig(Strict):
    setup_commands: list[str] = []
    verify_commands: list[str] = Field(min_length=1)
    notify_feishu: bool = True
    limits: dict[str, int] = Field(default_factory=lambda: dict(core.DEFAULT_LIMITS))
    eval_timeout_seconds: int = 120
    discussion_debounce_seconds: int = 20
class ProjectCreate(Strict):
    repository: str
    default_branch: str = "main"
    policy: Policy
    config: ExecutionConfig
class ProjectPatch(Strict):
    policy: PolicyPatch | None = None
    config: ExecutionConfig | None = None
    paused: bool | None = None
class PauseRequest(Strict):
    paused: bool
class TaskCreate(Strict):
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=30000)
    acceptance: list[str] = []
    scope: str = "implement"
    issue_number: int | None = Field(default=None, gt=0)
    priority: int = 0
class TaskCommand(Strict):
    action: str
    priority: int | None = None
    authorization: dict | None = None
class Input(Strict):
    text: str = Field(min_length=1, max_length=30000)
class TriggerRoute(Strict):
    kind: Literal["accept", "attach", "stop", "ignore"]
    task_id: str | None = None
    title: str | None = None
    goal: str | None = None
    acceptance: list[str] = []
    scope: str = "implement"
    reason: str = ""
class PolicyUpdate(Strict):
    allowed_actors: list[str] | None = None
    agent_login: str | None = None
    mode: str | None = None
    bots: list[str] | None = None
    rules: list[dict] | None = None
    admission: dict | None = None
    source_scopes: dict | None = None
    source_allowed: dict | None = None
class SpecUpdate(Strict):
    text: str = Field(min_length=1, max_length=30000)
    version: int | None = Field(default=None, gt=0)
    task_ids: list[str] | None = None
class SteerRequest(Strict):
    kind: str = "note"
    text: str = Field(min_length=1, max_length=30000)
    version: int | None = Field(default=None, gt=0)
class FeishuBind(Strict):
    project_id: str
    chat_id: str = Field(min_length=1, max_length=200)
    bot_open_id: str = Field(min_length=1, max_length=200)
    allowed_open_ids: list[str] = Field(min_length=1)
class MessageCreate(Input):
    task_id: str | None = None
    scope: str = "implement"
class ConversationCreate(Strict):
    project_id: str
class DependencyCreate(Strict):
    prerequisite_id: str
class WatchdogQuestion(Strict):
    text: str = Field(min_length=1, max_length=8000)
class WatchdogInstructionCreate(Strict):
    text: str = Field(min_length=1, max_length=4000)
class ThreadReference(Strict):
    thread_key: str = Field(min_length=1, max_length=300)
    reason: str = ""

def auth(authorization: str | None = Header(default=None)):
    token = settings().api_token
    if not token or not secrets.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "valid owner token required")

def idempotent(db, key, payload, execute):
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    old = db.get(Receipt, key)
    if old:
        if old.fingerprint != fingerprint: raise core.Conflict("idempotency key reused with different request")
        return old.result
    result = execute()
    db.flush()
    db.add(Receipt(key=key, fingerprint=fingerprint, result=result))
    return result

def project_view(db, project):
    state = router_state.get(db, project.id)
    return {**view(project), "router_summary": router_state.summary(db, project.id),
            "router_until": (state.lease_until if state else 0)}


def structured_task(db, body):
    project = core.get(db, Project, body.project_id)
    data = body.model_dump()
    data.pop("project_id", None)
    return router.accept_owner_request(db, project, **data)


def controlled_command(db, task_id, body):
    from . import control
    task = core.get(db, Task, task_id)
    row = control.submit(db, task, "stop" if body.action == "cancel" else body.action,
                         {"priority": body.priority, "authorization": body.authorization})
    return {**view(task), "control_id": row.id}


def app_factory():
    app = FastAPI(title="DevFlow Recode", version="0.1.0")
    @app.exception_handler(core.Conflict)
    async def conflict(_request, exc): return JSONResponse({"detail": str(exc)}, status_code=409)
    @app.exception_handler(core.Missing)
    async def missing(_request, exc): return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/health/live")
    def live(): return {"status": "alive"}
    @app.get("/health/ready")
    def ready():
        with transaction() as db: db.execute(select(Project.id).limit(1))
        return {"database": "ready"}

    public = [Depends(auth)]
    @app.post("/api/v1/projects", dependencies=public, status_code=201)
    def projects_create(body: ProjectCreate):
        if body.config.limits.keys() - core.DEFAULT_LIMITS.keys() or any(v <= 0 for v in body.config.limits.values()):
            raise core.Conflict("invalid limits")
        with transaction() as db:
            if db.scalar(select(Project.id).where(Project.repository == body.repository)):
                raise core.Conflict("repository already connected")
            return project_view(db, core.new_project(db, body.repository, body.policy.model_dump(), body.config.model_dump(), body.default_branch))
    @app.get("/api/v1/projects", dependencies=public)
    def projects_list():
        with transaction() as db: return [project_view(db, x) for x in db.scalars(select(Project))]
    @app.get("/api/v1/projects/{project_id}", dependencies=public)
    def project_get(project_id: str):
        with transaction() as db: return project_view(db, core.get(db, Project, project_id))
    @app.patch("/api/v1/projects/{project_id}", dependencies=public)
    def project_patch(project_id: str, body: ProjectPatch):
        with transaction() as db:
            p = core.lock_project(db, project_id)
            old_policy = dict(p.policy)
            if body.policy:
                try:
                    merged = policy_module.normalize(p.policy)
                    merged.update({key: value for key, value in body.policy.model_dump(exclude_none=True).items()})
                    p.policy = policy_module.normalize(merged)
                except ValueError as exc:
                    raise core.Conflict(str(exc))
            if body.config:
                if body.config.limits.keys() - core.DEFAULT_LIMITS.keys() or any(v <= 0 for v in body.config.limits.values()):
                    raise core.Conflict("invalid limits")
                p.config = body.config.model_dump()
            if body.paused is not None:
                p.paused = body.paused
                core.event(db, p.id, "project.pause", {"paused": p.paused})
            if body.policy:
                version, delivered, requeued = steering.policy_changed(db, p, old_policy)
                return {**project_view(db, p), "policy_change": {"version": version, "tickets": delivered,
                                                                  "requeued_evals": requeued}}
            core.event(db, p.id, "project.updated", {})
            return project_view(db, p)
    @app.get("/api/v1/projects/{project_id}/candidates", dependencies=public)
    def candidates(project_id: str):
        with transaction() as db:
            core.get(db, Project, project_id)
            return [view(x) for x in db.scalars(select(Inbox).where(Inbox.project_id == project_id, Inbox.kind == "observed").limit(100))]

    @app.get("/api/v1/projects/{project_id}/triggers", dependencies=public)
    def project_triggers(project_id: str, status: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        with transaction() as db:
            core.get(db, Project, project_id)
            rows = list(db.scalars(select(Trigger).where(Trigger.project_id == project_id)
                                   .order_by(Trigger.created_at.desc()).limit(limit)))
            if status == "decided":
                rows = [row for row in rows if (row.disposition or {}).get("decision")]
            elif status == "undecided":
                rows = [row for row in rows if not (row.disposition or {}).get("decision")]
            elif status == "filtered":
                rows = [row for row in rows if (row.disposition or {}).get("filtered")]
            return [view(row) for row in rows]

    @app.get("/api/v1/triggers/{trigger_id}", dependencies=public)
    def trigger_get(trigger_id: str):
        with transaction() as db:
            return view(core.get(db, Trigger, trigger_id))

    @app.post("/api/v1/triggers/{trigger_id}/requeue", dependencies=public)
    def trigger_requeue(trigger_id: str):
        with transaction() as db:
            trigger = core.get(db, Trigger, trigger_id)
            project = core.get(db, Project, trigger.project_id)
            return {"requeued": triggers.requeue(db, project, trigger_id)}

    @app.post("/api/v1/triggers/{trigger_id}/route", dependencies=public)
    def trigger_route(trigger_id: str, body: TriggerRoute):
        with transaction() as db:
            trigger = core.get(db, Trigger, trigger_id)
            project = core.lock_project(db, trigger.project_id)
            thread = db.get(Thread, (trigger.thread_keys or [""])[0])
            item = router.RouteItem(trigger_id=trigger_id, kind=body.kind, task_id=body.task_id,
                                    title=body.title, goal=body.goal, acceptance=body.acceptance,
                                    scope=body.scope, reason=body.reason)
            result = router.manual_route(db, project, trigger, item)
            return {"ticket_id": result.id if result is not None else None}

    @app.get("/api/v1/projects/{project_id}/threads", dependencies=public)
    def project_threads(project_id: str, limit: int = Query(default=100, ge=1, le=500)):
        with transaction() as db:
            core.get(db, Project, project_id)
            return [view(row) for row in db.scalars(select(Thread).where(Thread.project_id == project_id)
                                                    .order_by(Thread.updated_at.desc()).limit(limit))]

    @app.get("/api/v1/threads/{key:path}", dependencies=public)
    def thread_get(key: str):
        with transaction() as db:
            thread = core.get(db, Thread, key)
            pending = [view(core.get(db, Trigger, value)) for value in (thread.pending or [])
                       if db.get(Trigger, value)]
            return {**view(thread), "pending_triggers": pending}

    @app.get("/api/v1/projects/{project_id}/evals", dependencies=public)
    def project_evals(project_id: str, limit: int = Query(default=100, ge=1, le=500)):
        with transaction() as db:
            core.get(db, Project, project_id)
            return [view(row) for row in db.scalars(
                select(Decision).where(Decision.project_id == project_id,
                                       Decision.kind.in_(["accept", "ignore", "stop", "unavailable"]))
                .order_by(Decision.created_at.desc()).limit(limit))]

    @app.post("/api/v1/projects/{project_id}/pause", dependencies=public)
    def project_pause(project_id: str, body: PauseRequest):
        with transaction() as db:
            project = core.lock_project(db, project_id)
            project.paused = body.paused
            core.event(db, project.id, "project.pause", {"paused": project.paused},
                       None, "pause:" + project.id + ":" + str(int(time.time())))
            return {"paused": project.paused}

    @app.get("/api/v1/projects/{project_id}/health", dependencies=public)
    def project_health(project_id: str):
        with transaction() as db:
            return health.summary(db, project_id)

    @app.get("/api/v1/projects/{project_id}/router", dependencies=public)
    def router_memory(project_id: str):
        with transaction() as db:
            core.get(db, Project, project_id)
            return {**router_state.snapshot(db, project_id),
                    "summary": router_state.summary(db, project_id)}

    @app.get("/api/v1/projects/{project_id}/watchdog", dependencies=public)
    def watchdog_state(project_id: str):
        with transaction() as db:
            return watcher.state(db, project_id)

    @app.post("/api/v1/projects/{project_id}/watchdog/ask", dependencies=public)
    async def watchdog_ask(project_id: str, body: WatchdogQuestion):
        with transaction() as db:
            state = watcher.state(db, project_id)
        try:
            answer = await watcher.ask(state, body.text)
        except Exception as exc:
            raise HTTPException(503, "watchdog model unavailable: " + redact(exc)[:200])
        return {"answer": answer}

    @app.post("/api/v1/projects/{project_id}/watchdog/instructions", dependencies=public, status_code=201)
    def watchdog_instruction(project_id: str, body: WatchdogInstructionCreate):
        with transaction() as db:
            project = core.lock_project(db, project_id)
            return view(watcher.add_instruction(db, project, body.text))

    @app.post("/api/v1/projects/{project_id}/watchdog/instructions/{instruction_id}/retire", dependencies=public)
    def watchdog_retire(project_id: str, instruction_id: str):
        with transaction() as db:
            project = core.lock_project(db, project_id)
            return view(watcher.retire_instruction(db, project, instruction_id))

    @app.post("/api/v1/projects/{project_id}/policy", dependencies=public)
    def project_policy(project_id: str, body: PolicyUpdate):
        with transaction() as db:
            project = core.lock_project(db, project_id)
            old_policy = dict(project.policy)
            try:
                merged = policy_module.normalize(project.policy)
                merged.update({key: value for key, value in body.model_dump(exclude_none=True).items()})
                project.policy = policy_module.normalize(merged)
            except ValueError as exc:
                raise core.Conflict(str(exc))
            version, delivered, requeued = steering.policy_changed(db, project, old_policy)
            return {"policy_version": version, "tickets": delivered, "requeued_evals": requeued}

    @app.post("/api/v1/projects/{project_id}/spec", dependencies=public)
    def project_spec(project_id: str, body: SpecUpdate):
        with transaction() as db:
            project = core.lock_project(db, project_id)
            version, delivered = steering.spec_update(db, project, body.text, body.version,
                                                      body.task_ids)
            return {"spec_version": version, "delivered": delivered}

    @app.post("/api/v1/feishu/bind", dependencies=public, status_code=201)
    def feishu_bind(body: FeishuBind):
        from .feishu_receiver import bind_group
        with transaction() as db:
            binding = bind_group(db, body.project_id, settings().feishu_app_id, body.chat_id,
                                 body.bot_open_id, body.allowed_open_ids)
            return {"project_id": binding.project_id, "chat_id": binding.chat_id,
                    "conversation_id": binding.conversation_id}
    @app.get("/api/v1/projects/{project_id}/decisions", dependencies=public)
    def decisions(project_id: str):
        with transaction() as db:
            return [view(x) for x in db.scalars(select(Decision).where(Decision.project_id == project_id)
                                               .order_by(Decision.created_at.desc()).limit(100))]
    @app.post("/api/v1/tasks", dependencies=public, status_code=201)
    def task_create(body: TaskCreate, idempotency_key: str = Header(min_length=1, max_length=100)):
        with transaction() as db:
            core.lock_project(db, body.project_id)
            return idempotent(db, f"task:{body.project_id}:{idempotency_key}", body.model_dump(),
                lambda: structured_task(db, body))
    @app.get("/api/v1/tasks", dependencies=public)
    def tasks(project_id: str | None = None, status: str | None = None):
        with transaction() as db:
            query = select(Task).order_by(Task.priority.desc(), Task.created_at)
            if project_id: query = query.where(Task.project_id == project_id)
            if status: query = query.where(Task.status == status)
            return [view(x) for x in db.scalars(query.limit(200))]
    @app.get("/api/v1/tasks/{task_id}", dependencies=public)
    def task_get(task_id: str):
        with transaction() as db:
            t = core.get(db, Task, task_id)
            return {**view(t), "dependencies": [x.prerequisite_id for x in db.scalars(
                select(Dependency).where(Dependency.task_id == task_id))]}
    @app.post("/api/v1/tasks/{task_id}/commands", dependencies=public)
    def task_command(task_id: str, body: TaskCommand, idempotency_key: str = Header(min_length=1, max_length=100)):
        with transaction() as db:
            core.lock_task(db, task_id)
            return idempotent(db, f"command:{task_id}:{idempotency_key}", body.model_dump(),
                              lambda: controlled_command(db, task_id, body))
    @app.post("/api/v1/tasks/{task_id}/dependencies", dependencies=public)
    def dependency(task_id: str, body: DependencyCreate):
        with transaction() as db:
            core.add_dependency(db, task_id, body.prerequisite_id)
            return {"added": True}
    @app.post("/api/v1/tasks/{task_id}/inputs", dependencies=public)
    def task_input(task_id: str, body: Input, idempotency_key: str = Header(min_length=1, max_length=100)):
        with transaction() as db:
            task = core.lock_task(db, task_id)
            def submit():
                trigger = triggers.api_request(db, core.get(db, Project, task.project_id), body.text,
                                               task=task, identity="input:" + task_id + ":" + idempotency_key)
                return {"accepted": True, "input_id": trigger.id, "trigger_id": trigger.id}
            return idempotent(db, f"input:{task_id}:{idempotency_key}", body.model_dump(), submit)
    @app.get("/api/v1/tasks/{task_id}/stage", dependencies=public)
    def task_stage(task_id: str):
        with transaction() as db:
            task = core.get(db, Task, task_id)
            return {"task_id": task.id, "stage": tickets.stage(task), "status": task.status,
                    "report_state": task.report_state, "spec_version": task.spec_version,
                    "mandate": task.mandate}
    @app.post("/api/v1/tasks/{task_id}/steer", dependencies=public)
    def task_steer(task_id: str, body: SteerRequest):
        with transaction() as db:
            task = core.lock_task(db, task_id)
            row = steering.steer(db, task, body.text, kind=body.kind, version=body.version,
                                 source="owner", require_ack=body.kind == "spec_update")
            return {"input_id": row.id, "kind": row.kind}
    @app.get("/api/v1/tasks/{task_id}/{resource}", dependencies=public)
    async def task_resources(task_id: str, resource: str, request: Request, after: int = 0, stream: bool = False):
        if resource == "events":
            return await event_response(request, task_id=task_id, after=after, stream=stream)
        models = {"sessions": ExecutionSession, "attempts": Attempt, "findings": Finding,
                  "attentions": Attention, "artifacts": Artifact, "effects": Effect}
        if resource not in models: raise HTTPException(404)
        model = models[resource]
        with transaction() as db:
            core.get(db, Task, task_id)
            values = [view(x) for x in db.scalars(select(model).where(model.task_id == task_id).order_by(model.created_at).limit(200))]
            for value in values: value.pop("runner_token_hash", None)
            return values
    @app.post("/api/v1/tasks/{task_id}/effects/{effect_id}/retry", dependencies=public)
    def effect_retry(task_id: str, effect_id: str):
        with transaction() as db:
            task = core.lock_task(db, task_id)
            e = core.get(db, Effect, effect_id)
            if e.task_id != task_id: raise core.Missing("effect not in task")
            if e.status != "failed": raise core.Conflict("only failed effects can be explicitly retried")
            e.status = "pending"; e.tries = 0; e.available_at = time.time()
            e.owner = None; e.lease_until = 0
            core.event(db, task.project_id, "effect.retry", {"effect_id": effect_id}, task.id)
            return view(e)

    @app.post("/api/v1/tasks/{task_id}/attentions/{attention_id}/answer", dependencies=public)
    def task_answer(task_id: str, attention_id: str, body: Input):
        with transaction() as db: return view(core.answer(db, task_id, attention_id, body.text))

    @app.get("/api/v1/projects/{project_id}/attentions", dependencies=public)
    def project_attentions(project_id: str, unresolved: bool = True):
        with transaction() as db:
            core.get(db, Project, project_id)
            query = select(Attention).join(Task, Attention.task_id == Task.id).where(Task.project_id == project_id)
            if unresolved: query = query.where(Attention.answer.is_(None))
            return [view(x) for x in db.scalars(query.order_by(Attention.created_at.desc()).limit(200))]

    @app.get("/api/v1/conversations", dependencies=public)
    def conversations_list(project_id: str):
        with transaction() as db:
            core.get(db, Project, project_id)
            return [view(x) for x in db.scalars(select(Conversation).where(Conversation.project_id == project_id)
                                               .order_by(Conversation.created_at.desc()).limit(200))]

    @app.post("/api/v1/conversations", dependencies=public, status_code=201)
    def conversation_create(body: ConversationCreate):
        with transaction() as db:
            core.get(db, Project, body.project_id)
            c = Conversation(project_id=body.project_id); db.add(c); db.flush(); return view(c)
    @app.post("/api/v1/conversations/{conversation_id}/messages", dependencies=public, status_code=202)
    def conversation_message(conversation_id: str, body: MessageCreate,
                             idempotency_key: str = Header(min_length=1, max_length=100)):
        with transaction() as db:
            c = core.get(db, Conversation, conversation_id); core.lock_project(db, c.project_id)
            if body.scope not in {"analysis", "implement"}: raise core.Conflict("invalid scope")
            if body.task_id and core.get(db, Task, body.task_id).project_id != c.project_id:
                raise core.Conflict("task belongs to another project")
            def submit():
                m = Message(conversation_id=c.id, task_id=body.task_id, role="user", content=body.text)
                db.add(m); db.flush()
                trigger = triggers.api_request(db, core.get(db, Project, c.project_id), body.text,
                    conversation_id=c.id, task=db.get(Task, body.task_id) if body.task_id else None,
                    scope=body.scope, identity=m.id)
                core.event(db, c.project_id, "conversation.message", {"conversation_id": c.id,
                    "message_id": m.id, "role": "user", "content": body.text}, body.task_id)
                return {"message_id": m.id, "job_id": trigger.id, "trigger_id": trigger.id}
            return idempotent(db, f"message:{c.id}:{idempotency_key}", body.model_dump(), submit)
    @app.get("/api/v1/conversations/{conversation_id}/messages", dependencies=public)
    def conversation_messages(conversation_id: str, before: float | None = None, before_id: str | None = None, limit: int = Query(default=100, ge=1, le=200)):
        with transaction() as db:
            core.get(db, Conversation, conversation_id)
            query = select(Message).where(Message.conversation_id == conversation_id)
            if before_id:
                cursor = core.get(db, Message, before_id)
                if cursor.conversation_id != conversation_id: raise core.Conflict("message belongs to another conversation")
                query = query.where(or_(Message.created_at < cursor.created_at,
                    and_(Message.created_at == cursor.created_at, Message.id < cursor.id)))
            elif before is not None: query = query.where(Message.created_at < before)
            return list(reversed([view(x) for x in db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))]))

    async def event_response(request, *, task_id=None, project_id=None, conversation_id=None, after=0, stream=False):
        try: after = max(after, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError: raise HTTPException(422, "invalid Last-Event-ID")
        def batch(cursor):
            with transaction() as db:
                query = select(Event).where(Event.sequence > cursor)
                if task_id: query = query.where(Event.task_id == task_id)
                if project_id: query = query.where(Event.project_id == project_id)
                if conversation_id:
                    query = query.where(Event.payload["conversation_id"].as_string() == conversation_id)
                rows = [view(e) for e in db.scalars(query.order_by(Event.sequence).limit(200))]
                return rows
        if not stream: return batch(after)
        async def generate():
            cursor = after
            while not await request.is_disconnected():
                rows = batch(cursor)
                for row in rows:
                    cursor = row["sequence"]
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if not rows: yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                  headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # Register concrete event endpoints before generic resource route matching.
    @app.get("/api/v1/events", dependencies=public)
    async def events(request: Request, project_id: str, after: int = 0, stream: bool = False):
        return await event_response(request, project_id=project_id, after=after, stream=stream)
    @app.get("/api/v1/conversations/{conversation_id}/events", dependencies=public)
    async def conversation_events(conversation_id: str, request: Request, after: int = 0, stream: bool = False):
        with transaction() as db: project_id = core.get(db, Conversation, conversation_id).project_id
        return await event_response(request, project_id=project_id, conversation_id=conversation_id, after=after, stream=stream)

    @app.post("/api/v1/webhooks/github", status_code=202)
    async def webhook(request: Request, x_github_event: str = Header(), x_github_delivery: str = Header(),
                      x_hub_signature_256: str = Header()):
        body = await request.body()
        if len(body) > 4 * 1024 * 1024: raise HTTPException(413)
        if not signature(body, x_hub_signature_256): raise HTTPException(401, "invalid signature")
        try: payload = json.loads(body)
        except ValueError: raise HTTPException(400, "invalid JSON")
        with transaction() as db: return intake(db, x_github_event, payload, x_github_delivery)

    def runner(attempt_id, authorization):
        token = (authorization or "").removeprefix("Bearer ")
        with transaction() as db:
            try:
                attempt, task = core.active_attempt(db, attempt_id, token)
                return task.id, task.project_id
            except core.Conflict: raise HTTPException(403, "expired or invalid runner capability")

    @app.post("/internal/attempts/{attempt_id}/threads/follow")
    def runner_follow(attempt_id: str, body: ThreadReference,
                      authorization: str | None = Header(default=None)):
        task_id, _ = runner(attempt_id, authorization)
        with transaction() as db:
            tickets.follow_for_context(db, core.get(db, Task, task_id), body.thread_key)
            return {"thread": body.thread_key, "context": True}

    @app.post("/internal/attempts/{attempt_id}/threads/unrelated")
    def runner_unrelated(attempt_id: str, body: ThreadReference,
                         authorization: str | None = Header(default=None)):
        task_id, _ = runner(attempt_id, authorization)
        with transaction() as db:
            dropped = tickets.mark_unrelated(db, core.get(db, Task, task_id),
                                             body.thread_key, body.reason)
            return {"thread": body.thread_key, "dropped": bool(dropped)}

    @app.post("/internal/attempts/{attempt_id}/control")
    def runner_control(attempt_id: str, authorization: str | None = Header(default=None)):
        task_id, _ = runner(attempt_id, authorization)
        with transaction() as db:
            task = core.get(db, Task, task_id); project = core.get(db, Project, task.project_id)
            budget = core.limits(project)
            a = core.get(db, Attempt, attempt_id)
            stop = task.tokens >= budget["tokens"] or task.active_seconds >= budget["task_seconds"] or time.time() - a.created_at >= budget["turn_seconds"]
            from sqlalchemy import exists
            pending = list(db.scalars(select(TaskInput).where(TaskInput.task_id == task.id,
                TaskInput.consumed_at.is_(None), ~exists(select(MessageOwnership.task_id).where(
                    MessageOwnership.task_id == TaskInput.task_id,
                    MessageOwnership.trigger_id == TaskInput.trigger_id,
                    MessageOwnership.released_at.is_not(None)))).order_by(TaskInput.created_at)))
            return {"stop": stop, "reason": "budget" if stop else "", "input_version": task.input_version,
                    "inputs": [{"id": row.id, "kind": row.kind, "payload": row.payload} for row in pending]}

    @app.post("/internal/attempts/{attempt_id}/model/chat/completions")
    async def model_proxy(attempt_id: str, request: Request, authorization: str | None = Header(default=None)):
        task_id, _ = runner(attempt_id, authorization)
        payload = await request.json()
        if len(json.dumps(payload)) > 2 * 1024 * 1024: raise HTTPException(413)
        reserved = min(max(int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 4096), 1), 8192)
        with transaction() as db:
            a, task = core.active_attempt(db, attempt_id, (authorization or "").removeprefix("Bearer "))
            project = core.get(db, Project, task.project_id)
            # Reserve input + output before network I/O, conservatively keeping reservation on unknown usage.
            reserve = reserved + len(json.dumps(payload.get("messages", [])))  # conservative upper-bound token estimate
            if task.tokens + reserve > core.limits(project)["tokens"]: raise HTTPException(429, "task token budget")
            task.tokens += reserve; a.usage += reserve
        if settings().llm_thinking:
            payload["thinking"] = {"type": settings().llm_thinking}
        payload["model"] = settings().llm_model
        payload["max_tokens"] = reserved
        payload.pop("max_completion_tokens", None)
        payload.pop("user", None)
        if payload.get("stream"): payload["stream_options"] = {"include_usage": True}
        if not settings().llm_api_key or not settings().llm_model: raise HTTPException(503, "model not configured")
        async def relay():
            usage = None; buffer = ""
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", settings().llm_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + settings().llm_api_key}, json=payload) as response:
                        if response.status_code >= 400:
                            yield b'data: {"error":{"message":"upstream model unavailable"}}\n\n'
                            return
                        async for chunk in response.aiter_bytes():
                            yield chunk
                            buffer += chunk.decode("utf-8", errors="replace")
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                                    try:
                                        item = json.loads(line[6:])
                                        if item.get("usage"): usage = item["usage"].get("total_tokens")
                                    except ValueError: pass
            finally:
                if usage is not None:
                    with transaction() as db:
                        task = core.lock_task(db, task_id)
                        task.tokens = max(0, task.tokens + int(usage) - reserve)
                        core.get(db, Attempt, attempt_id).usage += int(usage) - reserve
        if not payload.get("stream"): raise HTTPException(400, "runner protocol requires streaming")
        return StreamingResponse(relay(), media_type="text/event-stream")

    @app.get("/internal/attempts/{attempt_id}/github/actions/jobs/{job_id}/logs")
    async def runner_logs(attempt_id: str, job_id: int, authorization: str | None = Header(default=None)):
        _, project_id = runner(attempt_id, authorization)
        with transaction() as db: repository = core.get(db, Project, project_id).repository
        from urllib.parse import urlparse
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(settings().github_api_url + f"/repos/{repository}/actions/jobs/{job_id}/logs",
                headers={"Authorization": "Bearer " + settings().github_token})
            if response.status_code not in {200, 302}: raise HTTPException(502, "GitHub job logs unavailable")
            if response.status_code == 302:
                location = response.headers.get("location", "")
                parsed = urlparse(location)
                if parsed.scheme != "https" or not parsed.hostname or not any(
                    parsed.hostname.endswith(suffix) for suffix in (".blob.core.windows.net", ".githubusercontent.com", ".s3.amazonaws.com")):
                    raise HTTPException(502, "unsupported signed log origin")
                response = await client.get(location)
            if response.status_code != 200: raise HTTPException(502, "job log download failed")
            return {"job_id": job_id, "text": response.text[-60000:], "truncated": len(response.content) > 60000}

    @app.get("/internal/attempts/{attempt_id}/github/{path:path}")
    async def runner_github(attempt_id: str, path: str, page: int = Query(default=1,ge=1,le=1000),
                            authorization: str | None = Header(default=None)):
        _, project_id = runner(attempt_id, authorization)
        allowed = r"(issues/\d+|issues/\d+/comments|pulls/\d+|pulls/\d+/files|pulls/\d+/reviews|actions/runs/\d+/jobs)"
        if not re.fullmatch(allowed, path): raise HTTPException(403, "read endpoint not allowed")
        with transaction() as db: repository = core.get(db, Project, project_id).repository
        result = await GitHub().request("GET", f"/repos/{repository}/{path}", params={"page":page,"per_page":100})
        rows = result if isinstance(result,list) else result.get("jobs",[]) if isinstance(result,dict) else []
        return {"page":page,"per_page":100,"maybe_more":len(rows)==100,"data":result}
    return app

app = app_factory()
