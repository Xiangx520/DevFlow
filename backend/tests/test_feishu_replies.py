import json
import time
import httpx
import pytest
from sqlalchemy import select
from devflow import core, effects, router
from devflow.config import settings
from devflow.db import transaction
from devflow.models import Effect, FeishuBinding, Inbox, Task
from devflow.feishu_receiver import bind_group, receive
from devflow.feishu_replies import reply
from test_management import Scripted


async def route_reply(text):
    from devflow.models import Trigger
    with transaction() as db:
        trigger = db.scalar(select(Trigger))
        identifier = trigger.id
    await router.route_one(Scripted({"items": [{"trigger_id": identifier, "kind": "ignore", "reason": text}]}))


def admit(project, monkeypatch):
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_SECRET", "secret-test")
    settings.cache_clear()
    with transaction() as db:
        binding = bind_group(db,project,"cli_test","oc_group","ou_bot",["ou_owner"])
        conversation_id = binding.conversation_id
    receive({"event":{"sender":{"sender_type":"user","sender_id":{"open_id":"ou_owner"}},
        "message":{"message_id":"om_request","chat_id":"oc_group","chat_type":"group",
            "message_type":"text","content":'{"text":"@_bot status"}',
            "mentions":[{"key":"@_bot","id":{"open_id":"ou_bot"}}]}}})


async def test_router_reply_and_delivery_retry(project, monkeypatch):
    admit(project,monkeypatch)
    await route_reply("No active tasks.")
    calls=[]
    async def send(mid,text,key):
        calls.append((mid,text,key))
        if len(calls)==1: raise httpx.ReadTimeout("response lost")
        return {"message_id":"om_reply"}
    monkeypatch.setattr(effects,"feishu_reply",send)
    await effects.process_one(include={"feishu_reply"})
    with transaction() as db:
        e=db.scalar(select(Effect).where(Effect.kind=="feishu_reply"))
        assert e.status=="pending" and e.tries==1
        e.available_at=0
        assert not list(db.scalars(select(Task)))
    await effects.process_one(include={"feishu_reply"})
    assert len(calls)==2 and calls[0]==calls[1]
    with transaction() as db:
        rows=list(db.scalars(select(Effect).where(Effect.kind=="feishu_reply")))
        assert len(rows)==1 and rows[0].status=="sent"
        assert rows[0].result=={"message_id":"om_reply"}


async def test_revoked_binding_drops_reply(project,monkeypatch):
    admit(project,monkeypatch)
    await route_reply("ok")
    with transaction() as db:
        db.get(FeishuBinding,("cli_test","oc_group")).allowed_open_ids=[]
    async def forbidden(*args): raise AssertionError("must not send")
    monkeypatch.setattr(effects,"feishu_reply",forbidden)
    await effects.process_one(include={"feishu_reply"})
    with transaction() as db:
        assert db.scalar(select(Effect)).status=="dropped"


async def test_router_failure_queues_one_reply(project,monkeypatch):
    admit(project,monkeypatch)
    class Broken:
        async def json(self,*args,**kwargs): raise RuntimeError("model unavailable")
    for _ in range(3):
        assert await router.route_one(Broken()) is False
        with transaction() as db:
            db.scalar(select(Inbox).where(Inbox.kind=="explicit_candidate")).available_at=0
    with transaction() as db:
        assert db.scalar(select(Inbox).where(Inbox.kind=="explicit_candidate")).status=="unavailable"
        rows=list(db.scalars(select(Effect)))
        assert len(rows)==1 and rows[0].kind=="feishu_reply"


async def test_no_resend_after_provider_dedup_window(project,monkeypatch):
    admit(project,monkeypatch)
    await route_reply("ok")
    with transaction() as db:
        e=db.scalar(select(Effect));e.tries=1;e.created_at=time.time()-3601
    async def forbidden(*args): raise AssertionError("must not resend")
    monkeypatch.setattr(effects,"feishu_reply",forbidden)
    await effects.process_one(include={"feishu_reply"})
    with transaction() as db:
        assert db.scalar(select(Effect)).status=="failed"


async def test_reply_api_uses_original_message_and_stable_uuid(monkeypatch):
    config=settings();config.feishu_app_id="cli_test";config.feishu_app_secret="test-secret"
    seen=[]
    def respond(req):
        body=json.loads(req.content);seen.append((req,body))
        if req.url.path.endswith("/internal"):
            return httpx.Response(200,json={"code":0,"tenant_access_token":"temporary"})
        return httpx.Response(200,json={"code":0,"data":{"message_id":"om_reply"}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    assert await reply("om_original","hello","stable-uuid")=={"message_id":"om_reply"}
    assert seen[1][0].url.path.endswith("/om_original/reply")
    assert seen[1][0].headers["authorization"]=="Bearer temporary"
    assert seen[1][1]["uuid"]=="stable-uuid"
    assert json.loads(seen[1][1]["content"])["text"]=="hello"


async def test_provider_error_does_not_leak_credentials(monkeypatch):
    config=settings();config.feishu_app_id="cli_test";config.feishu_app_secret="test-secret"
    original=httpx.AsyncClient
    def respond(req):
        return httpx.Response(200,json={"code":123,"msg":"test-secret temporary"})
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    with pytest.raises(Exception) as exc:
        await reply("om_original","hello","stable")
    assert "test-secret" not in str(exc.value) and "temporary" not in str(exc.value)
