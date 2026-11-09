"""Application-bot replies. Destination is derived from the admitted source message."""
import json
import re
import httpx
from . import core
from .adapters import ExternalError
from .config import settings
from .models import FeishuBinding, FeishuMessage


def queue_reply(db, project_id, job, text):
    if job.payload.get("source") != "feishu":
        return
    message_id = job.payload.get("message_id")
    source = db.get(FeishuMessage, message_id) if message_id else None
    chat_id = job.payload.get("chat_id")
    binding = db.get(FeishuBinding, (settings().feishu_app_id, chat_id)) if chat_id else None
    if not source or not binding or source.chat_id != chat_id or source.status != "routed":
        return
    if (binding.project_id != project_id or source.sender_id not in binding.allowed_open_ids
            or binding.conversation_id != job.payload.get("conversation_id")):
        return
    text = text.strip() or "已处理本条请求，请查看项目进展。"
    if len(text.encode("utf-8")) > 18000:
        text = text.encode("utf-8")[:18000].decode("utf-8", errors="ignore") + "\n（回复较长，完整内容已保存在项目对话中。）"
    return core.effect(db, project_id, "feishu_reply", "feishu-reply:"+job.id,
        {"app_id":binding.app_id, "chat_id":chat_id, "message_id":message_id, "text":text})


async def reply(message_id, text, dedupe_id):
    if not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
        raise ValueError("Invalid Feishu message ID")
    config = settings()
    if not config.feishu_app_id or not config.feishu_app_secret:
        raise ExternalError("Feishu application credentials are not configured")
    # Never persist the short-lived tenant token or include provider bodies in errors.
    async with httpx.AsyncClient(timeout=15) as client:
        auth = await client.post(config.feishu_api_url.rstrip("/") + "/auth/v3/tenant_access_token/internal",
            json={"app_id":config.feishu_app_id, "app_secret":config.feishu_app_secret})
        if auth.status_code >= 400:
            raise ExternalError(f"Feishu authentication HTTP {auth.status_code}")
        data = auth.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            code = data.get("code")
            raise ExternalError(f"Feishu application authentication rejected (code {code if isinstance(code,int) else chr(63)})")
        response = await client.post(config.feishu_api_url.rstrip("/") + "/im/v1/messages/"+message_id+"/reply",
            headers={"Authorization":"Bearer "+data["tenant_access_token"]},
            json={"msg_type":"text", "content":json.dumps({"text":text},ensure_ascii=False), "uuid":dedupe_id})
    if response.status_code >= 400:
        retry = response.headers.get("retry-after", "2")
        raise ExternalError(f"Feishu reply HTTP {response.status_code}", int(retry) if retry.isdigit() else 60)
    body = response.json()
    if body.get("code") != 0:
        code = body.get("code")
        raise ExternalError(f"Feishu reply rejected (code {code if isinstance(code,int) else 'unknown'})")
    return {"message_id":body["data"]["message_id"]}
