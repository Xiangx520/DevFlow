from contextlib import contextmanager
from functools import lru_cache
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

class Base(DeclarativeBase):
    pass

@lru_cache
def engine():
    e = create_engine(settings().database_url, pool_pre_ping=True,
                      connect_args={"check_same_thread": False} if settings().database_url.startswith("sqlite") else {})
    if e.dialect.name == "sqlite":
        @event.listens_for(e, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return e

@contextmanager
def transaction():
    with sessionmaker(bind=engine(), expire_on_commit=False)() as db:
        with db.begin():
            yield db
