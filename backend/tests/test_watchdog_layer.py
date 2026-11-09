"""Independent Watchdog query and maintainer-instruction surface."""
from devflow import core, watcher
from devflow.db import transaction
from devflow.models import Project


class Scripted:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def json(self, *args, **kwargs):
        self.calls += 1
        return self.result


async def test_watchdog_answers_from_bounded_state(project):
    with transaction() as db:
        state = watcher.state(db, project)
    model = Scripted({"answer": "CI is pending; no worker is stuck."})
    answer = await watcher.ask(state, "why is this slow?", model)
    assert answer == "CI is pending; no worker is stuck."
    assert model.calls == 1


def test_watchdog_instructions_are_audited_and_visible(project):
    with transaction() as db:
        p = core.get(db, Project, project)
        row = watcher.add_instruction(db, p, "Prefer small, reversible diffs.")
        instruction_id = row.id
        state = watcher.state(db, project)
        assert state["instructions"][0]["text"] == "Prefer small, reversible diffs."
        watcher.retire_instruction(db, p, instruction_id)
        assert watcher.state(db, project)["instructions"] == []
