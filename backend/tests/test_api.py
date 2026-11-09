import hashlib
import hmac
import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from devflow.api import app_factory
from devflow.config import settings
from devflow.db import transaction
from devflow.models import Inbox, Task

def client(): return TestClient(app_factory(),headers={"Authorization":"Bearer owner-test-token"})

def test_auth_and_idempotency(project):
    c=client()
    assert TestClient(app_factory()).get("/api/v1/projects").status_code == 401
    body={"project_id":project,"title":"Bug","goal":"Fix bug","acceptance":["test"]}
    one=c.post("/api/v1/tasks",json=body,headers={"Idempotency-Key":"same"})
    assert one.status_code == 201
    two=c.post("/api/v1/tasks",json=body,headers={"Idempotency-Key":"same"})
    assert one.json()["id"] == two.json()["id"]
    body["goal"]="different"
    assert c.post("/api/v1/tasks",json=body,headers={"Idempotency-Key":"same"}).status_code == 409

def test_conversation_async_and_task_scope(project,task):
    c=client()
    conv=c.post("/api/v1/conversations",json={"project_id":project}).json()["id"]
    result=c.post(f"/api/v1/conversations/{conv}/messages",json={"text":"prioritize login","task_id":task},
                  headers={"Idempotency-Key":"message-1"})
    assert result.status_code == 202
    events=c.get(f"/api/v1/conversations/{conv}/events").json()
    assert events[-1]["payload"]["content"] == "prioritize login"

def test_webhook_signed_and_deduplicated(project):
    c=client()
    payload=json.dumps({"repository":{"full_name":"owner/repo"},"action":"opened","issue":{"number":1}})
    signature="sha256="+hmac.new(b"webhook-test-secret",payload.encode(),hashlib.sha256).hexdigest()
    headers={"X-Github-Event":"issues","X-Github-Delivery":"delivery-1","X-Hub-Signature-256":signature}
    assert c.post("/api/v1/webhooks/github",content=payload,headers=headers).status_code==202
    assert c.post("/api/v1/webhooks/github",content=payload,headers=headers).json()["duplicate"]
    headers["X-Hub-Signature-256"]="wrong"
    assert c.post("/api/v1/webhooks/github",content=payload,headers=headers).status_code==401

def test_no_secrets_in_public_attempts(task):
    from devflow import core
    with transaction() as db: attempt,secret=core.claim_task(db,task,"test")
    data=client().get(f"/api/v1/tasks/{task}/attempts").json()
    assert "runner_token_hash" not in data[0]
    assert secret not in json.dumps(data)


def test_task_events_and_conversation_pagination(project,task):
    from devflow import core
    c=client()
    conv=c.post("/api/v1/conversations",json={"project_id":project}).json()["id"]
    with transaction() as db:
        for i in range(205):core.event(db,project,"unrelated",{"n":i})
        core.event(db,project,"conversation.message",{"conversation_id":conv,"content":"visible"},task)
    assert c.get(f"/api/v1/tasks/{task}/events").status_code==200
    events=c.get(f"/api/v1/conversations/{conv}/events").json()
    assert len(events)==1 and events[0]["payload"]["content"]=="visible"


def test_runner_github_pagination_and_revocation(task,monkeypatch):
    from devflow import api,core
    seen=[]
    class FakeGitHub:
        async def request(self,method,path,**kwargs):
            seen.append((path,kwargs));return [{"filename":"test.py"}]
    monkeypatch.setattr(api,"GitHub",FakeGitHub)
    with transaction() as db:a,token=core.claim_task(db,task,"runner")
    headers={"Authorization":"Bearer "+token}
    c=client()
    result=c.get(f"/internal/attempts/{a.id}/github/pulls/1/files?page=2",headers=headers)
    assert result.status_code==200 and result.json()["page"]==2
    assert seen[0][0]=="/repos/owner/repo/pulls/1/files"
    assert seen[0][1]["params"]["page"]==2
    assert c.get(f"/internal/attempts/{a.id}/github/issues/1/labels",headers=headers).status_code==403
    with transaction() as db:core.command(db,task,"cancel")
    assert c.get(f"/internal/attempts/{a.id}/github/pulls/1/files",headers=headers).status_code==403


def test_frontend_queries_are_project_scoped(project, task):
    from devflow import core
    from devflow.models import Conversation, Message
    c = client()
    with transaction() as db:
        other = core.new_project(db, 'other/repo', {'allowed_actors':['other']}, {'verify_commands':['pytest']})
        foreign = Conversation(project_id=other.id); db.add(foreign)
        local = Conversation(project_id=project); db.add(local); db.flush()
        core.ask(db, core.get(db, Task, task), 'Choose approach?', 'Use small change')
        for i in range(205):
            db.add(Message(conversation_id=local.id, role='user', content=str(i), created_at=1000+i))
        local_id, other_id = local.id, other.id
    assert [x['id'] for x in c.get('/api/v1/conversations', params={'project_id':project}).json()] == [local_id]
    assert len(c.get(f'/api/v1/projects/{project}/attentions').json()) == 1
    assert c.get(f'/api/v1/projects/{other_id}/attentions').json() == []
    newest = c.get(f'/api/v1/conversations/{local_id}/messages').json()
    assert len(newest) == 100 and newest[-1]['content'] == '204'
    older = c.get(f'/api/v1/conversations/{local_id}/messages', params={'before':newest[0]['created_at']}).json()
    assert len(older) == 100 and older[-1]['content'] == '104'
    assert c.get('/api/v1/conversations/missing/messages').status_code == 404
    assert c.get('/api/v1/projects/missing/attentions').status_code == 404
    assert TestClient(app_factory()).get('/api/v1/conversations', params={'project_id':project}).status_code == 401


def test_message_cursor_handles_equal_timestamps(project):
    from devflow.models import Conversation, Message
    c = client()
    with transaction() as db:
        conv=Conversation(project_id=project); db.add(conv); db.flush()
        conv_id=conv.id
        for i in range(105):
            db.add(Message(id=f"message-{i:03}",conversation_id=conv_id,role="user",content=str(i),created_at=1000))
    newest=c.get(f"/api/v1/conversations/{conv_id}/messages").json()
    older=c.get(f"/api/v1/conversations/{conv_id}/messages",params={"before_id":newest[0]["id"]}).json()
    assert len(newest)==100 and len(older)==5
    assert len({m["id"] for m in newest+older})==105
