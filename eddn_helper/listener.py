"""Subscribe to the EDDN ZeroMQ relay and persist commodity / faction data."""

import json
import logging
import logging.handlers
import sys
import time
import zlib

from . import config, database, migrate, parsers, store
from .lock import ListenerLock, holder

RELAY = "tcp://eddn.edcd.io:9500"
IDLE_TIMEOUT_S = 90        # reconnect if the relay is silent this long (EDDN normally sends many msgs/s)
COMMIT_EVERY_S = 2.0
COMMIT_EVERY_N = 500
PRUNE_EVERY_S = 3600
DB_RETRY_MAX_S = 300       # keep retrying the database at start-up (e.g. server still booting)

log = logging.getLogger("eddn_helper")


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
    counts = {"received": 0, "market": 0, "system": 0, "errors": 0}
    now = time.time()
    last_commit = last_stats = last_recv = now
    last_prune = 0.0
    pending = 0
    ctx = zmq.Context()
    sock = None
    backoff = 1
    mode = store.history_mode(engine)
    log.info("listening to %s, db=%s, history mode=%s", relay, database.masked_url(url), mode)
    if mode == "full" and engine.dialect.name == "sqlite":
        log.warning("full history on SQLite stores ~40M rows/day; this is meant for PostgreSQL/TimescaleDB")
    try:
        while True:
            if sock is None:
                sock = ctx.socket(zmq.SUB)
                sock.setsockopt(zmq.SUBSCRIBE, b"")
                sock.connect(relay)
            try:
                raw = sock.recv() if sock.poll(1000) else None
                backoff = 1
            except zmq.ZMQError as e:
                log.warning("zmq error (%s); reconnecting in %ss", e, backoff)
                sock.close(linger=0)
                sock = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            now = time.time()
            if raw is not None:
                last_recv = now
                counts["received"] += 1
                try:
                    kind = handle_raw(session, raw)
                except Exception:  # noqa: BLE001 - one bad message must not kill the listener
                    log.exception("failed to apply a message; skipping it")
                    session.rollback()
                    store.reset_caches(session)
                    counts["errors"] += 1
                    kind = None
                if kind:
                    counts[kind] += 1
                    pending += 1
            elif now - last_recv > IDLE_TIMEOUT_S:
                log.warning("relay silent for %ss; reconnecting", IDLE_TIMEOUT_S)
                sock.close(linger=0)
                sock = None
                last_recv = now

            if pending >= COMMIT_EVERY_N or now - last_commit >= COMMIT_EVERY_S:
                try:
                    store.set_meta(session, "listener_heartbeat", int(now))
                    if counts["received"]:
                        store.set_meta(session, "last_message_at", int(last_recv))
                    session.commit()
                except Exception:  # noqa: BLE001
                    log.exception("commit failed")
                    session.rollback()
                    store.reset_caches(session)
                pending, last_commit = 0, now
            if now - last_prune >= PRUNE_EVERY_S:
                try:
                    removed = store.prune(session)
                    linked = store.link_markets_to_systems(session)
                    if any(removed.values()) or linked:
                        log.info("pruned data older than %sd: %s; linked %s markets to systems",
                                 store.RETENTION_DAYS, removed, linked)
                except Exception:  # noqa: BLE001
                    log.exception("prune failed")
                    session.rollback()
                last_prune = now
            if now - last_stats >= stats_every_s:
                log.info("received=%s markets=%s systems=%s errors=%s",
                         counts["received"], counts["market"], counts["system"], counts["errors"])
                last_stats = now
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        try:
            session.commit()
        finally:
            session.close()
            engine.dispose()
            if sock is not None:
                sock.close(linger=0)
            ctx.term()
            lock.release()
    return 0
