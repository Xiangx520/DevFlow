import json
import httpx
import pytest
from fastapi.testclient import TestClient
from devflow import adapters, core
from devflow.api import app_factory
from devflow.config import settings
from devflow.db import transaction


@pytest.mark.parametrize("thinking", [None, "disabled"])
async def test_management_thinking_option(monkeypatch, thinking):
    config = settings()
    config.llm_api_key = "test-only"
    config.llm_model = "test-model"
    config.llm_thinking = thinking
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"arguments": '{"ok": true}'}}]}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert await adapters.Model().json("Probe", {}, {"type": "object"}) == {"ok": True}
    if thinking:
        assert requests[0]["thinking"] == {"type": thinking}
    else:
        assert "thinking" not in requests[0]


def test_runner_proxy_enforces_configured_thinking(task, monkeypatch):
    config = settings()
    config.llm_api_key = "test-only"
    config.llm_model = "test-model"
    config.llm_thinking = "disabled"
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[],"usage":{"total_tokens":7}}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with transaction() as db:
        attempt, capability = core.claim_task(db, task, "probe")
    with TestClient(app_factory()) as client:
        result = client.post(f"/internal/attempts/{attempt.id}/model/chat/completions",
            headers={"Authorization": "Bearer " + capability},
            json={"model": "untrusted", "messages": [], "stream": True, "thinking": {"type": "enabled"}})
        assert result.status_code == 200
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert seen[0]["model"] == "test-model"
