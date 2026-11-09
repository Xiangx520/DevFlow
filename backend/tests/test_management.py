import pytest
from sqlalchemy import select
from devflow import core, watcher
from devflow.db import transaction
from devflow.models import Artifact, Decision, Finding, Inbox, Project, Task, TaskInput

class Scripted:
    def __init__(self,result): self.result=result;self.calls=0
    async def json(self,*args,**kwargs): self.calls+=1;return self.result

async def test_negative_finding_requires_real_evidence(task):
    with transaction() as db:
        db.add(Artifact(task_id=task,kind="progress",payload={"summary":"working"}))
    model=Scripted({"aligned":False,"finding":"bad","evidence_ids":["invented"],"recommendation":"replace"})
    with pytest.raises(core.Conflict): await watcher.review_task(task,model,True)

async def test_watcher_failure_is_rate_limited(task):
    class Broken:
        calls=0
        async def json(self,*args,**kwargs):
            self.calls+=1
            raise RuntimeError("provider unavailable")
    with transaction() as db:db.add(Artifact(task_id=task,kind="progress",payload={"summary":"working"}))
    model=Broken()
    with pytest.raises(RuntimeError):await watcher.review_task(task,model,True)
    assert await watcher.review_task(task,model,True) is False
    assert model.calls==1



def test_watchdog_control_is_durable_and_stop_revokes_capability(task):
    from devflow import control
    from devflow.models import Control
    with transaction() as db:
        attempt, token = core.claim_task(db, task, "worker")
        row = control.submit(db, db.get(Task, task), "correction", {"text": "fix login only"}, "watchdog")
        assert row.status == "delivered" and db.get(TaskInput, row.input_id)
        stop = control.submit(db, db.get(Task, task), "stop")
        assert stop.status == "applied"
        assert db.get(Task, task).ended_by == "human"
        with pytest.raises(core.Conflict):
            core.active_attempt(db, attempt.id, token)
        assert len(list(db.scalars(select(Control)))) == 2


def test_two_corrections_then_attention(task):
    from devflow.models import Attention, Control
    with transaction() as db:
        t = db.get(Task, task)
        for index in range(3):
            watcher.finding(db, t, f"evidence:{index}", "direction", {"index": index}, "fix login")
        assert len(list(db.scalars(select(Control)))) == 2
        assert db.scalar(select(Attention))
        assert t.status == "waiting_human"
