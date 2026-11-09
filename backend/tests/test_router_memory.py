"""Project-level Router memory: follow-ups, worker reports, decisions and payload injection."""
from sqlalchemy import select
from devflow import core, router, router_state, tickets, triggers
from devflow.db import transaction
from devflow.models import Project, Thread, Trigger


def human_event(identifier="trg-router", thread="github:issue:1", text="please adjust the copy"):
    return {"id": identifier, "source": "github", "kind": "issue_comment.created",
            "actor": "owner", "human": True, "addressed": "mention",
            "thread_keys": [thread], "text": text, "url": "",
            "channel": {"repo": "owner/repo", "number": 1, "is_issue": True, "is_pull": False},
            "dedupe_key": "router-memory:" + identifier, "at": 1_700_000_000.0}


def test_follow_up_is_visible_to_router(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [])
        tickets.subscribe(db, task, "github:issue:1")
        triggers.receive(db, db.get(Project, project), human_event())
        state = router_state.snapshot(db, project)
        assert any(item["kind"] == "follow_up" and task.id in item["tickets"]
                   for item in state["notices"])


def test_worker_report_is_visible_to_router(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [])
        attempt, _ = core.claim_task(db, task.id, "worker")
        core.report(db, attempt.id, {"kind": "progress", "summary": "halfway"}, "progress-1")
        state = router_state.snapshot(db, project)
        assert any(item["kind"] == "worker_report" and item["report"] == "progress"
                   for item in state["notices"])


def test_decision_is_recorded_in_router_memory(project):
    with transaction() as db:
        task = core.create_task(db, project, "Fix", "Fix the login", [])
        tickets.subscribe(db, task, "github:issue:2")
        trigger = Trigger(id="trg-decision", project_id=project, source="github",
                          kind="issue_comment.created", actor="owner", human=True,
                          thread_keys=[f"{project}:github:issue:2"], dedupe_key="decision-trigger")
        db.add(trigger); db.flush()
        thread = db.get(Thread, f"{project}:github:issue:2")
        evaluation = {"id": "eval:test", "thread_key": thread.key,
                      "trigger_ids": [trigger.id], "primary": trigger.id, "strength": "inferred"}
        triggers.decision(db, db.get(Project, project), thread, evaluation, "ignore",
                          {"reason": "ordinary discussion"})
        state = router_state.snapshot(db, project)
        assert any(item["kind"] == "decision" and item["decision"] == "ignore"
                   for item in state["memory"])


def test_candidate_payload_contains_router_memory(project):
    with transaction() as db:
        project_row = db.get(Project, project)
        router_state.record(db, project, "decision",
                            {"decision": "ignore", "thread": "github:issue:3"})
        triggers.receive(db, project_row, human_event("trg-payload", thread="github:issue:3"))
        trigger = db.get(Trigger, "trg-payload")
        payload = router.candidate_payload(db, project_row, trigger)
        assert payload["router"]["memory"]
