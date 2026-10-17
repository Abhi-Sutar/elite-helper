"""Subscribe to the EDDN ZeroMQ relay and persist commodity / faction data."""

import contextlib
import json
import logging
import logging.handlers
import sys
import time
import zlib

from . import config, database, migrate, parsers, store
from .lock import ListenerLock, holder

RELAY = "tcp://eddn.edcd.io:9500"
RCV_HWM = 10_000           # frames ZeroMQ queues while we are busy (about 12 minutes of traffic)
IDLE_TIMEOUT_S = 90        # reconnect if the relay is silent this long (EDDN normally sends many msgs/s)
COMMIT_EVERY_S = 2.0
COMMIT_EVERY_N = 500
PRUNE_EVERY_S = 3600
DB_RETRY_MAX_S = 300       # keep retrying the database at start-up (e.g. server still booting)
DB_OUTAGE_BACKOFF_MAX_S = 30   # while a commit keeps failing, wait this long at most between attempts
SLOW_S = 1.0               # warn when a database step takes longer than this
LOG_FAILURE_EVERY_S = 60   # one traceback per exception type per this long

log = logging.getLogger("eddn_helper")
_last_logged = {}


def setup_logging(log_file=None):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if log_file:
        h = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(fmt)
        log.addHandler(h)
    if sys.stderr is not None:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        log.addHandler(h)


def handle_raw(session, raw):
    """Decode one raw relay frame and apply it. Returns 'market' | 'system' | None."""
    try:
        obj = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        return None
    parsed = parsers.parse_message(obj)
    if parsed and store.apply_parsed(session, parsed):
        return parsed["kind"]
    return None


def new_stats():
    """Counters plus the slowest apply / commit / prune seen since the last stats line."""
    return dict.fromkeys(("received", "market", "system", "errors", "lost", "rollbacks", "reconnects",
                          "apply_s", "commit_s", "prune_s"), 0)


def log_failure(message):
    """log.exception, but once per exception type per minute so a repeating fault cannot flood the log."""
    kind, now = sys.exc_info()[0], time.monotonic()
    if now - _last_logged.get(kind, -LOG_FAILURE_EVERY_S) >= LOG_FAILURE_EVERY_S:
        _last_logged[kind] = now
        log.exception(message)


@contextlib.contextmanager
def timed(name, stats):
    """Keep the slowest duration under stats[name] and warn about slow steps."""
    start = time.monotonic()
    try:
        yield
    finally:
        took = time.monotonic() - start
        stats[name] = max(stats[name], took)
        if took > SLOW_S:
            log.warning("slow %s: %.1fs", name.removesuffix("_s"), took)


def apply_one(session, raw, stats):
    """Apply one frame inside a SAVEPOINT, so a failure costs only that message and not the open batch."""
    try:
        with timed("apply_s", stats), session.begin_nested():
            return handle_raw(session, raw)
    except Exception:  # noqa: BLE001 - one bad message must not kill the listener
        log_failure("failed to apply a message; skipping it")
        store.reset_caches(session)
        stats["errors"] += 1
        return None


def commit_batch(session, frames, stats, last_recv):
    """
    Commit the open batch. If that fails, roll back and replay its frames once on a fresh transaction; frames
    that still do not commit are counted as lost. Returns whether the commit went through; never raises.
    """
    for attempt in range(2):
        try:
            if attempt:
                for raw in frames:
                    apply_one(session, raw, stats)
            with timed("commit_s", stats):
                store.set_meta(session, "listener_heartbeat", int(time.time()))
                if stats["received"]:
                    store.set_meta(session, "last_message_at", int(last_recv))
                session.commit()
            return True
        except Exception:  # noqa: BLE001
            log_failure("commit failed")
            session.rollback()
            store.reset_caches(session)
            stats["rollbacks"] += 1
    stats["lost"] += len(frames)
    return False


def connect_with_retry(url):
    """Create the engine, wait for the database to accept connections, then migrate it to head."""
    delay, waited = 1, 0
    while True:
        try:
            engine = database.make_engine(url)
            with engine.connect():
                pass
            migrate.upgrade(engine)
            return engine
        except migrate.SchemaError:
            raise
        except Exception as e:  # noqa: BLE001 - any connection-level failure: back off and retry
            if waited >= DB_RETRY_MAX_S:
                raise
            log.warning("database not ready (%s); retrying in %ss", type(e).__name__, delay)
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 30)


