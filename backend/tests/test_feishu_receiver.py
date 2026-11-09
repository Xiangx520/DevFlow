import logging
import pytest
from sqlalchemy import select
from devflow.db import transaction
from devflow.feishu_receiver import receive, SafeSDKLog
from devflow.models import FeishuMessage, Inbox, Task, Trigger


def payload(sender_type="user"):
    return {"event": {"sender": {"sender_type": sender_type, "sender_id": {"open_id": "ou_owner"}},
        "message": {"message_id": "om_test", "chat_id": "oc_test", "content": '{"text":"hello"}'}}}


def test_durable_dedup_and_no_automatic_development():
    assert receive(payload())
    assert not receive(payload())
    with transaction() as db:
        assert len(list(db.scalars(select(FeishuMessage)))) == 1
        assert db.get(FeishuMessage, "om_test").status == "unbound"
        assert not list(db.scalars(select(Inbox)))
        assert not list(db.scalars(select(Task)))


def test_ignore_bot_and_reject_missing_identity():
    assert not receive(payload("bot"))
    p = payload(); del p["event"]["message"]["message_id"]
    with pytest.raises(ValueError):
        receive(p)


def test_sdk_logs_hide_authenticated_url_and_secret(monkeypatch):
    from devflow.config import settings
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_SECRET", "test-secret")
    settings.cache_clear()
    record = logging.LogRecord("Lark", 20, "", 1,
        "connected to wss://example.test/ws?token=private test-secret", (), None)
    assert SafeSDKLog().filter(record)
    assert "private" not in record.getMessage()
    assert "test-secret" not in record.getMessage()
    assert "connected to" in record.getMessage()


def test_bound_owner_mention_routes_once(project, monkeypatch):
    from devflow.feishu_receiver import bind_group
    from devflow.models import Conversation
    from devflow.config import settings
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test"); settings.cache_clear()
    with transaction() as db:
        b = bind_group(db, project, "cli_test", "oc_test", "ou_bot", ["ou_owner"])
        cid = b.conversation_id
        assert bind_group(db, project, "cli_test", "oc_test", "ou_bot", ["ou_owner"]).conversation_id == cid
    p = payload(); m = p["event"]["message"]
    m.update(chat_type="group", message_type="text", content='{"text":"@_bot inspect project status"}',
        mentions=[{"key":"@_bot","id":{"open_id":"ou_bot"}}])
    assert receive(p)
    assert not receive(p)
    with transaction() as db:
        triggers = list(db.scalars(select(Trigger)))
        assert len(triggers) == 1 and triggers[0].source == "feishu"
        assert triggers[0].addressed == "mention" and triggers[0].actor == "ou_owner"
        assert triggers[0].text == "inspect project status"
        jobs = list(db.scalars(select(Inbox)))
        assert len(jobs) == 1 and jobs[0].kind == "explicit_candidate"
        assert db.get(FeishuMessage, "om_test").status == "routed"
        assert not list(db.scalars(select(Task)))


def test_binding_does_not_replay_or_trust_other_senders(project, monkeypatch):
    from devflow.feishu_receiver import bind_group
    from devflow.config import settings
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test"); settings.cache_clear()
    receive(payload())
    with transaction() as db:
        bind_group(db, project, "cli_test", "oc_test", "ou_bot", ["ou_owner"])
    assert not receive(payload())
    for mid, actor, mentioned in [("ordinary","ou_owner",False), ("stranger","ou_stranger",True), ("otherbot","ou_owner",True)]:
        p = payload(); p["event"]["sender"]["sender_id"]["open_id"] = actor
        p["event"]["message"].update(message_id=mid, chat_type="group", message_type="text",
            mentions=[{"id":{"open_id":"ou_other" if mid == "otherbot" else "ou_bot"}}] if mentioned else [])
        receive(p)
    with transaction() as db:
        assert not list(db.scalars(select(Inbox)))
        assert db.get(FeishuMessage,"stranger").status == "unauthorized"

