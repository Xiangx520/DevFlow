"""Real API, adapter HTTP, scheduler and database; only external services are substitutes."""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import urlparse, parse_qs
import httpx
import pytest
import uvicorn
from sqlalchemy import select
from devflow import assessment, core, effects, github_events, policy, router, tickets, triggers, watcher
from devflow.api import app_factory
from devflow.config import settings
from devflow.db import transaction
from devflow.models import Control, Decision, Inbox, Project, SourceCursor, Task, TaskInput, Thread, Trigger
from devflow.worker import schedule_one


class Platform(BaseHTTPRequestHandler):
    calls = Counter()
    writes = Counter()
    failures = Counter()
    def log_message(self, *args): pass
    def respond(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def issue(self):
        return {"id": 10, "number": 7, "title": "Copy", "body": "Change label to Save", "updated_at": "2026-11-08T01:00:00Z", "user": {"login": "owner"}, "labels": []}
    def comment(self):
        return {"id": 70, "body": "Change the button label to Save", "updated_at": "2026-11-08T01:00:00Z", "user": {"login": "owner"}, "issue_url": "http://local/repos/owner/repo/issues/7"}
    def do_GET(self):
        parsed = urlparse(self.path); path = parsed.path; query = parse_qs(parsed.query)
        Platform.calls[path] += 1
        if Platform.failures[path]:
            Platform.failures[path] -= 1
            return self.respond({"error": "rate limited"}, 429)
        if path == "/repos/owner/repo/issues":
            return self.respond([self.issue()] * (100 if query.get("page") == ["1"] else 1))
        if path == "/repos/owner/repo/issues/comments": return self.respond([self.comment()])
        if path == "/repos/owner/repo/issues/7": return self.respond(self.issue())
        if path == "/repos/owner/repo/pulls": return self.respond([])
        if path == "/open-apis/im/v1/messages":
            item = {"message_id": "om_history", "msg_type": "text", "body": {"content": '{"text":"Change the label to Save"}'}, "sender": {"id": "ou_owner", "sender_type": "user"}}
            return self.respond({"code": 0, "data": {"items": [item], "has_more": not bool(query.get("page_token", [""])[0]), "page_token": "second"}})
        return self.respond({"detail": path}, 404)
    def do_POST(self):
        path = urlparse(self.path).path
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        Platform.calls[path] += 1
        if path.endswith("/chat/completions"):
            schema = payload["tools"][0]["function"]["parameters"]["properties"]
            source = json.loads(payload["messages"][1]["content"])
            if "items" in schema:
                Platform.calls["model.router"] += 1
                result = {"items": [{"trigger_id": source["trigger"]["trigger_id"], "kind": "accept", "title": "Copy fix", "goal": "router summary must not become goal"}]}
            elif "decision" in schema:
                Platform.calls["model.assessment"] += 1
                result = {"decision": "accept", "rule_assessments": [{"rule_id": "copy", "matched": True}],
                          "direction": {"implementation_requested": True, "target_basis": "specified", "requires_design_decision": False},
                          "title": "Copy fix", "scope": "small copy edit", "intent_evidence": source["trigger"]["text"]}
            else:
                Platform.calls["model.watchdog"] += 1
                result = {"aligned": True}
            return self.respond({"choices": [{"message": {"tool_calls": [{"function": {"name": "submit", "arguments": json.dumps(result)}}]}}], "usage": {"total_tokens": 20}})
        if path.endswith("/internal"):
            return self.respond({"code": 0, "tenant_access_token": "local-token"})
        if path.endswith("/reply"):
            Platform.writes["feishu.reply"] += 1
            return self.respond({"code": 0, "data": {"message_id": "om_reply"}})
        if path == "/notify":
            Platform.writes["feishu.notify"] += 1
            return self.respond({"code": 0})
        return self.respond({"detail": path}, 404)


@pytest.fixture
def services(monkeypatch):
    Platform.calls = Counter(); Platform.writes = Counter(); Platform.failures = Counter()
    external = ThreadingHTTPServer(("127.0.0.1", 0), Platform)
    background = threading.Thread(target=external.serve_forever, daemon=True); background.start()
    origin = f"http://127.0.0.1:{external.server_port}"
    monkeypatch.setenv("DEVFLOW_LLM_BASE_URL", origin + "/v1")
    monkeypatch.setenv("DEVFLOW_LLM_MODEL", "simulation")
    monkeypatch.setenv("DEVFLOW_LLM_API_KEY", "local-substitute")
    monkeypatch.setenv("DEVFLOW_GITHUB_API_URL", origin)
    monkeypatch.setenv("DEVFLOW_FEISHU_API_URL", origin + "/open-apis")
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_SECRET", "local-secret")
    monkeypatch.setenv("DEVFLOW_FEISHU_WEBHOOK", origin + "/notify")
    settings.cache_clear()
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    api = uvicorn.Server(uvicorn.Config(app_factory(), host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    api_thread = threading.Thread(target=api.run, daemon=True); api_thread.start()
    for _ in range(100):
        if api.started: break
        time.sleep(.02)
    assert api.started
    yield f"http://127.0.0.1:{port}"
    api.should_exit = True; api_thread.join(timeout=5)
    external.shutdown(); external.server_close(); background.join(timeout=5)


async def drain_intake():
    for _ in range(30):
        if not await github_events.process_one(): return
    raise AssertionError("intake failed to drain")


async def test_http_observe_auto_fast_pr_control_and_recovery(project, services, request):
    async with httpx.AsyncClient(base_url=services, headers={"Authorization": "Bearer owner-test-token"}) as api:
        rule = {"id": "copy", "instruction": "Accept settled small wording changes"}
        response = await api.post(f"/api/v1/projects/{project}/policy", json={"mode": "observe", "rules": [rule]})
        assert response.status_code == 200
        payload = {"repository": {"full_name": "owner/repo"}, "action": "created", "issue": Platform.issue(None), "comment": Platform.comment(None), "sender": {"login": "owner"}}
        body = json.dumps(payload).encode()
        headers = {"X-Github-Event": "issue_comment", "X-Github-Delivery": "one",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(b"webhook-test-secret", body, hashlib.sha256).hexdigest()}
        assert (await api.post("/api/v1/webhooks/github", content=body, headers=headers)).status_code == 202
        assert (await api.post("/api/v1/webhooks/github", content=body, headers=headers)).json()["duplicate"]
        await drain_intake()
        with transaction() as db: triggers.evaluate_due(db, db.get(Project, project))
        await effects.process_one(include={"assessment"})
        with transaction() as db:
            assert not list(db.scalars(select(Task)))
            assert db.scalar(select(Decision)).audit["would_accept"]
        assert not Platform.writes
        await api.post(f"/api/v1/projects/{project}/policy", json={"mode": "auto"})
        payload["comment"]["id"] = 71; payload["comment"]["body"] = "Please change caption to Save"
        body = json.dumps(payload).encode(); headers["X-Github-Delivery"] = "two"
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"webhook-test-secret", body, hashlib.sha256).hexdigest()
        await api.post("/api/v1/webhooks/github", content=body, headers=headers)
        await drain_intake()
        with transaction() as db: triggers.evaluate_due(db, db.get(Project, project))
        await effects.process_one(include={"assessment"})
        with transaction() as db:
            task = db.scalar(select(Task)); task_id = task.id
            assert task.goal == "Please change caption to Save"
            task.pr_number = 42
            tickets.subscribe(db, task, "github:pr:42")
        assert Platform.calls["model.router"] == 0
        claimed = await schedule_one(); assert claimed
        # Simulate a worker process disappearing; the durable session survives recovery.
        with transaction() as db:
            task = db.get(Task, task_id); session = task.session_id
            task.lease_until = time.time()-1; watcher.run_liveness(db)
            assert task.status == "queued" and task.session_id == session
        payload = {"repository": {"full_name": "owner/repo"}, "action": "created", "issue": {"number": 42, "pull_request": {"url": "http://local/pulls/42"}},
                   "comment": {"id": 72, "body": "Also preserve existing spacing", "updated_at": "v1"}, "sender": {"login": "owner"}}
        body = json.dumps(payload).encode(); headers["X-Github-Delivery"] = "pr-comment"
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"webhook-test-secret", body, hashlib.sha256).hexdigest()
        await api.post("/api/v1/webhooks/github", content=body, headers=headers)
        await drain_intake()
        with transaction() as db:
            assert any(row.payload.get("text") == "Also preserve existing spacing" for row in db.scalars(
                select(TaskInput).where(TaskInput.task_id == task_id)))
        assert not await router.route_one()
        assert Platform.calls["model.router"] == 0
        claimed = await schedule_one(); assert claimed
        stopped = await api.post(f"/api/v1/tasks/{task_id}/inputs", json={"text": "/stop"}, headers={"Idempotency-Key": "stop"})
        assert stopped.json()["trigger_id"] == stopped.json()["input_id"]
        with transaction() as db:
            assert db.get(Task, task_id).ended_by == "human"
            assert db.scalar(select(Control).where(Control.kind == "stop")).status == "applied"
            with pytest.raises(core.Conflict): core.active_attempt(db, *claimed)
        assert Platform.calls["model.router"] == 0
    request.node.simulation_metrics = {"model_calls": dict(Platform.calls), "external_writes": dict(Platform.writes)}


async def test_http_pagination_rate_limit_and_backfill_dedupe(project, services, request):
    from devflow import feishu_receiver
    with transaction() as db:
        feishu_receiver.bind_group(db, project, "cli_test", "group", "bot", ["ou_owner"])
    Platform.failures["/repos/owner/repo/issues"] = 1
    await github_events.reconcile()
    with transaction() as db: assert db.get(SourceCursor, (project, "github", "owner/repo")) is None
    await github_events.reconcile(); await drain_intake()
    with transaction() as db:
        cursor = db.get(SourceCursor, (project, "github", "owner/repo")); assert cursor.position["through"]
        before = len(list(db.scalars(select(Trigger))))
        db.get(Project, project).last_poll = 0
    await github_events.reconcile(); await drain_intake()
    with transaction() as db: assert len(list(db.scalars(select(Trigger)))) == before
    await feishu_receiver.reconcile()
    with transaction() as db:
        assert db.get(SourceCursor, (project, "feishu", "group"))
        before = len(list(db.scalars(select(Trigger))))
    await feishu_receiver.reconcile()
    with transaction() as db: assert len(list(db.scalars(select(Trigger)))) == before
    assert Platform.calls["/repos/owner/repo/issues"] >= 5
    assert Platform.calls["/open-apis/im/v1/messages"] == 4
    assert not list(Platform.writes)
    request.node.simulation_metrics = {"model_calls": 0, "external_writes": dict(Platform.writes), "http_calls": dict(Platform.calls)}
