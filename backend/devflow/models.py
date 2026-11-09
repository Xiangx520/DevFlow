import time
import uuid
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def uid(): return str(uuid.uuid4())

class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

class Project(Entity, Base):
    __tablename__ = "projects"
    repository: Mapped[str] = mapped_column(String(240), unique=True)
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    last_poll: Mapped[float] = mapped_column(Float, default=0)

class Task(Entity, Base):
    __tablename__ = "tasks"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    goal: Mapped[str] = mapped_column(Text)
    acceptance: Mapped[list] = mapped_column(JSON, default=list)
    scope: Mapped[str] = mapped_column(String(30), default="implement")
    authorization: Mapped[dict] = mapped_column(JSON, default=dict)
    authorization_version: Mapped[int] = mapped_column(Integer, default=1)
    mandate: Mapped[dict] = mapped_column(JSON, default=dict)
    follows: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    issue_number: Mapped[int | None] = mapped_column(Integer)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    branch: Mapped[str] = mapped_column(String(200), default="")
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(36))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    ci_repairs: Mapped[int] = mapped_column(Integer, default=0)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    report_state: Mapped[str] = mapped_column(String(30), default="")
    wait_reason: Mapped[str] = mapped_column(Text, default="")
    waiting_since: Mapped[float | None] = mapped_column(Float)
    input_version: Mapped[int] = mapped_column(Integer, default=0)
    ended_by: Mapped[str | None] = mapped_column(String(30))
    outcome: Mapped[str | None] = mapped_column(String(30))
    ended_at: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (UniqueConstraint("project_id", "branch"),)

class Dependency(Base):
    __tablename__ = "dependencies"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    prerequisite_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)

class ExecutionSession(Entity, Base):
    __tablename__ = "execution_sessions"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    predecessor_id: Mapped[str | None] = mapped_column(String(36))
    handoff: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(30), default="active")

class Attempt(Entity, Base):
    __tablename__ = "attempts"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("execution_sessions.id"))
    owner: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="running")
    runner_token_hash: Mapped[str] = mapped_column(String(64))
    last_heartbeat: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float)
    container_id: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, default="")
    report_state: Mapped[str] = mapped_column(String(30), default="")
    usage: Mapped[int] = mapped_column(Integer, default=0)

class Event(Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    cause_id: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(500), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

class Inbox(Entity, Base):
    __tablename__ = "inbox"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(500), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)

class Trigger(Base):
    """One immutable event from a Source. Accepting it twice creates at most one task."""
    __tablename__ = "triggers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source: Mapped[str] = mapped_column(String(30), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(200), default="")
    dedupe_key: Mapped[str] = mapped_column(String(500), unique=True)
    actor: Mapped[str] = mapped_column(String(200), default="")
    human: Mapped[bool] = mapped_column(Boolean, default=True)
    addressed: Mapped[str | None] = mapped_column(String(30))
    thread_keys: Mapped[list] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    channel: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Float, default=time.time)
    disposition: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Thread(Base):
    """Triggers in time order. Evals run one at a time; subscriptions route later messages to tasks."""
    __tablename__ = "threads"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source: Mapped[str] = mapped_column(String(30), default="")
    pending: Mapped[list] = mapped_column(JSON, default=list)
    evaluating: Mapped[dict] = mapped_column(JSON, default=dict)
    subscriptions: Mapped[list] = mapped_column(JSON, default=list)
    context: Mapped[list] = mapped_column(JSON, default=list)
    expired: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class TaskInput(Entity, Base):
    __tablename__ = "task_inputs"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="text")
    trigger_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    consumed_by: Mapped[str | None] = mapped_column(String(36))
    consumed_at: Mapped[float | None] = mapped_column(Float)
    read_at: Mapped[float | None] = mapped_column(Float)

