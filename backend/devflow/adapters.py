"""External adapters; callers own retry and durable effect semantics."""
import asyncio
import json
import re
import httpx
from .config import settings

class ExternalError(RuntimeError):
    def __init__(self, message, retry_after=2):
        super().__init__(message); self.retry_after = retry_after

class GitHub:
    def __init__(self):
        self.config = settings()
        self.write_guard = None
    async def request(self, method, path, **kwargs):
        if not path.startswith("/repos/"): raise ValueError("repository API only")
        if method.upper() not in {"GET", "HEAD"} and self.write_guard is not None:
            self.write_guard()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": f"Bearer {self.config.github_token}"}
        async with httpx.AsyncClient(base_url=self.config.github_api_url, headers=headers, timeout=30) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            retry = response.headers.get("retry-after", "2")
            raise ExternalError(f"GitHub HTTP {response.status_code}", int(retry) if retry.isdigit() else 60)
        return response.json() if response.content else {}
    async def pages(self, path, params=None):
        result = []
        for page in range(1, 101):
            items = await self.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            if not isinstance(items, list): raise ExternalError("unexpected pagination shape")
            result.extend(items)
            if len(items) < 100: return result
        raise ExternalError("pagination exceeds 100 pages; refusing incomplete evidence")
    async def comment(self, repository, number, body, key):
        path = f"/repos/{repository}/issues/{number}/comments"
        marker = f"<!-- devflow:{key} -->"
        comments = await self.pages(path)
        existing = next((x for x in comments if marker in (x.get("body") or "")), None)
        if existing: return existing
        return await self.request("POST", path, json={"body": body + "\n\n" + marker})
    async def pull_request(self, project, task, body):
        path = f"/repos/{project.repository}/pulls"
        owner = project.repository.split("/")[0]
        pulls = await self.request("GET", path, params={"state": "all", "head": f"{owner}:{task.branch}"})
        marker = f"<!-- devflow-task:{task.id} -->"
        if pulls:
            pr = pulls[0]
            if pr["state"] != "open": raise ExternalError("task PR is closed; explicit recovery required")
            expected_body = body + "\n\n" + marker
            if pr.get("body") == expected_body:
                return pr
            return await self.request("PATCH", path + f"/{pr['number']}",
                                      json={"body": body + "\n\n" + marker})
        return await self.request("POST", path, json={"title": task.title, "head": task.branch,
                                  "base": project.default_branch, "body": body + "\n\n" + marker})

class Model:
    async def json(self, system, payload, schema, timeout=45, task_id=None):
        config = settings()
        if not config.llm_api_key or not config.llm_model:
            raise ExternalError("model is not configured")
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode()) > 300000:
            raise ExternalError("management context too large; narrow the task or evidence")
        reservation = len((system + text).encode()) + 3000
        if task_id:
            from . import core
            from .db import transaction
            from .models import Project
            exhausted = False
            with transaction() as db:
                task = core.lock_task(db, task_id)
                project = core.get(db, Project, task.project_id)
                if task.tokens + reservation > core.limits(project)["tokens"]:
                    core.ask(db, task, "任务模型预算不足", "先审阅已有结果，必要时调整预算")
                    exhausted = True
                else:
                    task.tokens += reservation
            if exhausted: raise ExternalError("task model budget exhausted")
        usage = None
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(config.llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {config.llm_api_key}"},
                    json={"model": config.llm_model, "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": text}],
                        "tools": [{"type": "function", "function": {
                            "name": "submit", "description": "Submit a structured decision",
                            "parameters": schema}}],
                        "tool_choice": {"type": "function", "function": {"name": "submit"}},
                        "max_tokens": 3000,
                        **({"thinking": {"type": config.llm_thinking}} if config.llm_thinking else {})})
            if response.status_code >= 400: raise ExternalError(f"model HTTP {response.status_code}")
            body = response.json()
            usage = (body.get("usage") or {}).get("total_tokens")
            calls = body["choices"][0]["message"].get("tool_calls") or []
            if len(calls) != 1: raise ExternalError("model did not return one decision")
            return json.loads(calls[0]["function"]["arguments"])
        finally:
            if task_id and usage is not None:
                with transaction() as db:
                    task = core.lock_task(db, task_id)
                    task.tokens = max(0, task.tokens + int(usage) - reservation)

async def feishu(text):
    config = settings()
    if not config.feishu_webhook: return {"status": "not_configured"}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(config.feishu_webhook, json={"msg_type": "text", "content": {"text": text}})
    if response.status_code >= 400: raise ExternalError(f"notification HTTP {response.status_code}")
    payload = response.json()
    if payload.get("code", payload.get("StatusCode", 0)) not in (0, "0"):
        raise ExternalError("notification rejected")
    return {"status": "sent"}

def redact(value):
    text = str(value)
    for secret in (settings().api_token, settings().github_token, settings().llm_api_key, settings().feishu_webhook, settings().feishu_app_secret):
        if secret: text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(authorization|api[_-]?key|token)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
