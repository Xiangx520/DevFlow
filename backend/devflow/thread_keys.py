"""Canonical project-scoped thread identity for every source."""
from .core import Conflict

KINDS = {"github": {"issue", "pr"}, "feishu": {"chat"}, "api": {"session", "task"}}


def make(project_id, source, kind, identity):
    if not project_id or ":" in project_id or source not in KINDS or kind not in KINDS[source] or not str(identity):
        raise Conflict("invalid thread identity")
    return f"{project_id}:{source}:{kind}:{identity}"


def parse(key):
    parts = key.split(":", 3)
    if len(parts) != 4:
        raise Conflict("project-scoped thread key required")
    project, source, kind, identity = parts
    make(project, source, kind, identity)
    return project, source, kind, identity


def normalize(project_id, key):
    if key.split(":", 1)[0] in KINDS:
        key = project_id + ":" + key
    project, source, kind, identity = parse(key)
    if project != project_id:
        raise Conflict("thread belongs to another project")
    return make(project, source, kind, identity)
