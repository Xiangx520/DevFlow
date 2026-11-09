"""Application bot long connection: persist before acknowledging."""
import json
import hashlib
import time
import logging
import re
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import transaction
from .models import FeishuMessage, FeishuBinding, Conversation, Message
from . import core

log = logging.getLogger("devflow.feishu")

def bind_group(db, project_id, app_id, chat_id, bot_open_id, allowed_open_ids):
    if not all((app_id, chat_id, bot_open_id, allowed_open_ids)):
        raise ValueError("Application, group, bot and authorized users are required")
    project = core.lock_project(db, project_id)
    # Feishu open_ids live in their own identity space; never map them onto GitHub logins.
    from . import policy as policy_module
    policy_value = policy_module.normalize(project.policy)
    policy_value["source_allowed"]["feishu"] = list(dict.fromkeys(
        list(policy_value["source_allowed"].get("feishu") or []) + list(allowed_open_ids)))
    project.policy = policy_value
    project.policy_version = policy_module.version(policy_value)
    binding = db.get(FeishuBinding, (app_id, chat_id))
    if binding:
        if binding.project_id != project_id:
            raise core.Conflict("Group already belongs to another project")
        binding.bot_open_id = bot_open_id
        binding.allowed_open_ids = list(set(allowed_open_ids))
    else:
        conversation = Conversation(project_id=project_id)
        db.add(conversation); db.flush()
        binding = FeishuBinding(app_id=app_id, chat_id=chat_id, project_id=project_id,
            conversation_id=conversation.id, bot_open_id=bot_open_id,
            allowed_open_ids=list(set(allowed_open_ids)))
        db.add(binding); db.flush()
    core.event(db, project_id, "feishu.bound", {"chat_id":chat_id, "conversation_id":binding.conversation_id})
    return binding


def route(db, row, message, sender):
    binding = db.get(FeishuBinding, (settings().feishu_app_id, row.chat_id))
    if binding is None:
        return
    project = core.lock_project(db, binding.project_id)
    row.status = "observed"
    open_id = sender.get("sender_id", {}).get("open_id", "")
    mentions = message.get("mentions", [])
    mentioned = any(m.get("id", {}).get("open_id") == binding.bot_open_id for m in mentions)
    addressed = "mention" if mentioned else ("direct" if message.get("chat_type") != "group" else None)
    if message.get("message_type") != "text":
        row.status = "unsupported"
        return
    try:
        text = json.loads(message.get("content", "{}"))["text"]
    except (ValueError, KeyError, TypeError):
        row.status = "unsupported"
        return
    if not isinstance(text, str):
        row.status = "unsupported"
        return
    # Remove only the bot mention placeholder; keep the rest of the owner's request.
    for mention in mentions:
        if mention.get("id", {}).get("open_id") == binding.bot_open_id and mention.get("key"):
            text = text.replace(mention["key"], "")
    text = text.strip()
    if not text or len(text) > 30000:
        row.status = "unsupported"
        return
    entry = Message(conversation_id=binding.conversation_id, role="user", content=text)
    db.add(entry); db.flush()
    core.event(db, binding.project_id, "conversation.message", {
        "conversation_id":binding.conversation_id, "message_id":entry.id,
        "role":"user", "content":text, "source":"feishu"}, key="feishu-event:"+row.message_id)
    # Every admitted message becomes a Trigger. The project's Feishu scope decides whether unaddressed
    # discussion is watched; authorization remains a gate property, never a property of the text.
    from . import triggers
    result = triggers.receive(db, project, {
        "source": "feishu", "kind": "feishu.message",
        "external_id": row.message_id, "dedupe_key": "feishu-trigger:" + row.message_id + ":" + revision(message),
        "actor": open_id, "human": True, "addressed": addressed,
        "thread_keys": [project.id + ":feishu:chat:" + row.chat_id],
        "text": text, "url": "", "channel": {"chat_id": row.chat_id,
                                             "message_id": row.message_id,
                                             "chat_type": message.get("chat_type"),
                                             "conversation_id": binding.conversation_id},
        "at": row.received_at, "payload": {"message_id": row.message_id},
    })
    if open_id not in (binding.allowed_open_ids or []):
        row.status = "unauthorized"
    else:
        row.status = "routed" if result == "stored" else result


def revision(message):
    content = {key: message.get(key) for key in ("content", "mentions", "message_type")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:24]


