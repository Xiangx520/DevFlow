"""GitHub Source: durable webhook admission, trigger translation, follow-ups and reconciliation.

The Source drops its own bot's events, translates each payload into an immutable Trigger, and lets
Intake do the routing. Rich CI and PR-close handling remains here because it needs GitHub-specific
facts (head SHA, run attempt, merge state); the trigger records the same fact for audit and Evals.
"""
import hashlib
import hmac
import json
import time
from sqlalchemy import select
from . import changes, core, policy, triggers
from .adapters import GitHub
from .config import settings
from .db import transaction
from .models import Inbox, Project, SourceCursor, Task

def signature(body, supplied):
    secret = settings().github_webhook_secret
    if not secret: raise core.Conflict("webhook secret is not configured")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied or "")

def intake(db, event_type, payload, delivery):
    repository = (payload.get("repository") or {}).get("full_name")
    project = db.scalar(select(Project).where(Project.repository == repository))
    if not project: return {"accepted": False, "reason": "repository not connected"}
    core.lock_project(db, project.id)
    existing = db.scalar(select(Inbox).where(Inbox.dedupe_key == "github:" + delivery))
    if existing: return {"accepted": True, "duplicate": True}
    core.enqueue(db, project.id, "github", {"event_type": event_type, "data": payload},
                 key="github:" + delivery)
    return {"accepted": True}


def _threads(payload, kind):
    issue = payload.get("issue") or {}
    pr = payload.get("pull_request") or {}
    if pr:
        number = pr.get("number")
        return [f"github:pr:{number}"], {"number": number, "is_pull": True, "is_issue": False}
    if issue:
        number = issue.get("number")
        is_pr = bool(issue.get("pull_request")) or kind.startswith("pull_request")
        return [f"github:pr:{number}" if is_pr else f"github:issue:{number}"], {
            "number": number, "is_pull": is_pr, "is_issue": not is_pr}
    return [], {}


def _actor(payload):
    return ((payload.get("sender") or {}).get("login") or "")


def _addressed(payload, policy_value):
    body = _text(payload)
    agent = (policy_value or {}).get("agent_login") or ""
    if payload.get("action") == "labeled" and (payload.get("label") or {}).get("name") == "agent:run":
        return "label"
    if agent and ("@" + agent).lower() in body.lower():
        return "mention"
    return None


def _text(payload):
    issue = payload.get("issue") or {}
    pr = payload.get("pull_request") or {}
    body = (payload.get("comment") or payload.get("review") or {}).get("body") or ""
    return (body or issue.get("body") or pr.get("body") or issue.get("title")
            or pr.get("title") or "")


def _is_bot(login, policy_value):
    if not login:
        return False
    if login.endswith("[bot]") or login.endswith(".bot"):
        return True
    return any(str(item).lower() == login.lower() for item in (policy_value or {}).get("bots") or [])


def _ci_task(db, project, sha):
    if not sha:
        return None
    return db.scalar(select(Task).where(Task.project_id == project.id, Task.head_sha == sha)
                     .order_by(Task.created_at.desc()))


