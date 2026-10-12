"""
Journal files -> database. Used two ways that share `sync()`:

* on demand: every DB-backed journal query calls `ensure_fresh()` first - it compares what the database holds with
  the files on disk (a few `stat` calls) and ingests only what is new before answering;
* as a service: `watch()` (`journal-sync --watch`, optionally a scheduled task) runs `sync()` in a loop.

Both are idempotent (rows are keyed on file + line), serialised by a cross-process lock, and a query never fails just
because the database cannot be written: it answers from what is stored and says the data may be behind.
"""

import os
import subprocess
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from .. import config, database, lock, migrate, store
from ..models import JournalCommander, JournalEvent, JournalFile
from . import files as jfiles
from . import state as jstate

ENV_IGNORE = "ELITE_HELPER_JOURNAL_IGNORE"
# Event names not stored (comma separated in the env var). Default: store everything - results then match the files
# exactly (even "Music" lines shape playtime gaps); line numbers advance either way.
IGNORED_EVENTS = frozenset(x.strip() for x in os.environ.get(ENV_IGNORE, "").split(",") if x.strip())
BATCH = 300
TASK_NAME = "EliteHelperJournal"
META_INGEST_AT = "journal_last_ingest_at"
META_INGEST_SECONDS = "journal_last_ingest_seconds"
META_INGEST_EVENTS = "journal_last_ingest_events"
META_ERROR = "journal_last_error"
META_HEARTBEAT = "journal_ingester_heartbeat"


