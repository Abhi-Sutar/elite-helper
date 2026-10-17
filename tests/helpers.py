"""Shared test setup. Set TEST_DATABASE_URL (the elite_test database) to run the store/query tests on PostgreSQL."""

import os

from sqlalchemy import text

from eddn_helper import database, migrate
from eddn_helper.models import Base

PG_URL = os.environ.get("TEST_DATABASE_URL")


def _pause_timescale_jobs(engine):
    """Every fresh schema gets policy jobs that start within seconds and deadlock with the test's own inserts."""
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first():
            conn.execute(text("SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
                              "WHERE hypertable_name IS NOT NULL"))


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
    if PG_URL:
        _pause_timescale_jobs(engine)
    return database.make_session_factory(engine)()