def business_identity(project_id, kind, payload):
    """Source-independent identity shared by webhook and reconciliation.

    Delivery IDs describe transport attempts. Message IDs plus revisions describe work.
    """
    message = payload.get("comment") or payload.get("review")
    if message:
        family = "review" if payload.get("review") else "comment"
        revision = message.get("updated_at") or message.get("submitted_at") or message.get("created_at")
        value = [project_id, family, message.get("id"), revision, message.get("body", "")]
    else:
        entity = payload.get("pull_request") or payload.get("issue") or {}
        action = payload.get("action")
        if action in {"opened", "edited"}: action = "content"
        value = [project_id, kind, action, entity.get("id") or entity.get("number"),
                 entity.get("updated_at"), entity.get("state"), entity.get("head"), payload.get("label")]
    return "github-business:" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def process(db, job):
    project = core.lock_project(db, job.project_id)
    payload = job.payload["data"]; kind = job.payload["event_type"]
    policy_value = policy.normalize(project.policy)
    sender = _actor(payload)
    agent = policy_value.get("agent_login", "")
    if sender and sender.lower() == str(agent).lower():
        job.status = "done"; return
    action = payload.get("action", "")
    thread_keys, channel = _threads(payload, kind)
    thread_keys = [f"{project.id}:{key}" for key in thread_keys]
    channel = {"repo": project.repository, **channel}

    if kind in {"workflow_run", "check_run", "check_suite"}:
        _handle_ci(db, project, job, kind, payload, channel)
        job.status = "done"; return

    if not thread_keys:
        job.status = "done"; return

    human = not _is_bot(sender, policy_value)
    addressed = _addressed(payload, policy_value) if human else None
    dedupe = business_identity(project.id, kind, payload)
    event = {
        "source": "github", "kind": f"{kind}.{action}" if action else kind,
        "external_id": job.id, "dedupe_key": dedupe, "actor": sender, "human": human,
        "addressed": addressed, "thread_keys": thread_keys,
        "context_only": kind == "pull_request" and action in {"closed", "reopened", "synchronize"},
        "text": _text(payload), "url": _html_url(payload), "channel": channel,
        "at": time.time(), "payload": {"event_type": kind, "action": action},
    }
    # The Source records facts; deterministic domain services react to them.
    if kind == "pull_request" and action == "synchronize":
        task = _task_for(db, project, channel)
        sha = (payload.get("pull_request") or {}).get("head", {}).get("sha")
        if task and sha and sha != task.head_sha and task.status not in core.TERMINAL:
            changes.enqueue(db, project.id, "pr_head_changed",
                            {"task_id": task.id, "sha": sha}, task.id,
                            "source-pr-head:" + task.id + ":" + sha)
    result = triggers.receive(db, project, event)
    if kind == "pull_request" and action in {"closed", "reopened"}:
        revision = str((payload.get("pull_request") or {}).get("updated_at") or job.id)
        if action == "closed":
            merged = bool((payload.get("pull_request") or {}).get("merged"))
            changes.enqueue(db, project.id, "pr_closed",
                            {"thread_key": thread_keys[0],
                             "outcome": "merged" if merged else "closed"}, None,
                            "source-pr-close:" + thread_keys[0] + ":" + revision)
        else:
            changes.enqueue(db, project.id, "pr_reopened",
                            {"pr_number": channel.get("number")}, None,
                            "source-pr-reopen:" + thread_keys[0] + ":" + revision)
    # Owner comments that answer a single open Attention belong to that question.
    if human and kind in {"issue_comment", "pull_request_review", "pull_request_review_comment"}:
        target = _task_for(db, project, channel)
        if target is not None and target.status not in core.TERMINAL:
            changes.enqueue(db, project.id, "attention_answer",
                            {"task_id": target.id, "sender": sender, "body": _text(payload)},
                            target.id, "source-attention:" + job.id)
    if result == "filtered":
        job.status = "done"; return
    job.status = "done"


def _task_for(db, project, channel):
    number = channel.get("number")
    if not number:
        return None
    column = Task.pr_number if channel.get("is_pull") else Task.issue_number
    return db.scalar(select(Task).where(Task.project_id == project.id, column == number)
                     .order_by(Task.created_at.desc()))


def _handle_ci(db, project, job, kind, payload, channel):
    ci = payload.get("workflow_run") or payload.get("check_run") or payload.get("check_suite") or {}
    sha = ci.get("head_sha")
    task = _ci_task(db, project, sha)
    thread_keys = []
    if task:
        if task.pr_number:
            thread_keys = [f"{project.id}:github:pr:{task.pr_number}"]
        elif task.issue_number:
            thread_keys = [f"{project.id}:github:issue:{task.issue_number}"]
        else:
            thread_keys = []
    # A CI fact is recorded even when no ticket owns it; it never creates work.
    if thread_keys:
        triggers.receive(db, project, {
            "source": "github", "kind": "ci.completed", "external_id": job.id,
            "dedupe_key": "github-trigger:" + job.id, "actor": "", "human": False,
            "addressed": None, "thread_keys": thread_keys,
            "text": f"{kind} {ci.get('conclusion') or ci.get('status') or ''}".strip(),
            "url": ci.get("html_url") or "", "channel": {"repo": project.repository,
                                                        "number": channel.get("number"),
                                                        "is_pull": True}, "at": time.time(),
            "payload": {"conclusion": ci.get("conclusion"), "sha": sha},
        })
    if task and (ci.get("status") == "completed" or payload.get("action") == "completed"):
        conclusion = ci.get("conclusion")
        ci_key = f"ci:{kind}:{ci.get('id')}:{ci.get('run_attempt', 1)}:{conclusion}"
        changes.enqueue(db, project.id, "ci_completed",
                        {"task_id": task.id, "ci_key": ci_key, "ci": ci,
                         "event_kind": kind}, task.id, "source-" + ci_key)
    elif sha:
        core.enqueue(db, project.id, "observed", {"reason": "unowned or old CI", "sha": sha},
                     None, "observed:" + job.id)


def _html_url(payload):
    issue = payload.get("issue") or {}
    pr = payload.get("pull_request") or {}
    review = payload.get("review") or {}
    comment = payload.get("comment") or {}
    return (comment.get("html_url") or review.get("html_url") or pr.get("html_url")
            or issue.get("html_url") or "")


async def process_one():
    with transaction() as db:
        job = db.scalar(select(Inbox).where(Inbox.kind == "github", Inbox.status == "pending")
                        .order_by(Inbox.created_at).with_for_update(skip_locked=True).limit(1))
        if not job: return False
        process(db, job)
    return True


