"""Human follow-ups win the scheduler and may use one extra worker slot."""
from devflow import core
from devflow.config import settings
from devflow.db import transaction
from devflow.models import MessageOwnership, Project, Task, Trigger
from devflow.worker import schedule_one


def make_project(name):
    with transaction() as db:
        return core.new_project(db, name, {"allowed_actors": ["owner"]},
                                {"verify_commands": ["true"]}).id


def make_task(project, title="Work"):
    with transaction() as db:
        return core.create_task(db, project, title, title, []).id


def mark_human(task_id):
    with transaction() as db:
        trigger = Trigger(project_id=db.get(Task, task_id).project_id, source="github",
                          kind="issue_comment.created", actor="owner", human=True,
                          thread_keys=[], dedupe_key="scheduler:" + task_id)
        db.add(trigger); db.flush()
        db.add(MessageOwnership(trigger_id=trigger.id, task_id=task_id))


async def test_human_follow_up_wins_normal_slots(monkeypatch):
    monkeypatch.setenv("DEVFLOW_MAX_CONCURRENCY", "2")
    settings.cache_clear()
    first = make_task(make_project("owner/one"), "first")
    second = make_task(make_project("owner/two"), "second")
    mark_human(second)
    claimed = await schedule_one()
    assert claimed is not None
    with transaction() as db:
        assert db.get(Task, second).status == "running"
        assert db.get(Task, first).status == "queued"


async def test_human_follow_up_uses_one_extra_slot(monkeypatch):
    monkeypatch.setenv("DEVFLOW_MAX_CONCURRENCY", "1")
    settings.cache_clear()
    running = make_task(make_project("owner/three"), "running")
    with transaction() as db:
        assert core.claim_task(db, running, "worker")
    human = make_task(make_project("owner/four"), "human")
    mark_human(human)
    assert (await schedule_one()) is not None
    with transaction() as db:
        assert db.get(Task, human).status == "running"
    extra = make_task(make_project("owner/five"), "extra")
    mark_human(extra)
    assert (await schedule_one()) is None