def receive(payload):
    event = payload.get("event", {})
    message, sender = event.get("message", {}), event.get("sender", {})
    if sender.get("sender_type") != "user":
        return False
    message_id, chat_id = message.get("message_id"), message.get("chat_id")
    if not message_id or not chat_id:
        raise ValueError("Feishu message missing identity")
    try:
        with transaction() as db:
            existing = db.get(FeishuMessage, message_id)
            if existing:
                old_message = (existing.payload.get("event") or {}).get("message") or {}
                if revision(old_message) == revision(message):
                    return False
                existing.payload = payload
                existing.received_at = time.time()
                route(db, existing, message, sender)
                return True
            row = FeishuMessage(message_id=message_id, chat_id=chat_id,
                sender_id=sender.get("sender_id", {}).get("open_id", ""), payload=payload)
            db.add(row); db.flush()
            route(db, row, message, sender)
    except IntegrityError:
        with transaction() as db:
            if not db.get(FeishuMessage, message_id):
                raise
        return False
    log.info("Feishu message persisted and admission evaluated")
    return True

class SafeSDKLog(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        config = settings()
        for secret in (config.feishu_app_secret, config.feishu_app_id):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?:https?|wss?)://\S+", "[connection URL redacted]", text)
        record.msg, record.args = text, ()
        return True


def main():
    import lark_oapi as lark
    config = settings()
    if not config.feishu_app_id or not config.feishu_app_secret:
        raise RuntimeError("Configure DEVFLOW_FEISHU_APP_ID and DEVFLOW_FEISHU_APP_SECRET")
    logging.basicConfig(level=logging.INFO)
    def handle(data):
        receive(json.loads(lark.JSON.marshal(data)))
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle).build()
    client = lark.ws.Client(config.feishu_app_id, config.feishu_app_secret,
        event_handler=handler, log_level=lark.LogLevel.INFO)
    from lark_oapi.core.log import logger as sdk_log
    sdk_log.addFilter(SafeSDKLog())
    sdk_log.propagate = False
    log.info("Starting Feishu receiver; authenticated connection URLs are suppressed")
    client.start()

if __name__ == "__main__":
    main()


async def reconcile():
    """Backfill bound group history. Commit cursor only after every page succeeded."""
    import httpx
    from sqlalchemy import select
    from .models import SourceCursor
    config = settings()
    if not config.feishu_app_id or not config.feishu_app_secret:
        return
    with transaction() as db:
        bindings = [(row.project_id, row.chat_id) for row in db.scalars(select(FeishuBinding).where(
                    FeishuBinding.app_id == config.feishu_app_id))]
    async with httpx.AsyncClient(base_url=config.feishu_api_url.rstrip("/"), timeout=30) as client:
        response = await client.post("auth/v3/tenant_access_token/internal",
            json={"app_id": config.feishu_app_id, "app_secret": config.feishu_app_secret})
        response.raise_for_status()
        token = response.json().get("tenant_access_token")
        if not token:
            raise RuntimeError("Feishu history authentication unavailable")
        for project_id, chat_id in bindings:
            started = time.time()
            with transaction() as db:
                cursor = db.get(SourceCursor, (project_id, "feishu", chat_id))
                since = float((cursor.position if cursor else {}).get("through", 0))
            page = ""; seen = set()
            while True:
                result = await client.get("im/v1/messages", headers={"Authorization": "Bearer " + token},
                    params={"container_id_type": "chat", "container_id": chat_id, "page_size": 50,
                            "start_time": str(int(max(0, since-120))), "sort_type": "ByCreateTimeAsc", "page_token": page})
                result.raise_for_status()
                body = result.json()
                if body.get("code") != 0:
                    raise RuntimeError("Feishu history unavailable")
                data = body.get("data") or {}
                for item in data.get("items") or []:
                    sender = item.get("sender") or {}
                    message = {"message_id": item["message_id"], "chat_id": chat_id, "chat_type": "group",
                               "message_type": item.get("msg_type", "text"),
                               "content": (item.get("body") or {}).get("content", "{}"),
                               "mentions": item.get("mentions") or []}
                    receive({"event": {"message": message, "sender": {"sender_type": sender.get("sender_type", "user"),
                             "sender_id": {"open_id": sender.get("id") or ""}}}})
                if not data.get("has_more"):
                    break
                page = data.get("page_token")
                if not page or page in seen:
                    raise RuntimeError("Feishu history cursor did not advance")
                seen.add(page)
            with transaction() as db:
                row = db.get(SourceCursor, (project_id, "feishu", chat_id))
                if row is None:
                    row = SourceCursor(project_id=project_id, source="feishu", scope=chat_id)
                    db.add(row)
                row.position = {"through": started}; row.updated_at = time.time()