class MessageOwnership(Base):
    """Per-recipient lifecycle; transport/read receipts never imply processing."""
    __tablename__ = "message_ownership"
    trigger_id: Mapped[str] = mapped_column(ForeignKey("triggers.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    delivered_at: Mapped[float] = mapped_column(Float, default=time.time)
    read_at: Mapped[float | None] = mapped_column(Float)
    processed_at: Mapped[float | None] = mapped_column(Float)
    released_at: Mapped[float | None] = mapped_column(Float)
    release_reason: Mapped[str | None] = mapped_column(Text)


class Decision(Entity, Base):
    __tablename__ = "decisions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    trigger_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30), default="router", index=True)
    thread_key: Mapped[str | None] = mapped_column(String(300), index=True)
    trigger_ids: Mapped[list] = mapped_column(JSON, default=list)
    strength: Mapped[str | None] = mapped_column(String(30))
    mandate: Mapped[dict] = mapped_column(JSON, default=dict)
    audit: Mapped[dict] = mapped_column(JSON, default=dict)
    action: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text, default="")

class Attention(Entity, Base):
    __tablename__ = "attentions"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[float | None] = mapped_column(Float)

class Finding(Entity, Base):
    __tablename__ = "findings"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    evidence_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(80))
    evidence: Mapped[dict] = mapped_column(JSON)
    recommendation: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

class Artifact(Entity, Base):
    __tablename__ = "artifacts"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(80))
    sha: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)

class Effect(Entity, Base):
    __tablename__ = "effects"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(500), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    tries: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)

class Conversation(Entity, Base):
    __tablename__ = "conversations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

class Message(Entity, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)

class Receipt(Base):
    __tablename__ = "receipts"
    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)

def view(entity):
    return {column.name: getattr(entity, column.name) for column in entity.__table__.columns}


class FeishuMessage(Base):
    """Raw inbox; unbound messages cannot authorize development."""
    __tablename__ = "feishu_messages"
    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(200), index=True)
    sender_id: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="unbound")
    received_at: Mapped[float] = mapped_column(Float, default=time.time)


class FeishuBinding(Base):
    __tablename__ = "feishu_bindings"
    app_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    bot_open_id: Mapped[str] = mapped_column(String(200))
    allowed_open_ids: Mapped[list] = mapped_column(JSON)


class Control(Entity, Base):
    __tablename__ = "controls"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    input_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[float | None] = mapped_column(Float)
    applied_at: Mapped[float | None] = mapped_column(Float)


class PolicyRevision(Base):
    __tablename__ = "policy_revisions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class SpecRevision(Entity, Base):
    __tablename__ = "spec_revisions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("project_id", "version"),)


class SpecReceipt(Base):
    __tablename__ = "spec_receipts"
    spec_id: Mapped[str] = mapped_column(ForeignKey("spec_revisions.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    delivered_at: Mapped[float] = mapped_column(Float, default=time.time)
    read_at: Mapped[float | None] = mapped_column(Float)
    applied_at: Mapped[float | None] = mapped_column(Float)
    verified_at: Mapped[float | None] = mapped_column(Float)


class ReviewRecord(Entity, Base):
    __tablename__ = "review_records"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    sha: Mapped[str | None] = mapped_column(String(64))
    aligned: Mapped[bool] = mapped_column(Boolean)
    snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class RouterState(Base):
    """Project-level Router memory: durable summary, recent decisions and pending notices."""
    __tablename__ = "router_state"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    memory: Mapped[list] = mapped_column(JSON, default=list)
    notices: Mapped[list] = mapped_column(JSON, default=list)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class WatchdogInstruction(Entity, Base):
    """A maintainer instruction the independent Watchdog reads on its next review/query."""
    __tablename__ = "watchdog_instructions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(40), default="owner")


class SourceCursor(Base):
    __tablename__ = "source_cursors"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    scope: Mapped[str] = mapped_column(String(240), primary_key=True)
    position: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


# Revisions are append-only even when an application caller holds an ORM object.
from sqlalchemy import event as orm_event, inspect as orm_inspect
from sqlalchemy.orm import Session as OrmSession

@orm_event.listens_for(OrmSession, "before_flush")
def immutable_revisions(session, context, instances):
    for row in session.dirty | session.deleted:
        if isinstance(row, (PolicyRevision, SpecRevision)) and orm_inspect(row).persistent:
            if row in session.deleted or session.is_modified(row, include_collections=True):
                raise ValueError("policy and specification revisions are immutable")