class IngestBusy(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


# ---- comparing the database with the files -------------------------------------------------------------------------

def disk_files(directory):
    """{journal file name: size in bytes} for the files on disk."""
    out = {}
    for path in jfiles.find_journal_files(directory):
        try:
            out[os.path.basename(path)] = os.stat(path).st_size
        except OSError:
            continue
    return out


def compare(session, directory):
    """What the database is missing, without parsing anything: new / grown / shrunk / gone files."""
    stored = {r.name: r.size_bytes for r in session.execute(select(JournalFile.name, JournalFile.size_bytes))}
    disk = disk_files(directory)
    new = sorted(n for n in disk if n not in stored)
    grown = sorted(n for n in disk if n in stored and disk[n] > stored[n])
    shrunk = sorted(n for n in disk if n in stored and disk[n] < stored[n])
    gone = sorted(n for n in stored if n not in disk)
    todo = sorted(new + grown + shrunk)
    return {"disk_files": len(disk), "stored_files": len(stored), "new": new, "grown": grown, "shrunk": shrunk,
            "gone": gone, "to_ingest": todo,
            "bytes_behind": sum(disk[n] - stored.get(n, 0) for n in todo if disk[n] > stored.get(n, 0)),
            "behind": bool(todo)}


# ---- ingest --------------------------------------------------------------------------------------------------------

def _ingest_file(session, path, is_newest, rebuild=False):
    name = os.path.basename(path)
    size, rows, final = jfiles.read_numbered(path)
    jf = session.execute(select(JournalFile).where(JournalFile.name == name)).scalar_one_or_none()
    if jf is not None and (rebuild or size < jf.size_bytes):
        session.execute(delete(JournalEvent.__table__).where(JournalEvent.file_id == jf.id))
        jf.lines_read = 0
    if jf is None:
        jf = JournalFile(name=name, size_bytes=0, lines_read=0, ingested_at=_now())
        session.add(jf)
        session.flush()
    start = jf.lines_read
    state = jstate.CommanderState()
    insert = database.insert_for(session)(JournalEvent.__table__).on_conflict_do_nothing()
    batch, inserted, shutdown, null_fid = [], 0, False, False
    first_ts = last_ts = None
    for n, dt, e in rows:
        jstate.apply_event(state, dt, e)
        first_ts = first_ts or dt
        last_ts = dt
        ev = e.get("event")
        shutdown = shutdown or ev == "Shutdown"
        if n <= start or ev in IGNORED_EVENTS:
            continue
        station = state.station if state.docked else None
        null_fid = null_fid or state.fid is None
        batch.append({"file_id": jf.id, "line_no": n, "fid": state.fid, "ts": dt, "event": str(ev)[:60],
                      "system_id64": state.system_address, "system_name": state.system and state.system[:120],
                      "body": state.body and state.body[:120],
                      "station": station and station["name"][:120], "market_id": station and station.get("market_id"),
                      "data": e})
        if len(batch) >= BATCH:
            session.execute(insert, batch)
            inserted += len(batch)
            batch = []
    if batch:
        session.execute(insert, batch)
        inserted += len(batch)
    if state.fid:
        if null_fid or jf.fid != state.fid:
            session.execute(update(JournalEvent.__table__).where(JournalEvent.file_id == jf.id,
                                                                  JournalEvent.fid.is_(None)).values(fid=state.fid))
        cmdr = session.get(JournalCommander, state.fid)
        if cmdr is None:
            cmdr = JournalCommander(fid=state.fid, first_seen=first_ts, last_seen=last_ts)
            session.add(cmdr)
        cmdr.name = state.commander or cmdr.name
        cmdr.first_seen = min(filter(None, (cmdr.first_seen, first_ts)), default=None)
        cmdr.last_seen = max(filter(None, (cmdr.last_seen, last_ts)), default=None)
    jf.fid = state.fid or jf.fid
    jf.size_bytes, jf.lines_read = size, final
    jf.first_ts, jf.last_ts = first_ts, last_ts
    jf.complete = shutdown or not is_newest
    jf.ingested_at = _now()
    session.commit()
    return inserted


def sync(session, directory, names=None, rebuild=False):
    """
    Ingest new journal lines. names limits the files (default: everything not up to date; rebuild: everything).
    Commits per file. Returns {'files', 'events', 'seconds'}.
    """
    started = time.time()
    files = jfiles.find_journal_files(directory)
    newest = os.path.basename(files[-1]) if files else None
    if names is None:
        names = [os.path.basename(p) for p in files] if rebuild else compare(session, directory)["to_ingest"]
    todo = [p for p in files if os.path.basename(p) in set(names)]
    events = 0
    for path in todo:
        events += _ingest_file(session, path, os.path.basename(path) == newest, rebuild)
    seconds = round(time.time() - started, 2)
    if todo:
        store.set_meta(session, META_INGEST_AT, int(time.time()))
        store.set_meta(session, META_INGEST_SECONDS, seconds)
        store.set_meta(session, META_INGEST_EVENTS, events)
        store.set_meta(session, META_ERROR, "")
        session.commit()
    return {"files": len(todo), "events": events, "seconds": seconds}


# ---- on-demand freshness gate --------------------------------------------------------------------------------------

_writer_engines = {}


def _writer_engine(url):
    if url not in _writer_engines:
        _writer_engines[url] = database.make_engine(url)
    return _writer_engines[url]


def _url_of(session_or_engine):
    bind = session_or_engine.get_bind() if hasattr(session_or_engine, "get_bind") else session_or_engine
    return bind.url.render_as_string(hide_password=False)


def _lock_for(url, name):
    """A file lock for this database, or None where no lock file makes sense (in-memory SQLite)."""
    if url.startswith("sqlite") and config.sqlite_path(url) is None:
        return None
    return lock.ListenerLock(config.named_lock_path(url, name))


def _acquire(lk, wait):
    deadline = time.time() + wait
    while not lk.acquire():
        if time.time() >= deadline:
            raise IngestBusy("another journal ingest is running")
        time.sleep(0.2)


def _run_locked(ws, directory, names, wait, rebuild=False):
    lk = _lock_for(_url_of(ws), "journal-ingest")
    if lk is not None:
        _acquire(lk, wait)
    try:
        return sync(ws, directory, names, rebuild)
    finally:
        if lk is not None:
            lk.release()


def _short_error(e):
    msg = str(getattr(e, "orig", e) or e).strip().splitlines()[0][:200]
    if any(k in msg.lower() for k in ("permission denied", "readonly", "read-only", "read only")):
        msg += (f" - the database connection cannot write; set {config.ENV_INGEST_URL} to a writer URL "
                f"or run `python -m eddn_helper journal-sync`")
    return msg


def ensure_fresh(session, directory=None, wait=10.0):
    """
    Bring the database up to date with the journal files if they are ahead of it, then report:
    {db_behind_journal, files_ingested_now, events_ingested_now, last_event_in_db, newest_journal_write, ingest_error}.
    Never raises for write problems (read-only role, busy lock): the query then answers from stored data.
    """
    directory = jfiles.journal_dir(directory)
    out = {"db_behind_journal": False, "files_ingested_now": 0, "events_ingested_now": 0, "ingest_error": None}
    files = jfiles.find_journal_files(directory)
    if not files:
        out["ingest_error"] = f"no journal files found in {directory}"
    else:
        plan = compare(session, directory)
        if plan["behind"]:
            try:
                writer_url = config.ingest_database_url()
                if writer_url:
                    with database.make_session_factory(_writer_engine(writer_url))() as ws:
                        stats = _run_locked(ws, directory, plan["to_ingest"], wait)
                else:
                    stats = _run_locked(session, directory, plan["to_ingest"], wait)
                out["files_ingested_now"], out["events_ingested_now"] = stats["files"], stats["events"]
            except IngestBusy as e:
                out["ingest_error"] = str(e)
            except DBAPIError as e:
                session.rollback()
                out["ingest_error"] = _short_error(e)
                _record_error(session, out["ingest_error"])
            session.rollback()          # end the read snapshot so the query sees what was just written
            out["db_behind_journal"] = compare(session, directory)["behind"]
        try:
            out["newest_journal_write"] = jfiles.iso(datetime.fromtimestamp(os.stat(files[-1]).st_mtime, tz=timezone.utc))
        except OSError:
            pass
    out["last_event_in_db"] = jfiles.iso(session.execute(select(func.max(JournalEvent.ts))).scalar_one())
    return out


def _record_error(session, message):
    """Best effort: remember the last ingest error (fails silently when the connection is read-only)."""
    try:
        store.set_meta(session, META_ERROR, message)
        session.commit()
    except DBAPIError:
        session.rollback()


# ---- service -------------------------------------------------------------------------------------------------------

def watch(db_url, directory=None, interval=30.0, once=False, log=print):
    """
    The ingester service loop: sync every `interval` seconds, keep a heartbeat. Returns False if another ingester
    already holds the service lock. `once` runs a single pass (used by tests).
    """
    directory = jfiles.journal_dir(directory)
    url = config.database_url(db_url)
    svc = _lock_for(url, "journal-ingester")
    if svc is not None and not svc.acquire():
        return False
    engine = database.make_engine(url)
    try:
        migrate.ensure_current(engine)
        factory = database.make_session_factory(engine)
        while True:
            try:
                with factory() as session:
                    stats = _run_locked(session, directory, None, wait=60)
                    store.set_meta(session, META_HEARTBEAT, int(time.time()))
                    session.commit()
                if stats["files"]:
                    log(f"ingested {stats['events']} events from {stats['files']} file(s) in {stats['seconds']}s")
            except (IngestBusy, DBAPIError, OSError) as e:
                log(f"ingest pass failed: {e}")
            if once:
                return True
            time.sleep(interval)
    finally:
        engine.dispose()
        if svc is not None:
            svc.release()


# ---- status --------------------------------------------------------------------------------------------------------

def can_write(session):
    """Whether journal rows can be written (through the ingest URL if set, else the query connection)."""
    url = config.ingest_database_url()
    bind = _writer_engine(url) if url else session.get_bind()
    if bind.dialect.name == "postgresql":
        with bind.connect() as conn:
            ok = conn.exec_driver_sql("SELECT has_table_privilege(current_user, 'journal_event', 'INSERT')").scalar()
            conn.rollback()
        return bool(ok)
    path = config.sqlite_path(bind.url.render_as_string(hide_password=False))
    return True if path is None else os.access(path, os.W_OK)


def task_state(name=TASK_NAME):
    """State of the optional scheduled task ('Running', 'Ready', 'Disabled', ...) or None (absent / not Windows)."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"(Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop).State"],
                           capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def journal_status(session, directory=None, check_task=True):
    """Everything needed to judge whether journal data in the database can be trusted right now."""
    directory = jfiles.journal_dir(directory)
    files = jfiles.find_journal_files(directory)
    plan = compare(session, directory) if files else None
    url = _url_of(session)
    writable = can_write(session)
    n_events = session.execute(select(func.count()).select_from(JournalEvent)).scalar_one()
    cmdrs = session.execute(
        select(JournalCommander.fid, JournalCommander.name, JournalCommander.last_seen,
               select(func.count()).select_from(JournalEvent).where(JournalEvent.fid == JournalCommander.fid)
               .scalar_subquery())).all()

    def meta(key):
        v = store.get_meta(session, key)
        return v if v not in (None, "") else None

    at, hb = meta(META_INGEST_AT), meta(META_HEARTBEAT)
    svc_lock = _lock_for(url, "journal-ingester")
    running, pid = lock.holder(svc_lock.path) if svc_lock is not None else (False, None)
    if not files:
        state = "no_journals"
    elif not plan["behind"]:
        state = "current"
    else:
        state = "behind" if writable else "no_writer"
    newest = None
    if files:
        try:
            newest = {"name": os.path.basename(files[-1]),
                      "modified": jfiles.iso(datetime.fromtimestamp(os.stat(files[-1]).st_mtime, tz=timezone.utc))}
        except OSError:
            pass
    return {
        "state": state, "journal_dir": directory, "journal_dir_exists": os.path.isdir(directory),
        "files_on_disk": len(files), "files_ingested": plan["stored_files"] if plan else 0,
        "files_to_ingest": plan["to_ingest"] if plan else [], "bytes_behind": plan["bytes_behind"] if plan else 0,
        "files_gone_from_disk": len(plan["gone"]) if plan else 0, "newest_journal_file": newest,
        "events": n_events, "newest_event_in_db": jfiles.iso(session.execute(select(func.max(JournalEvent.ts))).scalar_one()),
        "commanders": [{"fid": f, "name": n, "last_seen": jfiles.iso(ls), "events": c} for f, n, ls, c in cmdrs],
        "database_writable": writable, "ingest_url_configured": bool(config.ingest_database_url()),
        "last_ingest_minutes_ago": round((time.time() - int(at)) / 60, 1) if at else None,
        "last_ingest_seconds": meta(META_INGEST_SECONDS), "last_ingest_events": meta(META_INGEST_EVENTS),
        "last_error": meta(META_ERROR),
        "ingester": {"running": running, "pid": pid,
                     "heartbeat_seconds_ago": round(time.time() - int(hb)) if hb else None,
                     "scheduled_task": task_state() if check_task else None},
    }