async def reconcile(github=None):
    github = github or GitHub()
    with transaction() as db:
        projects = [p.id for p in db.scalars(select(Project).where(
            Project.last_poll < time.time() - settings().webhook_poll_seconds))]
    for project_id in projects:
        try:
            await reconcile_project(github, project_id)
        except Exception as exc:
            with transaction() as db:
                core.event(db, project_id, "source.unavailable", {"source": "github", "reason": type(exc).__name__})


async def reconcile_project(github, project_id):
    from datetime import datetime, timezone
    started = time.time()
    with transaction() as db:
        project = core.get(db, Project, project_id)
        repository = project.repository
        cursor = db.get(SourceCursor, (project_id, "github", repository))
        since = float((cursor.position if cursor else {}).get("through", project.last_poll or 0))
        scopes = policy.normalize(project.policy)["source_scopes"]["github"]
    params = {"state": "all", "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = datetime.fromtimestamp(max(0, since-120), timezone.utc).isoformat()

    def admit(kind, payload, identity):
        with transaction() as db:
            intake(db, kind, {"repository": {"full_name": repository}, **payload}, identity)

    if scopes.get("issues") != "off":
        issues = await github.pages(f"/repos/{repository}/issues", params)
        for issue in issues:
            if issue.get("pull_request"):
                continue
            admit("issues", {"action": "edited", "issue": issue, "sender": issue.get("user") or {}},
                  f"poll-issue:{project_id}:{issue['number']}:{issue.get('updated_at')}")
            if any(label.get("name") == "agent:run" for label in issue.get("labels") or []):
                events = await github.pages(f"/repos/{repository}/issues/{issue['number']}/events")
                for event in events:
                    if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == "agent:run":
                        admit("issues", {"action": "labeled", "issue": issue, "label": {"name": "agent:run"},
                                        "sender": event.get("actor") or {}}, f"poll-label:{project_id}:{event['id']}")
    if scopes.get("issues") != "off" or scopes.get("pulls") != "off":
        comments = await github.pages(f"/repos/{repository}/issues/comments", {"since": params["since"]} if since else {})
        for comment in comments:
            number = int(comment["issue_url"].rsplit("/", 1)[1])
            issue = await github.request("GET", f"/repos/{repository}/issues/{number}")
            admit("issue_comment", {"action": "created", "issue": issue, "comment": comment,
                  "sender": comment.get("user") or {}}, f"poll-comment:{project_id}:{comment['id']}:{comment.get('updated_at')}")
    if scopes.get("pulls") != "off":
        pulls = await github.pages(f"/repos/{repository}/pulls", {k:v for k,v in params.items() if k != "since"})
        for pr in pulls:
            number = pr["number"]
            admit("pull_request", {"action": "closed" if pr.get("state") == "closed" else "synchronize",
                  "pull_request": pr, "sender": {"login": ""}}, f"poll-pr:{project_id}:{number}:{pr.get('updated_at')}")
            # Pull body is a separate content revision; lifecycle facts never grant work ownership.
            admit("pull_request", {"action": "edited", "pull_request": pr, "sender": pr.get("user") or {}},
                  f"poll-pr-content:{project_id}:{number}:{pr.get('updated_at')}")
            for endpoint, kind in [("reviews", "pull_request_review"), ("comments", "pull_request_review_comment")]:
                for item in await github.pages(f"/repos/{repository}/pulls/{number}/{endpoint}"):
                    admit(kind, {"action": "submitted", "pull_request": pr,
                          "review" if endpoint == "reviews" else "comment": item,
                          "sender": item.get("user") or {}}, f"poll-pr-message:{project_id}:{endpoint}:{item['id']}:{item.get('updated_at', item.get('submitted_at'))}")
            if scopes.get("ci") and (sha := (pr.get("head") or {}).get("sha")):
                page = 1
                while True:
                    runs = await github.request("GET", f"/repos/{repository}/actions/runs",
                                                params={"head_sha": sha, "page": page, "per_page": 100})
                    rows = runs.get("workflow_runs") or []
                    for run in rows:
                        if run.get("status") == "completed":
                            admit("workflow_run", {"action": "completed", "workflow_run": run},
                                  f"poll-ci:{project_id}:{run['id']}:{run.get('run_attempt',1)}:{run.get('conclusion')}")
                    if page*100 >= runs.get("total_count", len(rows)):
                        break
                    if not rows or page >= 100:
                        raise RuntimeError("GitHub CI pagination incomplete")
                    page += 1
    with transaction() as db:
        row = db.get(SourceCursor, (project_id, "github", repository))
        if row is None:
            row = SourceCursor(project_id=project_id, source="github", scope=repository)
            db.add(row)
        row.position = {"through": started}; row.updated_at = time.time()
        core.get(db, Project, project_id).last_poll = started
