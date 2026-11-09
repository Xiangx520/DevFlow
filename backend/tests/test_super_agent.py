"""Super-agent core: gate, trigger fan-out, router, assessment, control plane and policy changes."""
import time
import pytest
from sqlalchemy import select
from devflow import assessment, core, policy, router, steering, tickets, triggers
from devflow.db import transaction
from devflow.models import (Decision, Effect, Inbox, Project, Task, TaskInput, Thread,
                            Trigger)


class Scripted:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def json(self, *args, **kwargs):
        self.calls += 1
        return self.result


def test_closing_pr_does_not_end_unrelated_task(project):
    with transaction() as db:
        p = core.get(db, Project, project)
        first = core.create_task(db, project, "First", "First", [], pr_number=7)
        second = core.create_task(db, project, "Second", "Second", [], pr_number=8)
        tickets.thread_closed(db, p, "github:pr:7", "merged")
        assert first.status == "completed"
        assert second.status == "queued"


def test_discussion_is_evaluated_after_maximum_wait(project):
    configure(project, source_scopes={"feishu": {"chats": {"group": {"receive": "all"}}}})
    with transaction() as db:
        p = core.get(db, Project, project)
        triggers.receive(db, p, event("feishu:chat:group", source="feishu", actor="owner",
                                     channel={"chat_id": "group"}, dedupe="max-wait"))
        row = db.scalar(select(Trigger).where(Trigger.dedupe_key == project + ":max-wait"))
        row.created_at = time.time() - 61
        db.get(Thread, f"{project}:feishu:chat:group").last_at = time.time()
        db.flush()
        assert triggers.evaluate_due(db, p)


def test_router_rechecks_authorization_after_model(project):
    import asyncio
    configure(project)
    with transaction() as db:
        triggers.receive(db, core.get(db, Project, project),
                         event("github:issue:77", addressed="mention", number=77))

    class RevokingModel:
        async def json(self, system, payload, *args, **kwargs):
            with transaction() as db:
                p = core.get(db, Project, project)
                value = policy.normalize(p.policy)
                value["allowed_actors"] = []
                p.policy = value
                p.policy_version = policy.version(value)
            return {"items": [{"trigger_id": payload["trigger"]["trigger_id"],
                               "kind": "accept", "title": "Fix", "goal": "Fix"}]}

    asyncio.run(router.route_one(RevokingModel()))
    with transaction() as db:
        assert not list(db.scalars(select(Task)))


def configure(project, **changes):
    with transaction() as db:
        p = core.get(db, Project, project)
        value = policy.normalize(p.policy)
        value.update(changes)
        p.policy = policy.normalize(value)
        p.policy_version = policy.version(p.policy)
        return p.policy_version


def event(thread="github:issue:1", *, actor="owner", human=True, addressed=None,
          kind="issue_comment.created", source="github", text="please make the button say Save",
          number=1, is_issue=True, is_pull=False, channel=None, dedupe=None):
    return {"source": source, "kind": kind, "actor": actor, "human": human,
            "addressed": addressed, "thread_keys": [thread], "text": text,
            "channel": channel or {"repo": "owner/repo", "number": number,
                                   "is_issue": is_issue, "is_pull": is_pull},
            "dedupe_key": dedupe or f"{thread}:{time.time()}:{text[:10]}",
            "url": ""}


def test_manual_route_is_idempotent_without_issue_number(project):
    configure(project)
    with transaction() as db:
        p = core.get(db, Project, project)
        triggers.receive(db, p, {
            "source": "feishu", "kind": "feishu.message", "actor": "owner", "human": True,
            "addressed": "mention", "thread_keys": ["feishu:chat:manual"], "text": "do the thing",
            "dedupe_key": "manual-idem", "channel": {"chat_id": "manual"}})
        row = db.scalar(select(Trigger).where(Trigger.dedupe_key == project + ":manual-idem"))
        first = router.manual_route(db, p, row, router.RouteItem(
            trigger_id=row.id, kind="accept", title="One", goal="Do the thing"))
        second = router.manual_route(db, p, row, router.RouteItem(
            trigger_id=row.id, kind="accept", title="Two", goal="Do the thing"))
        assert first is not None and second is not None and first.id == second.id
        assert len(list(db.scalars(select(Task)))) == 1


