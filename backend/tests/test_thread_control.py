"""Worker-facing thread control: follow a thread for context, drop an unrelated one."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from devflow import core, tickets, triggers
from devflow.api import app_factory
from devflow.db import transaction
from devflow.models import MessageOwnership, Project, Task, Thread


def runner(task_id: str):
    with transaction() as db:
        attempt, token = core.claim_task(db, task_id, "worker")
        return attempt.id, token


def test_runner_can_follow_and_drop_a_context_thread(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [])
        task_id = task.id
    attempt_id, token = runner(task_id)
    c = TestClient(app_factory())
    headers = {"Authorization": "Bearer " + token}
    followed = c.post(f"/internal/attempts/{attempt_id}/threads/follow", headers=headers,
                      json={"thread_key": "github:issue:9", "reason": "related discussion"})
    assert followed.status_code == 200
    with transaction() as db:
        thread = db.get(Thread, f"{project}:github:issue:9")
        assert task_id in thread.subscriptions and task_id in thread.context
    dropped = c.post(f"/internal/attempts/{attempt_id}/threads/unrelated", headers=headers,
                     json={"thread_key": "github:issue:9", "reason": "not this ticket"})
    assert dropped.status_code == 200 and dropped.json()["dropped"] is True
    with transaction() as db:
        thread = db.get(Thread, f"{project}:github:issue:9")
        assert task_id not in thread.subscriptions and task_id not in thread.context


def test_dropping_a_thread_hands_back_owned_messages(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [])
        tickets.subscribe(db, task, "github:issue:10")
        triggers.receive(db, db.get(Project, project), {
            "source": "github", "kind": "issue_comment.created", "actor": "owner",
            "human": True, "addressed": "mention", "thread_keys": ["github:issue:10"],
            "text": "also cover this", "dedupe_key": "thread-drop", "at": 1_700_000_000.0,
            "channel": {"repo": "owner/repo", "number": 10, "is_issue": True, "is_pull": False}})
        ownership = db.scalar(select(MessageOwnership).where(
            MessageOwnership.task_id == task.id))
        assert ownership is not None and ownership.released_at is None
        assert tickets.mark_unrelated(db, task, "github:issue:10", "unrelated")
        assert ownership.released_at is not None


def test_cannot_drop_the_thread_that_started_the_ticket(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [], pr_number=5)
        with pytest.raises(core.Conflict):
            tickets.mark_unrelated(db, task, "github:pr:5", "no")
