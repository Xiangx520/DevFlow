"""Backend interfaces for the super-agent layer."""
import asyncio
import hashlib
import hmac
import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from devflow import github_events
from devflow.api import app_factory
from devflow.config import settings
from devflow.db import transaction
from devflow.models import Task, TaskInput, Trigger


def client():
    return TestClient(app_factory(), headers={"Authorization": "Bearer owner-test-token"})


def webhook(c, delivery, payload, event="issues"):
    body = json.dumps(payload)
    signature = "sha256=" + hmac.new(b"webhook-test-secret", body.encode(), hashlib.sha256).hexdigest()
    return c.post("/api/v1/webhooks/github", content=body,
                  headers={"X-Github-Event": event, "X-Github-Delivery": delivery,
                           "X-Hub-Signature-256": signature})


def test_webhook_trigger_thread_and_manual_route(project):
    c = client()
    payload = {"repository": {"full_name": "owner/repo"}, "action": "labeled",
               "label": {"name": "agent:run"},
               "issue": {"number": 5, "title": "Bug", "body": "Fix the login"},
               "sender": {"login": "owner"}}
    assert webhook(c, "api-1", payload).status_code == 202
    assert asyncio.run(github_events.process_one())

    rows = c.get(f"/api/v1/projects/{project}/triggers").json()
    assert len(rows) == 1 and rows[0]["source"] == "github"
    assert rows[0]["addressed"] == "label" and rows[0]["thread_keys"] == [f"{project}:github:issue:5"]
    trigger_id = rows[0]["id"]

    threads = c.get(f"/api/v1/projects/{project}/threads").json()
    assert threads[0]["key"] == f"{project}:github:issue:5"

    result = c.post(f"/api/v1/triggers/{trigger_id}/route",
                    json={"kind": "accept", "title": "Fix login", "goal": "Fix the login"})
    assert result.status_code == 200 and result.json()["ticket_id"]
    task_id = result.json()["ticket_id"]
    again = c.post(f"/api/v1/triggers/{trigger_id}/route",
                   json={"kind": "accept", "title": "Fix login again", "goal": "Fix the login"})
    assert again.status_code == 200 and again.json()["ticket_id"] == task_id
    stage = c.get(f"/api/v1/tasks/{task_id}/stage").json()
    assert stage["stage"] == "working/queued"
    assert stage["mandate"]["strength"] == "explicit"


def test_same_issue_number_is_isolated_across_projects(project):
    from devflow import core
    from devflow.models import Project, Thread
    with transaction() as db:
        other = core.new_project(db, "other/repo", {"allowed_actors": ["owner"]},
                                 {"verify_commands": ["true"]})
        other_id = other.id
    c = client()
    for repository, delivery in [("owner/repo", "isolation-1"), ("other/repo", "isolation-2")]:
        payload = {"repository": {"full_name": repository}, "action": "opened",
                   "issue": {"number": 1, "title": "Idea", "body": "An idea"},
                   "sender": {"login": "owner"}}
        assert webhook(c, delivery, payload).status_code == 202
        assert asyncio.run(github_events.process_one())
    with transaction() as db:
        assert db.get(Thread, f"{project}:github:issue:1").project_id == project
        assert db.get(Thread, f"{other_id}:github:issue:1").project_id == other_id


def test_policy_spec_and_steer_endpoints(project, task):
    c = client()
    policy = c.post(f"/api/v1/projects/{project}/policy",
                    json={"mode": "auto",
                          "rules": [{"id": "copy", "name": "Copy", "enabled": True,
                                     "instruction": "Fix wording."}]})
    assert policy.status_code == 200 and policy.json()["policy_version"]

    spec = c.post(f"/api/v1/projects/{project}/spec",
                  json={"text": "Always run the full login regression."})
    assert spec.json()["spec_version"] == 1 and spec.json()["delivered"] == 1

    steer = c.post(f"/api/v1/tasks/{task}/steer",
                   json={"kind": "constraint", "text": "Keep the public API unchanged."})
    assert steer.status_code == 200 and steer.json()["kind"] == "steer"
    with transaction() as db:
        assert db.scalar(select(TaskInput).where(TaskInput.task_id == task,
                                                 TaskInput.kind == "steer"))
        assert db.get(Task, task).spec_version == 0


def test_requeue_and_evals_list(project):
    c = client()
    payload = {"repository": {"full_name": "owner/repo"}, "action": "created",
               "issue": {"number": 6, "title": "Idea", "body": "maybe dark mode?"},
               "comment": {"id": 1, "body": "maybe dark mode?"},
               "sender": {"login": "owner"}}
    webhook(c, "api-2", payload, event="issue_comment")
    assert asyncio.run(github_events.process_one())
    trigger = c.get(f"/api/v1/projects/{project}/triggers", params={"status": "undecided"}).json()[0]
    assert c.post(f"/api/v1/triggers/{trigger['id']}/requeue").json()["requeued"] is True
    assert c.get(f"/api/v1/projects/{project}/evals").status_code == 200


def test_router_memory_endpoint_and_project_summary(project):
    c = client()
    summary = c.get(f"/api/v1/projects/{project}").json()
    assert "router_summary" in summary and "router_until" in summary
    memory = c.get(f"/api/v1/projects/{project}/router")
    assert memory.status_code == 200
    assert "memory" in memory.json() and "notices" in memory.json()


def test_feishu_bind_endpoint(project, monkeypatch):
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test")
    settings.cache_clear()
    c = client()
    result = c.post("/api/v1/feishu/bind",
                    json={"project_id": project, "chat_id": "oc_api", "bot_open_id": "ou_bot",
                          "allowed_open_ids": ["ou_owner"]})
    assert result.status_code == 201 and result.json()["chat_id"] == "oc_api"
    from devflow.models import Project
    with transaction() as db:
        p = db.get(Project, project)
        assert "ou_owner" in p.policy["source_allowed"]["feishu"]