def run(db_url=None, relay=RELAY, log_file=None, stats_every_s=60):
    import zmq  # imported lazily so the query side works without pyzmq

    setup_logging(log_file)
    url = config.database_url(db_url)
    lock = ListenerLock(config.lock_path(url))
    if not lock.acquire():
        _, pid = holder(lock.path)
        log.error("another listener is already running (pid %s) for %s", pid, database.masked_url(url))
        return 3
    try:
        engine = connect_with_retry(url)
    except Exception:
        lock.release()
        raise
    session = database.make_session_factory(engine)()
    stats = new_stats()
    now = time.time()
    last_commit = last_stats = last_recv = now
    last_prune = 0.0
    frames = []                # raw frames of the open batch, kept so a failed commit can be replayed
    retry_at, outage_delay = 0.0, 1
    ctx = zmq.Context()
    sock = None
    backoff = 1
    mode = store.history_mode(engine)
    log.info("listening to %s, db=%s, history mode=%s, rcvhwm=%s", relay, database.masked_url(url), mode, RCV_HWM)
    if mode == "full" and engine.dialect.name == "sqlite":
        log.warning("full history on SQLite stores ~40M rows/day; this is meant for PostgreSQL/TimescaleDB")
    try:
        while True:
            if sock is None:
                sock = ctx.socket(zmq.SUB)
                sock.setsockopt(zmq.RCVHWM, RCV_HWM)
                sock.setsockopt(zmq.SUBSCRIBE, b"")
                sock.connect(relay)
            try:
                raw = sock.recv() if sock.poll(1000) else None
                backoff = 1
            except zmq.ZMQError as e:
                log.warning("zmq error (%s); reconnecting in %ss", e, backoff)
                sock.close(linger=0)
                sock = None
                stats["reconnects"] += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            now = time.time()
            if raw is not None:
                last_recv = now
                stats["received"] += 1
                if now < retry_at:     # database outage: do not hammer it, just count what we drop
                    stats["lost"] += 1
                else:
                    kind = apply_one(session, raw, stats)
                    if kind:
                        stats[kind] += 1
                        frames.append(raw)
            elif now - last_recv > IDLE_TIMEOUT_S:
                log.warning("relay silent for %ss; reconnecting", IDLE_TIMEOUT_S)
                sock.close(linger=0)
                sock = None
                stats["reconnects"] += 1
                last_recv = now

            if now >= retry_at and (len(frames) >= COMMIT_EVERY_N or now - last_commit >= COMMIT_EVERY_S):
                committed = commit_batch(session, frames, stats, last_recv)
                if committed:
                    outage_delay = 1
                else:
                    retry_at = now + outage_delay
                    outage_delay = min(outage_delay * 2, DB_OUTAGE_BACKOFF_MAX_S)
                frames.clear()
                last_commit = now
                # Only prune with nothing pending, so a failed prune cannot roll back an open batch.
                if committed and now - last_prune >= PRUNE_EVERY_S:
                    try:
                        with timed("prune_s", stats):
                            removed = store.prune(session)
                            linked = store.link_markets_to_systems(session)
                        if any(removed.values()) or linked:
                            log.info("pruned expired data (commodity history %sd): %s; linked %s markets to systems",
                                     store.RETENTION_DAYS, removed, linked)
                    except Exception:  # noqa: BLE001
                        log_failure("prune failed")
                        session.rollback()
                    last_prune = now
            if now - last_stats >= stats_every_s:
                log.info("received=%s markets=%s systems=%s errors=%s lost=%s rollbacks=%s reconnects=%s "
                         "slowest apply=%.2fs commit=%.2fs prune=%.2fs",
                         stats["received"], stats["market"], stats["system"], stats["errors"], stats["lost"],
                         stats["rollbacks"], stats["reconnects"], stats["apply_s"], stats["commit_s"],
                         stats["prune_s"])
                stats.update(apply_s=0, commit_s=0, prune_s=0)
                last_stats = now
    except KeyboardInterrupt:
        log.info("stopping")
    except Exception:
        log.exception("listener crashed")
        raise
    finally:
        try:
            commit_batch(session, frames, stats, last_recv)
        finally:
            session.close()
            engine.dispose()
            if sock is not None:
                sock.close(linger=0)
            ctx.term()
            lock.release()
    return 0
