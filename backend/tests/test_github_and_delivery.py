from sqlalchemy import select
from devflow import core, changes, github_events, effects, delivery, reviewer
from devflow.db import transaction
from devflow.models import Artifact, Effect, Inbox, Project, Task, Trigger
from test_management import Scripted

def deliver(db,project,kind,payload,identifier):
    github_events.intake(db,kind,{"repository":{"full_name":"owner/repo"},**payload},identifier)
    job=db.scalar(select(Inbox).where(Inbox.dedupe_key=="github:"+identifier))
    github_events.process(db,job)
    changes.run_pending(db)

def test_observe_unauthorized_self_and_explicit(project):
    with transaction() as db:
        deliver(db,project,"issues",{"action":"labeled","label":{"name":"agent:run"},
              "issue":{"number":1,"title":"bug","body":"fix"},"sender":{"login":"stranger"}},"1")
        assert not list(db.scalars(select(Task)))
        # The trigger is recorded for audit, but the gate admits no explicit candidate.
        assert db.scalar(select(Trigger))
        assert not db.scalar(select(Inbox).where(Inbox.kind=="explicit_candidate"))
        deliver(db,project,"issues",{"action":"labeled","label":{"name":"agent:run"},
              "issue":{"number":2,"title":"bug","body":"fix"},"sender":{"login":"owner"}},"2")
        assert db.scalar(select(Inbox).where(Inbox.kind=="explicit_candidate"))
        deliver(db,project,"issue_comment",{"action":"created","issue":{"number":3},
              "comment":{"body":"@devflow-bot do work"},"sender":{"login":"devflow-bot"}},"3")
        assert not list(db.scalars(select(Task)))
        # The agent's own event is dropped, so only the two admitted deliveries remain.
        assert len(list(db.scalars(select(Trigger)))) == 2

def test_old_ci_and_cancel_do_not_wake(project,task):
    with transaction() as db:
        t=core.get(db,Task,task);t.head_sha="new";core.transition(db,t,"waiting_external","CI pending")
        deliver(db,project,"workflow_run",{"action":"completed","workflow_run":{"id":1,"head_sha":"old",
              "status":"completed","conclusion":"success"}},"old")
        assert t.status=="waiting_external"
        core.command(db,task,"cancel")
        deliver(db,project,"workflow_run",{"action":"completed","workflow_run":{"id":2,"head_sha":"new",
              "status":"completed","conclusion":"failure"}},"new")
        assert t.status=="cancelled"

class Driver:
    def verify(self,task,project): return {"sha":"a"*40,"passed":True,"commands":[{"command":"pytest","exit_code":0}]}
    def push(self,task,project): return "pushed"

class GitHub:
    def __init__(self):self.comments={};self.prs={};self.ci="success"
    async def comment(self,repo,number,body,key):
        self.comments.setdefault(key,{"id":len(self.comments)+1});return self.comments[key]
    async def pull_request(self,project,task,body):
        self.prs.setdefault(task.id,{"number":12});return self.prs[task.id]
    async def request(self,method,path,**kwargs):
        if path.endswith("/actions/runs"):return {"total_count":1,"workflow_runs":[{"id":1,"workflow_id":1,"head_sha":"a"*40 if "params" not in kwargs else kwargs["params"]["head_sha"],"status":"completed","conclusion":self.ci}]}
        if path.endswith("/status"):return {"total_count":0,"state":"pending"}
        raise AssertionError(path)

async def test_development_close_loop_with_host_verification(task,project,monkeypatch):
    github=GitHub();driver=Driver()
    with transaction() as db:
        a,_=core.claim_task(db,task,"worker")
        core.report(db,a.id,{"kind":"question","question":"Keep compatibility?","recommendation":"yes"},"q")
        from devflow.models import Attention
        attention=db.scalar(select(Attention))
        core.answer(db,task,attention.id,"yes")
        a,_=core.claim_task(db,task,"worker")
        core.report(db,a.id,{"kind":"ready","summary":"implemented"},"ready")
        verify=db.scalar(select(Effect).where(Effect.kind=="verify"));verify.owner="e"
        verify_id=verify.id
    await effects.execute(verify_id,"e",github,driver)
    with transaction() as db:
        t=core.get(db,Task,task)
        assert t.head_sha=="a"*40 and t.wait_reason=="supervision"
        proposal=db.scalar(select(Artifact).where(Artifact.kind=="verification"))
        assert proposal.verified
    await reviewer.review_task(task, Scripted({"aligned":True,"finding":"","evidence_ids":[],"recommendation":""}), force=True)
    with transaction() as db:
        publish=db.scalar(select(Effect).where(Effect.kind=="publish"));publish.owner="p";pid=publish.id
    await effects.execute(pid,"p",github,driver)
    await effects.execute(pid,"p",github,driver)  # replay reconciles by branch/task
    assert len(github.prs)==1
    with transaction() as db:
        ci=db.scalar(select(Effect).where(Effect.kind=="check_ci"));ci.owner="c";cid=ci.id
    await effects.execute(cid,"c",github,driver)
    with transaction() as db:
        t=core.get(db,Task,task)
        assert t.status=="ready_for_review"
        deliver(db,project,"pull_request",{"action":"closed","pull_request":{"number":12,"merged":True},
              "sender":{"login":"owner"}},"merged")
        assert t.status=="completed"

