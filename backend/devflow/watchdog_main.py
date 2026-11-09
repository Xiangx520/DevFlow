"""Independent Watchdog process: liveness, drift review and stopped-container cleanup.

This process shares only the database with the Router/Worker. A Watchdog outage must not block
normal delivery, and a Router/Worker outage must not stop supervision.
"""
import asyncio
import logging
from sqlalchemy import select
from . import watcher
from .adapters import redact
from .db import transaction
from .execution import DockerExecution
from .models import Inbox, Task

log = logging.getLogger("devflow.watchdog")
STOPPED = ["paused", "cancelled", "completed", "waiting_human"]


async def liveness():
    with transaction() as db:
        watcher.run_liveness(db)
        # Tool failures are recorded as Events; their Inbox rows are audit-only.
        for failure in db.scalars(select(Inbox).where(Inbox.kind == "tool_failure",
                                                      Inbox.status == "pending")):
            failure.status = "done"


async def review_running():
    with transaction() as db:
        ids = list(db.scalars(select(Task.id).where(Task.status == "running")))

    async def review(identifier):
        try:
            await watcher.review_task(identifier)
        except Exception as exc:
            log.warning("watchdog review %s: %s", identifier, redact(exc))

    await asyncio.gather(*(review(identifier) for identifier in ids))


async def stop_containers():
    with transaction() as db:
        stopped = set(db.scalars(select(Task.id).where(Task.status.in_(STOPPED))))
    if not stopped:
        return
    driver = DockerExecution()
    containers = await asyncio.to_thread(driver.client.containers.list, all=True,
                                         filters={"label": "devflow.task"})
    for container in containers:
        if container.labels.get("devflow.task") in stopped:
            await asyncio.to_thread(driver.stop, container.id)


async def repeat(name, action, delay):
    while True:
        try:
            await action()
        except Exception as exc:
            log.error("%s: %s", name, redact(exc))
        await asyncio.sleep(delay)


async def main():
    await asyncio.gather(
        repeat("liveness", liveness, 5),
        repeat("review", review_running, 5),
        repeat("containers", stop_containers, 5),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
