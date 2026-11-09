import time
import pytest
from sqlalchemy import select
from devflow import core, delivery, specs, steering, reviewer
from devflow.db import transaction
from devflow.models import Artifact, Effect, PolicyRevision, Project, SpecReceipt, SpecRevision, Task, TaskInput
from test_management import Scripted


def test_task_spec_does_not_advance_other_tasks_or_future_tasks(project):
    with transaction() as db:
        p = db.get(Project, project)
        first = core.create_task(db, project, "a", "a", [])
        second = core.create_task(db, project, "b", "b", [])
        version, count = specs.update(db, p, "only a must preserve this invariant", task_ids=[first.id])
        third = core.create_task(db, project, "c", "c", [])
        assert p.spec_version == 0 and count == 1
        assert [r.text for r in specs.applicable(db, first)] == ["only a must preserve this invariant"]
        assert not specs.applicable(db, second) and not specs.applicable(db, third)
        specs.update(db, p, "all tasks must run regression")
        fourth = core.create_task(db, project, "d", "d", [])
        entry = db.scalar(select(TaskInput).where(TaskInput.task_id == fourth.id))
        assert entry.payload["text"] == "all tasks must run regression"
        receipt = db.scalar(select(SpecReceipt).where(SpecReceipt.task_id == fourth.id))
        assert receipt.delivered_at and receipt.read_at is None
        assert not steering.can_publish(db, fourth, p)


def test_revisions_are_immutable(project):
    with pytest.raises(ValueError, match="immutable"):
        with transaction() as db:
            row = db.scalar(select(PolicyRevision).where(PolicyRevision.project_id == project))
            row.body = {"mode": "auto"}
            db.flush()


async def test_delivery_bound_to_current_input_policy_and_evidence(task, project):
    with transaction() as db:
        t = db.get(Task, task); t.head_sha = "a" * 40
        core.transition(db, t, "waiting_external", "supervision")
        db.add(Artifact(task_id=task, kind="verification", sha=t.head_sha, verified=True, payload={"passed": True}))
    model = Scripted({"aligned": True, "evidence_ids": [], "finding": "", "recommendation": ""})
    assert await reviewer.review_task(task, model, force=True)
    with transaction() as db:
        t = db.get(Task, task)
        assert delivery.eligible(db, t)
        assert db.scalar(select(Effect).where(Effect.kind == "publish"))
        core.input_task(db, task, {"text": "new constraint"}, wake=False)
        assert not delivery.eligible(db, t)


async def test_spec_read_does_not_approve_delivery(task, project):
    with transaction() as db:
        t = db.get(Task, task); p = db.get(Project, project)
        specs.update(db, p, "test the original failure")
        entry = db.scalar(select(TaskInput).where(TaskInput.task_id == task))
        specs.read(db, t, entry.payload)
        assert not specs.complete(db, t)
        specs.applied(db, t)
        assert not specs.complete(db, t)
        t.head_sha = "current"
        core.transition(db, t, "waiting_external", "supervision")
        db.add(Artifact(task_id=task, kind="verification", sha="current", verified=True, payload={"passed": True}))
    await reviewer.review_task(task, Scripted({"aligned": True}), force=True)
    with transaction() as db:
        t = db.get(Task, task)
        assert specs.complete(db, t) and delivery.eligible(db, t)


async def test_analysis_delivers_without_manager(task):
    with transaction() as db:
        t = db.get(Task, task); t.scope = "analysis"
        core.transition(db, t, "waiting_external", "supervision")
        db.add(Artifact(task_id=task, kind="proposal", payload={"summary": "analysis result"}))
    await reviewer.review_task(task, Scripted({"aligned": True}), force=True)
    with transaction() as db:
        assert db.get(Task, task).status == "completed"
        assert not db.scalar(select(Effect).where(Effect.kind == "publish"))