async def test_ci_absence_not_success(task,project):
    class NoCI(GitHub):
        async def request(self,method,path,**kwargs):
            return {"total_count":0,"workflow_runs":[]} if path.endswith("/actions/runs") else {"total_count":0}
    assert await effects.check_ci(NoCI(),"owner/repo","a"*40) == "missing"


def test_multiple_ci_event_types_count_one_repair_round(task,project):
    with transaction() as db:
        t=core.get(db,Task,task);t.head_sha="current";core.transition(db,t,"waiting_external","CI pending")
        deliver(db,project,"check_run",{"action":"completed","check_run":{"id":30,"head_sha":"current",
            "status":"completed","conclusion":"failure"}},"check")
        deliver(db,project,"workflow_run",{"action":"completed","workflow_run":{"id":31,"head_sha":"current",
            "status":"completed","conclusion":"failure"}},"workflow")
        assert t.status=="queued" and t.ci_repairs==1

async def test_current_ci_cannot_replace_host_verification(task,project):
    with transaction() as db:
        t=core.get(db,Task,task);t.head_sha="external-current";core.transition(db,t,"waiting_external","CI pending")
        e=core.effect(db,project,"check_ci","current-ci",{"sha":t.head_sha},task);e.owner="ci";eid=e.id
    await effects.execute(eid,"ci",GitHub(),Driver())
    with transaction() as db:assert core.get(db,Task,task).status=="queued"


async def test_cancel_during_publication_remains_responsive(task,project):
    import asyncio
    started=asyncio.Event();release=asyncio.Event()
    class DelayedGitHub(GitHub):
        async def pull_request(self,project,task,body):
            started.set();await release.wait()
            return {"number":44}
    with transaction() as db:
        t=core.get(db,Task,task);t.head_sha="d"*40;core.transition(db,t,"waiting_external","supervision")
        db.add(Artifact(task_id=task,kind="verification",sha=t.head_sha,verified=True,payload={"passed":True}))
        db.flush()
        from devflow.models import ReviewRecord
        data = delivery.snapshot(db, t)
        db.add(ReviewRecord(task_id=task, fingerprint=delivery.fingerprint(data), sha=t.head_sha,
                            aligned=True, snapshot=data, result={"aligned": True}))
        db.flush()
        e=core.effect(db,project,"publish","delayed-publish",{"sha":t.head_sha},task);e.owner="pub";eid=e.id
    publishing=asyncio.create_task(effects.execute(eid,"pub",DelayedGitHub(),Driver()))
    await asyncio.wait_for(started.wait(),5)
    with transaction() as db:core.command(db,task,"cancel")
    release.set();await asyncio.wait_for(publishing,5)
    with transaction() as db:
        t=core.get(db,Task,task)
        assert t.status=="cancelled" and t.pr_number==44
        assert core.get(db,Effect,eid).status=="sent"


async def test_pat_ci_latest_attempt_and_current_sha():
    class ActionsOnly:
        def __init__(self):
            self.runs = [
                {"id": 9, "workflow_id": 1, "head_sha": "current", "run_attempt": 1, "status": "completed", "conclusion": "failure"},
                {"id": 9, "workflow_id": 1, "head_sha": "current", "run_attempt": 2, "status": "completed", "conclusion": "success"},
                {"id": 10, "workflow_id": 2, "head_sha": "old", "status": "completed", "conclusion": "failure"}]
            self.truncated = False
            self.commit_state = None
        async def request(self, method, path, **kwargs):
            if path.endswith("/actions/runs"):
                assert kwargs["params"]["head_sha"] == "current"
                return {"total_count": 101 if self.truncated else len(self.runs), "workflow_runs": self.runs}
            assert path.endswith("/status")
            return {"total_count": bool(self.commit_state), "state": self.commit_state}
    github = ActionsOnly()
    assert await effects.check_ci(github, "owner/repo", "current") == "success"
    github.runs.append({"id": 11, "workflow_id": 2, "head_sha": "current", "status": "in_progress"})
    assert await effects.check_ci(github, "owner/repo", "current") == "pending"
    github.commit_state = "failure"
    assert await effects.check_ci(github, "owner/repo", "current") == "failure"
    github.truncated = True
    assert await effects.check_ci(github, "owner/repo", "current") == "pending"
