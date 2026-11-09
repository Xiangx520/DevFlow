"""Versioned authorization policy: admission table, gate, and mandate revalidation.

The host enforces who may originate work and how strong that work may be. Messages, comments and
quoted documents are inputs to a decision; they can never change these rules. This mirrors the
super-agent split between a deterministic gate and a model assessment.
"""
import hashlib
import json
import re

ADMISSION_DEFAULTS = {"listedUnaddressed": "inferred", "unlistedAddressed": "ignore",
                      "unlistedUnaddressed": "ignore"}
ADMISSION_OPTIONS = {"listedUnaddressed": {"inferred", "ignore"},
                     "unlistedAddressed": {"explicit", "inferred", "ignore"},
                     "unlistedUnaddressed": {"inferred", "ignore"}}
STRENGTH = {"ignore": 0, "inferred": 1, "explicit": 2}
MODES = {"off", "observe", "auto"}
DEFAULT_SOURCE_SCOPES = {"github": {"issues": "all", "pulls": "all", "ci": True},
                         "feishu": {"chats": {}}, "api": {"enabled": True}}
DEFAULT_RULES: list[dict] = []


def normalize(policy):
    """Return a policy with defaults filled in, without changing the caller's dict."""
    value = dict(policy or {})
    value["allowed_actors"] = list(value.get("allowed_actors") or [])
    source_allowed = value.get("source_allowed") or {}
    value["source_allowed"] = {source: list(source_allowed.get(source) or [])
                               for source in ("github", "feishu", "api")}
    value["bots"] = list(value.get("bots") or [])
    value["agent_login"] = value.get("agent_login") or ""
    value["mode"] = value.get("mode") or "observe"
    if value["mode"] not in MODES:
        raise ValueError("policy mode must be off, observe or auto")
    value["rules"] = [normalize_rule(rule) for rule in (value.get("rules") or [])]
    ids = [rule["id"] for rule in value["rules"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule id")
    value["admission"] = {str(row): normalize_row(cells) for row, cells in (value.get("admission") or {}).items()}
    scopes = value.get("source_scopes") or {}
    value["source_scopes"] = {source: {**default, **(scopes.get(source) or {})}
                              for source, default in DEFAULT_SOURCE_SCOPES.items()}
    value["source_scopes"]["feishu"]["chats"] = {
        str(chat): {"receive": normalize_receive(cfg.get("receive", "mentions"))}
        for chat, cfg in (value["source_scopes"]["feishu"].get("chats") or {}).items()}
    return value


def normalize_rule(rule):
    rule = dict(rule)
    rule_id = str(rule.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rule_id):
        raise ValueError("rule id must be 1-80 letters, digits, _ or -")
    instruction = str(rule.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("rule instruction is required")
    return {"id": rule_id, "name": str(rule.get("name") or rule_id)[:200],
            "enabled": bool(rule.get("enabled", True)), "instruction": instruction[:4000]}


def normalize_row(cells):
    row = {}
    for cell, value in (cells or {}).items():
        if cell not in ADMISSION_OPTIONS:
            raise ValueError("unknown admission cell " + cell)
        if value not in ADMISSION_OPTIONS[cell]:
            raise ValueError(f"{value} is not allowed for {cell}")
        row[cell] = value
    return row


def normalize_receive(value):
    if value not in {"all", "mentions", "off"}:
        raise ValueError("receive must be all, mentions or off")
    return value


def ensure_version(project):
    """Backfill the version of a project created before policy versioning existed."""
    if not project.policy_version:
        project.policy_version = version(project.policy)
    return project.policy_version


def version(policy):
    """Hash exactly the rules that affect authorization; models and budgets do not invalidate decisions."""
    value = normalize(policy)
    body = {"rules": value["rules"], "mode": value["mode"], "admission": value["admission"],
            "source_scopes": value["source_scopes"], "allowed_actors": value["allowed_actors"],
            "source_allowed": value["source_allowed"],
            "bots": value["bots"], "agent_login": value["agent_login"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]


def same_name(entry, actor):
    return str(entry).lower() == str(actor or "").lower()


def allowed(policy, actor):
    return any(same_name(entry, actor) for entry in (policy.get("allowed_actors") or []))


def allowed_for(policy, source, actor):
    """Allowed identities are per Source; identities on different Sources are never mapped."""
    entries = (policy.get("source_allowed") or {}).get(source) or []
    if entries:
        return any(same_name(entry, actor) for entry in entries)
    return allowed(policy, actor)


def listed_bot(policy, actor):
    return any(same_name(entry, actor) for entry in (policy.get("bots") or []))


def looks_like_bot(actor):
    actor = str(actor or "")
    return actor.endswith("[bot]") or actor.endswith(".bot")


def enabled_rules(policy):
    return [rule for rule in (policy.get("rules") or []) if rule.get("enabled")]


def rules_enabled(policy, ids):
    enabled = {rule["id"] for rule in enabled_rules(policy)}
    return all(rule_id in enabled for rule_id in (ids or []))


def admission_value(policy, row, cell):
    configured = (policy.get("admission") or {}).get(row) if row else None
    return (configured or {}).get(cell) or ADMISSION_DEFAULTS[cell]


def scope(project, trigger):
    """Resolve whether a Source listens to a trigger's scope, and whether it observes every message there."""
    policy = normalize(project.policy)
    source = trigger["source"] if isinstance(trigger, dict) else trigger.source
    kind = trigger["kind"] if isinstance(trigger, dict) else trigger.kind
    channel = trigger["channel"] if isinstance(trigger, dict) else (trigger.channel or {})
    if source == "api":
        return {"listened": bool(policy["source_scopes"]["api"].get("enabled", True)), "observed": True, "row": "api", "reason": ""}
    if source == "feishu":
        chat = str(channel.get("chat_id") or "")
        config = (policy["source_scopes"]["feishu"]["chats"].get(chat) or {}).get("receive", "mentions")
        return {"listened": config != "off", "observed": config == "all", "row": "feishu:" + chat,
                "reason": "feishu scope " + config}
    if source == "github":
        if channel.get("repo") and channel["repo"] != getattr(project, "repository", channel["repo"]):
            return {"listened": False, "observed": False, "row": "github", "reason": "repository mismatch"}
        if kind.startswith("ci."):
            listened = bool(policy["source_scopes"]["github"].get("ci", True))
            return {"listened": listened, "observed": listened, "row": "github:ci", "reason": "github ci"}
        area = "pulls" if channel.get("is_pull") else "issues"
        config = policy["source_scopes"]["github"].get(area, "all")
        if config == "off":
            return {"listened": False, "observed": False, "row": "github:" + area, "reason": "github scope off"}
        return {"listened": True, "observed": config == "all", "row": "github:" + area,
                "reason": "github " + area + " " + config}
    return {"listened": False, "observed": False, "row": source, "reason": "unknown source"}


def _cell(is_allowed, addressed):
    if is_allowed:
        return None if addressed else "listedUnaddressed"
    return "unlistedAddressed" if addressed else "unlistedUnaddressed"


def _participating(thread, tickets):
    ids = [ticket for ticket in (thread.get("subscriptions") or []) if ticket not in (thread.get("context") or [])]
    return bool(ids) or bool(tickets)


def _recorded(trigger, scope_value, label, actor_name=None):
    channel = trigger.get("channel") or {}
    basis = {"source": trigger.get("source"), "scope": scope_value.get("row"),
             "trigger_id": trigger.get("id")}
    for key in ("repo", "number", "chat_id", "message_id", "url", "is_pull", "is_issue"):
        if channel.get(key) is not None:
            basis[key] = channel[key]
    if label:
        basis["via"] = label
    if actor_name:
        basis["sender"] = actor_name
    return basis


def gate(project, trigger, thread=None, tickets=None):
    """Return {admissible, strength, basis, reason}. Never mutates state."""
    policy = normalize(project.policy)
    trigger = trigger if isinstance(trigger, dict) else _trigger_dict(trigger)
    thread = thread or {}
    tickets = tickets or []
    mode = policy["mode"]
    scope_value = scope(project, trigger)
    channel = trigger.get("channel") or {}
    actor = trigger.get("actor") or ""
    human = bool(trigger.get("human", True))
    addressed = trigger.get("addressed")

    if not human:
        is_bot = bool(actor) and listed_bot(policy, actor)
        if actor and not is_bot:
            return {"admissible": False, "reason": "not_human"}
        if not scope_value["listened"]:
            return {"admissible": False, "reason": "scope_off"}
        if actor and not (addressed or scope_value["observed"]):
            return {"admissible": False, "reason": "not_human"}
        if mode == "off":
            return {"admissible": False, "reason": "admission_ignored"}
        basis = {**_recorded(trigger, scope_value, "bot" if is_bot else "admission", actor if is_bot else None),
                 "observation": {"mode": mode}}
        if not is_bot:
            basis["cell"] = "unlistedUnaddressed"
        return {"admissible": True, "strength": "inferred", "basis": basis, "reason": ""}

    is_allowed = allowed_for(policy, trigger.get("source"), actor)
    if not scope_value["listened"]:
        return {"admissible": False, "reason": "scope_off"}
    recorded = _recorded(trigger, scope_value, "", actor if is_allowed else None)
    if addressed and is_allowed:
        return {"admissible": True, "strength": "explicit",
                "basis": {**recorded, "via": "addressed", "addressed": addressed}, "reason": ""}

    receivable = bool(addressed) or scope_value["observed"]
    cell = _cell(is_allowed, bool(addressed))
    admission = admission_value(policy, scope_value["row"], cell) if receivable and cell else "ignore"
    basis = {**recorded, "via": "admission", "cell": cell}
    if addressed:
        basis["addressed"] = addressed
    if admission == "explicit":
        return {"admissible": True, "strength": "explicit", "basis": basis, "reason": ""}

    # A person's follow-up on a thread that already has tickets keeps the thread's work going.
    direct_by_unlisted = addressed == "direct" and not is_allowed
    if not direct_by_unlisted and (is_allowed or mode != "off") and _participating(thread, tickets):
        return {"admissible": True, "strength": "explicit" if is_allowed else "inferred",
                "basis": {**recorded, "via": "followup"}, "reason": ""}

    if admission == "inferred" and mode != "off":
        return {"admissible": True, "strength": "inferred",
                "basis": {**basis, "observation": {"mode": mode}}, "reason": ""}

    if not is_allowed:
        return {"admissible": False, "reason": "not_a_member"}
    if receivable and admission == "ignore":
        return {"admissible": False, "reason": "admission_ignored"}
    return {"admissible": False, "reason": "no_mention"}


def revalidate(project, mandate):
    """Whether a mandate still holds under the current policy. Used before resuming an ended task."""
    policy = normalize(project.policy)
    basis = (mandate or {}).get("basis") or {}
    strength = (mandate or {}).get("strength") or "inferred"
    mode = policy["mode"]
    actor = (mandate or {}).get("actor") or basis.get("sender") or ""
    via = basis.get("via")
    if basis.get("source"):
        filtered = {"source": basis["source"], "kind": "ci.completed" if basis.get("scope") == "github:ci" else "message",
                    "channel": {k: basis.get(k) for k in ("repo", "number", "chat_id", "is_pull") if basis.get(k) is not None}}
        if not scope(project, filtered)["listened"]:
            return False
    if via == "followup":
        return (strength == "explicit" and allowed_for(policy, basis.get("source"), actor)) or (mode == "auto" and rules_enabled(policy, basis.get("rules")))
    if via == "bot":
        return listed_bot(policy, actor) and mode == "auto" and rules_enabled(policy, basis.get("rules"))
    if via == "admission":
        cell = basis.get("cell")
        if cell == "unlistedAddressed" and allowed_for(policy, basis.get("source"), actor):
            return True
        listed = allowed_for(policy, basis.get("source"), actor)
        current_cell = "listedUnaddressed" if (cell == "unlistedUnaddressed" and listed) else cell
        if current_cell == "listedUnaddressed" and not listed:
            return False
        row = basis.get("scope")
        current = admission_value(policy, row, current_cell)
        if STRENGTH[current] < STRENGTH.get(strength, 0):
            return False
        if strength == "explicit":
            return True
        return mode == "auto" and rules_enabled(policy, basis.get("rules"))
    return allowed_for(policy, basis.get("source"), actor)


def policy_effect(project, task, old_policy):
    """How a policy change bears on one live task; None when it is unaffected."""
    new_policy = normalize(project.policy)
    old = normalize(old_policy)
    mandate = task.mandate or {}
    basis = mandate.get("basis") or {}
    effect = {}
    changed = []
    for rule_id in (basis.get("rules") or []):
        was = next((rule for rule in old["rules"] if rule["id"] == rule_id), None)
        now = next((rule for rule in new_policy["rules"] if rule["id"] == rule_id), None)
        if was and was.get("enabled") and not (now and now.get("enabled")):
            changed.append({"id": rule_id, "name": was.get("name"), "change": "disabled"})
        elif was and now and json.dumps(was, sort_keys=True) != json.dumps(now, sort_keys=True):
            changed.append({"id": rule_id, "name": now.get("name"), "change": "changed",
                            "instruction": now.get("instruction")})
    if changed:
        effect["rules"] = changed
    # Revalidation under old vs new policy, keeping the old accept mode to isolate sender/scope changes.
    class _Old:
        policy = old
    if revalidate(_Old, mandate):
        if not revalidate(project, mandate):
            as_before = {**new_policy, "mode": old["mode"],
                         "rules": old["rules"]}
            class _AsBefore:
                policy = as_before
            if not revalidate(_AsBefore, mandate):
                effect["requester_allowed"] = False
                effect["requester"] = mandate.get("actor")
            elif mandate.get("strength") == "inferred" and old.get("mode") == "auto" and new_policy["mode"] != "auto":
                effect["proactive"] = False
    return effect or None


def _trigger_dict(trigger):
    return {"id": trigger.id, "source": trigger.source, "kind": trigger.kind,
            "channel": trigger.channel or {}, "actor": trigger.actor, "human": trigger.human,
            "addressed": trigger.addressed, "text": trigger.text}
