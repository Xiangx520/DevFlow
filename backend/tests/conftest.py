import os
import uuid
import pytest
from sqlalchemy import create_engine, text
from devflow import db as database
from devflow.config import settings
from devflow.models import Base

@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    url = os.environ.get("DEVFLOW_TEST_DATABASE_URL")
    monkeypatch.setenv("DEVFLOW_API_TOKEN", "owner-test-token")
    monkeypatch.setenv("DEVFLOW_GITHUB_WEBHOOK_SECRET", "webhook-test-secret")
    monkeypatch.setenv("DEVFLOW_DATABASE_URL", "sqlite:///" + str(tmp_path / "test.db"))
    settings.cache_clear(); database.engine.cache_clear()
    if url:
        if "test" not in url: raise RuntimeError("test database URL must name a test database")
        schema = "test_" + uuid.uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection: connection.execute(text("CREATE SCHEMA " + schema))
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
        monkeypatch.setattr(database, "engine", lambda: engine)
    else:
        engine = database.engine()
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    if url:
        with admin.begin() as connection: connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        admin.dispose()
    settings.cache_clear()

@pytest.fixture
def project():
    from devflow import core
    with database.transaction() as db:
        p = core.new_project(db, "owner/repo", {"allowed_actors":["owner"],"agent_login":"devflow-bot"},
                             {"verify_commands":["python -m pytest"],"setup_commands":[]})
        return p.id

@pytest.fixture
def task(project):
    from devflow import core
    with database.transaction() as db:
        t = core.create_task(db, project, "Fix login", "Fix expired login", ["Regression test"])
        return t.id
