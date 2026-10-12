"""Programmatic Alembic API: status, upgrade (with backup), and the reader-side schema check."""

import sqlite3
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from . import config as app_config

# Revision the code expects. tests/test_migrations.py asserts this equals Alembic's real head.
SCHEMA_HEAD = "0006"
SCRIPT_LOCATION = Path(__file__).parent / "migrations"  # tests point this at a scratch copy


class SchemaError(RuntimeError):
    pass


def alembic_config(engine=None, connection=None):
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    if engine is not None:
        cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision():
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine):
    with engine.connect() as conn:
        rev = MigrationContext.configure(conn).get_current_revision()
        conn.rollback()
    return rev


def _has_user_tables(engine):
    return bool(set(inspect(engine).get_table_names()) - {"alembic_version"})


def status(engine):
    """{'current', 'head', 'state'}: state in empty | ok | behind | ahead."""
    current, head = current_revision(engine), head_revision()
    script = ScriptDirectory.from_config(alembic_config())
    if current is None:
        state = "empty" if not _has_user_tables(engine) else "behind"
    elif current == head:
        state = "ok"
    elif not _known(script, current):
        state = "ahead"
    else:
        state = "behind"
    return {"current": current, "head": head, "state": state}


def _known(script, rev):
    try:
        script.get_revision(rev)
        return True
    except Exception:
        return False


def ensure_current(engine):
    """Reader-side guard: refuse to run against a DB whose schema doesn't match this code."""
    s = status(engine)
    if s["state"] == "ok":
        return s
    if s["state"] == "ahead":
        raise SchemaError(f"database schema {s['current']} is newer than this code ({s['head']}); update the code")
    raise SchemaError(f"database schema is {s['current'] or 'uninitialised'}, code needs {s['head']}; "
                      "run `python -m eddn_helper migrate upgrade` (or start the listener, which migrates)")


def backup_database(engine, dest):
    """Consistent copy of a SQLite database (safe while another process is writing)."""
    src = app_config.sqlite_path(engine.url.render_as_string(hide_password=False))
    if src is None:
        raise SchemaError("backup is only implemented for SQLite files; use pg_dump for PostgreSQL")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    s, d = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dest


def upgrade(engine, rev="head", backup=True):
    """Apply pending revisions in one transaction (rolled back on failure). Returns status after."""
    before = status(engine)
    if before["state"] == "ahead":
        raise SchemaError(f"database schema {before['current']} is newer than this code")
    if before["state"] == "ok" and rev == "head":
        return before
    path = app_config.sqlite_path(engine.url.render_as_string(hide_password=False))
    if backup and path is not None and path.exists() and before["current"] is not None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_database(engine, path.with_name(f"{path.name}.pre-{before['current']}-{stamp}.bak"))
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            conn = conn.execution_options(sqlite_begin_immediate=True)  # serialises concurrent upgraders
        cfg = alembic_config(engine, conn)
        command.upgrade(cfg, rev)
        conn.commit()
    return status(engine)