def test_gate_strength_and_scope(project):
    configure(project, mode="observe",
              source_scopes={"github": {"issues": "all", "pulls": "all", "ci": True}})
    with transaction() as db:
        p = core.get(db, Project, project)
        explicit = policy.gate(p, event("github:issue:1", actor="owner", addressed="mention"))
        assert explicit["admissible"] and explicit["strength"] == "explicit"
        inferred = policy.gate(p, event("github:issue:2", actor="owner", addressed=None))
        assert inferred["admissible"] and inferred["strength"] == "inferred"
        stranger = policy.gate(p, event("github:issue:3", actor="stranger", addressed=None))
        assert not stranger["admissible"]
        bot = policy.gate(p, event("github:issue:4", actor="dependabot[bot]", human=False))
        assert not bot["admissible"]
        ci = policy.gate(p, event("github:issue:5", actor="", human=False,
                                  kind="ci.completed"))
        assert ci["admissible"] and ci["strength"] == "inferred"


def test_scope_mentions_ignores_unaddressed(project):
    configure(project, source_scopes={"github": {"issues": "mentions", "pulls": "all", "ci": True}})
    with transaction() as db:
        p = core.get(db, Project, project)
        assert not policy.gate(p, event("github:issue:1", actor="owner", addressed=None))["admissible"]
        assert policy.gate(p, event("github:issue:1", actor="owner", addressed="mention"))["admissible"]


def test_subscription_fanout_grants_context_not_authority(project):
    configure(project)
    with transaction() as db:
        task = core.create_task(db, project, "Fix login", "Fix login", ["test"])
        tickets.subscribe(db, task, "github:pr:9")
        result = triggers.receive(db, core.get(db, Project, project),
                                  event("github:pr:9", actor="stranger", human=True,
                                        addressed=None, is_issue=False, is_pull=True,
                                        number=9, text="looks unrelated"))
        assert result == "stored"
        assert db.scalar(select(TaskInput).where(TaskInput.task_id == task.id,
                                                 TaskInput.kind == "trigger"))
        assert len(list(db.scalars(select(Task)))) == 1
        assert not db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate"))


def test_router_accept_creates_ticket_with_mandate(project):
    configure(project, mode="auto")
    with transaction() as db:
        p = core.get(db, Project, project)
        triggers.receive(db, p, event("github:issue:11", actor="owner", addressed="mention",
                                      number=11, is_issue=True))
        assert db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate"))
    model = Scripted({"items": [{"trigger_id": "", "kind": "accept", "title": "Fix login",
                                 "goal": "Fix expired login", "reason": "explicit"}]})
    with transaction() as db:
        job = db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate"))
        model.result["items"][0]["trigger_id"] = job.payload["trigger_id"]
    assert __import__("asyncio").run(router.route_one(model))
    with transaction() as db:
        task = db.scalar(select(Task))
        assert task is not None and task.title == "Fix login"
        assert task.mandate["strength"] == "explicit"
        decision = db.scalar(select(Decision).where(Decision.kind == "accept"))
        assert decision is not None and decision.result["ticket_id"] == task.id
        assert db.get(Thread, f"{project}:github:issue:11").subscriptions == [task.id]


def test_router_hardcoded_issue_number_attaches(project):
    configure(project)
    with transaction() as db:
        task = core.create_task(db, project, "Existing", "Existing work", ["test"],
                                issue_number=7)
        p = core.get(db, Project, project)
        triggers.receive(db, p, event("github:issue:7", actor="owner", addressed="mention",
                                      number=7, is_issue=True, text="more detail"))
    model = Scripted({"items": []})
    assert __import__("asyncio").run(router.route_one(model))
    with transaction() as db:
        assert len(list(db.scalars(select(Task)))) == 1
        assert db.scalar(select(TaskInput).where(TaskInput.task_id == task.id,
                                                 TaskInput.kind == "trigger"))
        assert not db.scalar(select(Decision).where(Decision.kind == "accept"))


