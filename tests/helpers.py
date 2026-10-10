"""Shared test setup. Set TEST_DATABASE_URL (the elite_test database) to run the store/query tests on PostgreSQL."""

import os

from sqlalchemy import text

from eddn_helper import database, migrate
from eddn_helper.models import Base

PG_URL = os.environ.get("TEST_DATABASE_URL")


def new_session():
    """A session on a freshly migrated database: in-memory SQLite, or the PostgreSQL test database (wiped first)."""
    if PG_URL:
        engine = database.make_engine(PG_URL)
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    else:
        engine = database.make_engine("sqlite://")
    migrate.upgrade(engine, backup=False)
    return database.make_session_factory(engine)()
