"""Engine/session setup and dialect-neutral helpers (SQLite today, PostgreSQL/Timescale later)."""

import contextlib
import sqlite3
import time

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from . import config


def _enable_wal(cur, attempts=100, delay=0.1):
    """Switch to WAL. Changing journal mode needs an exclusive lock and ignores busy_timeout, so when
    several processes open a brand-new database at once the loser must retry; it persists once set."""
    for _ in range(attempts):
        if cur.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            return
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            time.sleep(delay)
    raise sqlite3.OperationalError("could not enable WAL mode: database stayed locked")


def make_engine(url=None, **kwargs):
    url = config.database_url(url)
    u = make_url(url)
    if u.get_backend_name() == "sqlite":
        path = config.sqlite_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:  # in-memory: one shared connection so every session sees the same database
            kwargs.setdefault("poolclass", StaticPool)
        kwargs.setdefault("connect_args", {"timeout": 30, "check_same_thread": False})
    elif u.get_backend_name() == "postgresql":
        if u.drivername == "postgresql":
            url = str(u.set(drivername="postgresql+psycopg"))  # prefer psycopg 3
        kwargs.setdefault("pool_pre_ping", True)  # survive a restarted database service
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        # Take transaction control from pysqlite so DDL is transactional and BEGIN IMMEDIATE is possible.
        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):
            immediate = conn.get_execution_options().get("sqlite_begin_immediate")
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            if config.sqlite_path(url) is not None:
                _enable_wal(cur)
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return engine


def make_session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)


@contextlib.contextmanager
def session_scope(factory):
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def insert_for(engine_or_session):
    """Dialect-specific INSERT construct supporting on_conflict_do_update/nothing."""
    bind = engine_or_session.get_bind() if hasattr(engine_or_session, "get_bind") else engine_or_session
    return postgresql.insert if bind.dialect.name == "postgresql" else sqlite.insert


def masked_url(url):
    """Display form of the DB URL: the file path for SQLite, otherwise the URL with the password hidden."""
    u = make_url(config.database_url(url))
    if u.get_backend_name() == "sqlite" and u.database:
        return u.database
    return u.render_as_string(hide_password=True)
