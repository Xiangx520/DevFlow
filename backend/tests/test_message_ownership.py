from sqlalchemy import select
from devflow import core, tickets, triggers, ownership
from devflow.db import transaction
from devflow.models import Project, Thread, Trigger, MessageOwnership


def setup_message(db, project):
    p = db.get(Project, project)
    first = core.create_task(db, project, "First", "First", [])
    second = core.create_task(db, project, "Second", "Second", [])
    tickets.subscribe(db, first, "github:issue:7")
    tickets.subscribe(db, second, "github:issue:7")
    triggers.receive(db, p, {"source": "github", "kind": "issue_comment.created",
        "actor": "owner", "human": True, "thread_keys": ["github:issue:7"],
        "channel": {"repo": "owner/repo", "number": 7, "is_issue": True},
        "text": "additional work", "dedupe_key": "message-7"})
    return first, second, db.scalar(select(Trigger))


def test_handback_waits_for_all_owners_and_keeps_subscription(project):
    with transaction() as db:
        first, second, trigger = setup_message(db, project)
        assert tickets.handback(db, first, trigger.id, "outside scope")
        thread = db.get(Thread, f"{project}:github:issue:7")
        assert not thread.pending
        assert first.id in thread.subscriptions
        assert tickets.handback(db, second, trigger.id, "outside scope")
        assert thread.pending == [trigger.id]
        assert tickets.add_input(db, first, kind="trigger", trigger_id=trigger.id) is None
        assert len(list(db.scalars(select(MessageOwnership)))) == 2


def test_unread_messages_release_only_after_last_task_ends(project):
    with transaction() as db:
        first, second, trigger = setup_message(db, project)
        tickets.end(db, first)
        assert not db.get(Thread, f"{project}:github:issue:7").pending
        tickets.end(db, second)
        assert db.get(Thread, f"{project}:github:issue:7").pending == [trigger.id]


def test_read_receipt_preserves_ownership_on_end(project):
    with transaction() as db:
        first, second, trigger = setup_message(db, project)
        owner = db.get(MessageOwnership, (trigger.id, first.id))
        owner.read_at = 1
        tickets.end(db, first)
        tickets.end(db, second)
        assert owner.processed_at is None
        assert owner.released_at is None
        assert not db.get(Thread, f"{project}:github:issue:7").pending


def test_webhook_and_poll_share_identity_but_edits_do_not():
    from devflow.github_events import business_identity
    payload = {"action": "created", "comment": {"id": 7, "updated_at": "v1", "body": "hello"}}
    first = business_identity("project", "issue_comment", payload)
    payload["action"] = "submitted"
    assert business_identity("project", "pull_request_review_comment", payload) == first
    payload["comment"]["updated_at"] = "v2"
    assert business_identity("project", "issue_comment", payload) != first
    assert business_identity("another", "issue_comment", payload) != first


def test_project_scoped_keys_and_receipts(project):
    from devflow.thread_keys import make, parse
    with transaction() as db:
        other = core.new_project(db, "other/repo", {"allowed_actors": ["owner"]}, {"verify_commands": ["true"]})
        data = {"source": "github", "kind": "issue_comment.created", "actor": "owner",
                "thread_keys": ["github:issue:7"], "channel": {"is_issue": True},
                "text": "fix", "dedupe_key": "same-delivery"}
        for identifier in [project, other.id]:
            assert triggers.receive(db, db.get(Project, identifier), data) == "stored"
            key = make(identifier, "github", "issue", 7)
            assert parse(key) == (identifier, "github", "issue", "7")
            assert db.get(Thread, key).project_id == identifier
        import pytest
        with pytest.raises(core.Conflict):
            tickets.subscribe(db, core.create_task(db, project, "x", "x", []), make(other.id, "github", "pr", 7))


def test_closure_waits_for_running_round_and_followup(project):
    with transaction() as db:
        first, second, trigger = setup_message(db, project)
        first.pr_number = 9
        attempt, _ = core.claim_task(db, first.id, "worker")
        owner = db.get(MessageOwnership, (trigger.id, first.id))
        owner.read_at = 1
        tickets.thread_closed(db, db.get(Project, project), "github:pr:9", "merged")
        assert first.status == "running"
        assert first.follows[0]["closed"] == "merged"
        core.report(db, attempt.id, {"kind": "ready", "summary": "followup complete"}, "ready")
        assert owner.processed_at
        assert first.status == "completed"
        assert second.status == "queued"
