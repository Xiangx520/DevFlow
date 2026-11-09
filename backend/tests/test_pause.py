"""Project-level pause stops dispatch and new Evals while Sources keep recording."""
import time
from fastapi.testclient import TestClient
from sqlalchemy import select
from devflow import core, policy, triggers
from devflow.api import app_factory
from devflow.db import transaction
from devflow.models import Effect, Project, Task, Thread
from devflow.worker import schedule_one


def client():
    return TestClient(app_factory(), headers={"Authorization": "Bearer owner-test-token"})


async def test_paused_project_does_not_dispatch_or_evaluate(project):
    with transaction() as db:
        project_row = db.get(Project, project)
        project_row.paused = True
        core.create_task(db, project, "paused", "paused", [])
        value = policy.normalize(project_row.policy)
        value["source_scopes"]["feishu"]["chats"]["paused"] = {"receive": "all"}
        project_row.policy = policy.normalize(value)
        project_row.policy_version = policy.version(project_row.policy)
        triggers.receive(db, project_row, {
            "source": "feishu", "kind": "feishu.message", "actor": "owner", "human": True,
            "addressed": None, "thread_keys": ["feishu:chat:paused"], "text": "discuss this",
            "dedupe_key": "paused-eval", "channel": {"chat_id": "paused"}, "at": time.time()})
        thread = db.get(Thread, f"{project}:feishu:chat:paused")
        assert thread.pending
        assert triggers.evaluate_due(db, project_row) == []
        assert not db.scalars(select(Effect).where(Effect.kind == "assessment")).all()
    assert await schedule_one() is None
    with transaction() as db:
        assert db.get(Task, db.scalar(select(Task.id))).status == "queued"


def test_pause_endpoint_and_health(project):
    c = client()
    paused = c.post(f"/api/v1/projects/{project}/pause", json={"paused": True})
    assert paused.status_code == 200 and paused.json()["paused"] is True
    health = c.get(f"/api/v1/projects/{project}/health").json()
    assert health["paused"] is True and health["open_tickets"] == 0
    resumed = c.post(f"/api/v1/projects/{project}/pause", json={"paused": False})
    assert resumed.json()["paused"] is False