def test_feishu_passive_discussion_is_an_inferred_candidate(project, monkeypatch):
    from devflow.config import settings
    from devflow.feishu_receiver import bind_group, receive
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test")
    settings.cache_clear()
    with transaction() as db:
        bind_group(db, project, "cli_test", "oc_chat", "ou_bot", ["ou_owner"])
        p = core.get(db, Project, project)
        value = policy.normalize(p.policy)
        value["source_scopes"]["feishu"]["chats"]["oc_chat"] = {"receive": "all"}
        value["rules"] = [{"id": "copy", "name": "Copy", "enabled": True,
                           "instruction": "Correct wording."}]
        p.policy = policy.normalize(value)
        p.policy_version = policy.version(p.policy)
    payload = {"event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                         "message": {"message_id": "om_discuss", "chat_id": "oc_chat",
                                     "chat_type": "group", "message_type": "text",
                                     "content": '{"text":"the empty state copy feels off, maybe say No tasks yet"}'}}}
    assert receive(payload)
    with transaction() as db:
        trigger = db.scalar(select(Trigger))
        assert trigger.source == "feishu" and trigger.addressed is None
        assert trigger.thread_keys == [f"{project}:feishu:chat:oc_chat"]
        p = core.get(db, Project, project)
        verdict = policy.gate(p, trigger, {"subscriptions": [], "context": []}, [])
        assert verdict["admissible"] and verdict["strength"] == "inferred"
        thread = db.get(Thread, f"{project}:feishu:chat:oc_chat")
        thread.last_at = time.time() - 60
        started = triggers.evaluate_due(db, p)
        assert started


def test_feishu_mentions_scope_filters_unaddressed(project, monkeypatch):
    from devflow.config import settings
    from devflow.feishu_receiver import bind_group, receive
    monkeypatch.setenv("DEVFLOW_FEISHU_APP_ID", "cli_test")
    settings.cache_clear()
    with transaction() as db:
        bind_group(db, project, "cli_test", "oc_chat", "ou_bot", ["ou_owner"])
    payload = {"event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                         "message": {"message_id": "om_chat", "chat_id": "oc_chat",
                                     "chat_type": "group", "message_type": "text",
                                     "content": '{"text":"random discussion"}'}}}
    assert receive(payload)
    with transaction() as db:
        trigger = db.scalar(select(Trigger))
        assert trigger.disposition["filtered"]


def _prepare_inferred_eval(project, mode):
    configure(project, mode=mode, rules=[{"id": "copy", "name": "Copy fixes", "enabled": True,
                                          "instruction": "Correct a typo or wording."}])
    with transaction() as db:
        p = core.get(db, Project, project)
        triggers.receive(db, p, event("github:issue:21", actor="owner", addressed=None,
                                      number=21, is_issue=True,
                                      text="this button label is odd; just Save would do",
                                      dedupe="assess-21"))
        thread = db.get(Thread, f"{project}:github:issue:21")
        thread.last_at = time.time() - 60
        started = triggers.evaluate_due(db, p)
        assert started
        effect = db.scalar(select(Effect).where(Effect.kind == "assessment"))
        assert effect is not None
        return effect.payload["eval_id"]


def test_assessment_observe_records_would_accept(project):
    eval_id = _prepare_inferred_eval(project, "observe")
    model = Scripted({"decision": "accept", "rule_assessments": [
        {"rule_id": "copy", "matched": True, "reason": "label wording"}],
        "direction": {"implementation_requested": True, "target_basis": "specified",
                      "requires_design_decision": False, "evidence": "just Save"},
        "title": "Change label", "scope": "Change the button label to Save",
        "intent_evidence": "just 'Save' would do", "acknowledgement": "已调整为 Save",
        "reason": "concrete copy fix"})
    assert __import__("asyncio").run(assessment.evaluate(eval_id, model))
    with transaction() as db:
        assert not list(db.scalars(select(Task)))
        decision = db.scalar(select(Decision).where(Decision.kind == "ignore"))
        assert decision.audit["would_accept"] is True


