"""Architecture boundary checks for the Router / Worker / Watchdog split."""
import ast
import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DEVFLOW = BACKEND / "devflow"


def source(name: str) -> str:
    return (DEVFLOW / f"{name}.py").read_text(encoding="utf-8")


def imported_roots(name: str) -> set[str]:
    """Top-level modules imported by a devflow module, ignoring docstrings and comments."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source(name))):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
            else:
                roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
    return roots


def test_retired_manager_module_is_gone():
    assert importlib.util.find_spec("devflow.manager") is None


def test_worker_does_not_reference_retired_manager():
    assert "manager" not in source("worker").lower()


def test_project_has_an_explicit_pause_control():
    from devflow.models import Project
    assert "paused" in Project.__table__.columns


def test_target_architecture_is_documented():
    doc = (BACKEND.parent / "docs" / "TARGET_ARCHITECTURE.md").read_text(encoding="utf-8")
    for role in ("Router", "Worker", "Watchdog", "Delivery", "Engine"):
        assert role in doc


def test_delivery_does_not_import_watchdog():
    assert "watcher" not in imported_roots("delivery")


def test_watchdog_does_not_import_delivery():
    assert "delivery" not in imported_roots("watcher")


def test_generic_effects_do_not_import_watchdog():
    assert "watcher" not in imported_roots("effects")


def test_reviewer_is_not_the_watchdog():
    assert "watcher" not in imported_roots("reviewer")
    assert "delivery" in imported_roots("reviewer")


def test_watchdog_runs_in_its_own_process():
    assert "watcher" not in imported_roots("worker")
    assert "watcher" in imported_roots("watchdog_main")


def test_github_source_does_not_write_domain_state():
    roots = imported_roots("github_events")
    assert "tickets" not in roots and "steering" not in roots and "delivery" not in roots
    assert "changes" in roots
