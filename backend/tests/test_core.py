import time
import pytest
from sqlalchemy import select
from devflow import core, watcher
from devflow.db import transaction
from devflow.models import Artifact, Attempt, Attention, Dependency, Effect, ExecutionSession, Finding, Inbox, Project, Task, TaskInput

def test_dependencies_priority_and_cycle(project):
    with transaction() as db:
        first = core.create_task(db,project,"API","Implement API",[])
        second = core.create_task(db,project,"UI","Implement UI",[])
        core.add_dependency(db, second.id, first.id)
        assert not core.can_run(db, second)
        core.command(db, second.id, "priority",priority=10)
        assert second.priority == 10
        with pytest.raises(core.Conflict): core.add_dependency(db,first.id,second.id)
        core.transition(db, first,"completed")
        assert core.can_run(db, second)

def test_lease_pause_cancel_and_resume(task):
    with transaction() as db:
        attempt, token = core.claim_task(db,task,"worker")
        assert core.claim_task(db,task,"another") is None
        core.active_attempt(db,attempt.id,token)
        core.command(db,task,"pause")
        with pytest.raises(core.Conflict): core.active_attempt(db,attempt.id,token)
        core.command(db,task,"resume")
        assert core.get(db,Task,task).status == "queued"
        core.command(db,task,"cancel")
        assert core.get(db,Task,task).status == "cancelled"

def test_replacement_preserves_goal_inputs_budget(task):
    with transaction() as db:
        t=core.get(db,Task,task); old=t.session_id; t.tokens=100; t.head_sha="a"*40
        core.input_task(db,task,{"text":"retain this"})
        core.command(db,task,"replace_session")
        assert t.session_id != old
        assert t.tokens == 100 and t.head_sha == "a"*40
        session=core.get(db,ExecutionSession,t.session_id)
        assert session.predecessor_id == old and session.handoff["goal"] == t.goal
        assert db.scalar(select(TaskInput).where(TaskInput.task_id==task)).consumed_at is None

def test_question_answer_dedup_and_budget(task):
    with transaction() as db:
        t=core.get(db,Task,task)
        a=core.ask(db,t,"Use compatibility mode?","Yes","Changes behavior")
        assert core.ask(db,t,"Use compatibility mode?").id == a.id
        assert t.status == "waiting_human"
        core.answer(db,task,a.id,"Yes")
        assert t.status == "queued"
        t.tokens=500000
        assert not core.can_run(db,t)
        assert t.status == "waiting_human"

def test_recovery_retains_session_and_no_polling_model(task):
    with transaction() as db:
        t=core.get(db,Task,task); session=t.session_id
        a,_=core.claim_task(db,task,"worker")
        t.lease_until=time.time()-1; a.last_heartbeat=time.time()-10
    with transaction() as db:
        watcher.run_liveness(db)
        t=core.get(db,Task,task)
        assert t.status == "queued" and t.session_id == session
        assert not list(db.scalars(select(Inbox)))  # Recovery is deterministic.

def test_runner_cannot_verify_itself(task):
    with transaction() as db:
        a,_=core.claim_task(db,task,"worker")
        core.report(db,a.id,{"kind":"ready","sha":"f"*40,"tests_passed":True},"report-1")
        artifacts=list(db.scalars(select(Artifact)))
        assert not any(x.verified for x in artifacts)
        assert core.get(db,Task,task).status == "waiting_external"
        assert db.scalar(select(Effect).where(Effect.kind=="verify"))

def test_router_lease_is_independent_of_project_state(project):
    with transaction() as db:
        job=core.enqueue(db,project,"explicit_candidate",{"trigger_id":"missing"})
        job.status="running";job.owner="router";job.lease_until=time.time()+30
        watcher.run_liveness(db)
        assert job.status == "running" and job.owner == "router"


def test_notifications_deduplicated(task):
    with transaction() as db:
        t=core.get(db,Task,task)
        core.notify(db,t,"review","Review","sha")
        core.notify(db,t,"review","Review","sha")
        assert len(list(db.scalars(select(Effect).where(Effect.key.like("%:review:sha"))))) == 1
