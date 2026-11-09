from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from devflow import core
from devflow.db import transaction
from devflow.models import Base, Task

def test_one_lease_under_concurrent_claims(task,isolated_database):
    if isolated_database.dialect.name != "postgresql": pytest.skip("PostgreSQL concurrency contract")
    def claim(owner):
        with transaction() as db:
            result=core.claim_task(db,task,owner)
            return result[0].id if result else None
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(claim,["worker-a","worker-b"]))
    assert len([x for x in results if x])==1

def test_initial_migration_upgrade_downgrade(isolated_database):
    Base.metadata.drop_all(isolated_database)
    cfg=Config(str(Path(__file__).parents[1]/"alembic.ini"))
    cfg.set_main_option("script_location",str(Path(__file__).parents[1]/"migrations"))
    command.upgrade(cfg,"head")
    assert "tasks" in inspect(isolated_database).get_table_names()
    command.downgrade(cfg,"base")
    assert "tasks" not in inspect(isolated_database).get_table_names()