def test_assessment_auto_accepts_and_creates_ticket(project):
    eval_id = _prepare_inferred_eval(project, "auto")
    model = Scripted({"decision": "accept", "rule_assessments": [
        {"rule_id": "copy", "matched": True, "reason": "label wording"}],
        "direction": {"implementation_requested": True, "target_basis": "specified",
                      "requires_design_decision": False, "evidence": "just Save"},
        "title": "Change label", "scope": "Change the button label to Save",
        "intent_evidence": "just 'Save' would do", "acknowledgement": "已调整为 Save",
        "reason": "concrete copy fix"})
    assert __import__("asyncio").run(assessment.evaluate(eval_id, model))
    with transaction() as db:
        task = db.scalar(select(Task))
        assert task is not None and task.mandate["strength"] == "inferred"
        decision = db.scalar(select(Decision).where(Decision.kind == "accept"))
        assert decision.result["ticket_id"] == task.id


def test_spec_update_steers_live_session_and_gates_publish(project):
    configure(project)
    with transaction() as db:
        task = core.create_task(db, project, "Fix login", "Fix login", ["test"])
        p = core.get(db, Project, project)
        version, delivered = steering.spec_update(db, p, "Always run the full login regression.")
        assert version == 1 and delivered == 1
        assert db.scalar(select(TaskInput).where(TaskInput.task_id == task.id,
                                                 TaskInput.kind == "steer"))
        assert not steering.can_publish(db, task, p)
        steering.acknowledge(db, task, version)
        db.flush()
        assert not steering.can_publish(db, task, p)  # Reading is not application or verification.
        from devflow import specs
        from devflow.models import SpecReceipt
        receipt = db.scalar(select(SpecReceipt).where(SpecReceipt.task_id == task.id))
        receipt.read_at = time.time()
        specs.applied(db, task)
        specs.verify(db, task)
        assert steering.can_publish(db, task, p)


def test_policy_change_delivers_to_live_ticket(project):
    configure(project, mode="auto", rules=[{"id": "copy", "name": "Copy", "enabled": True,
                                            "instruction": "Fix copy."}])
    with transaction() as db:
        task = core.create_task(db, project, "Fix login", "Fix login", ["test"],
                                mandate={"strength": "inferred", "policy_version": "",
                                         "basis": {"via": "admission", "cell": "listedUnaddressed",
                                                   "rules": ["copy"], "source": "github",
                                                   "scope": "github:issues"},
                                         "actor": "owner", "scope": "implement"})
        p = core.get(db, Project, project)
        old = dict(p.policy)
        changed = policy.normalize(old)
        changed["rules"][0]["instruction"] = "Fix copy and keep the product promise."
        p.policy = policy.normalize(changed)
        version, delivered, requeued = steering.policy_changed(db, p, old)
        assert delivered == 1
        assert db.scalar(select(TaskInput).where(TaskInput.task_id == task.id,
                                                 TaskInput.kind == "policy-changed"))


async def test_policy_version_change_discards_router_result(project):
    configure(project)
    with transaction() as db:
        triggers.receive(db, db.get(Project, project), event("github:issue:88", addressed="mention", number=88))
    class Changing:
        async def json(self, system, payload, *args, **kwargs):
            configure(project, rules=[{"id": "new", "instruction": "a different written rule"}])
            return {"items": [{"trigger_id": payload["trigger"]["trigger_id"], "kind": "accept", "title": "stale"}]}
    await router.route_one(Changing())
    with transaction() as db:
        assert not list(db.scalars(select(Task)))
        job = db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate"))
        assert job.status == "pending"


async def test_expired_router_claim_recovers_independently(project):
    configure(project)
    with transaction() as db:
        triggers.receive(db, db.get(Project, project), event("github:issue:89", addressed="mention", number=89))
        job = db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate"))
        job.status = "running"; job.owner = "dead"; job.lease_until = 0
        trigger_id = job.payload["trigger_id"]
    await router.route_one(Scripted({"items": [{"trigger_id": trigger_id, "kind": "ignore"}]}))
    with transaction() as db:
        assert db.scalar(select(Inbox).where(Inbox.kind == "explicit_candidate")).status == "done"
