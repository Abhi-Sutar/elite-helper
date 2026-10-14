"""The Recce HTTP server: 127.0.0.1 only, pages and JSON for dashboards, server-sent events for live updates.

One Runner thread per open dashboard polls the database cheaply and re-runs the full query through a worker pool
(never on a request thread), so a slow query cannot block the server. All queries are reads on the reader connection.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import database, migrate
from . import config as rcfg
from . import kinds
from .core import Core, CoreError, parse_ts
from .refresh import RefreshGate

POLL_SECONDS = 5            # how often the cheap "did the database move?" check runs
ACTIVE_POLL_SECONDS = 0.25  # while a full query is running, how often to look for its result
CHECK_TIMEOUT = 15          # the stamp check and the position lookup
SNAPSHOT_TIMEOUT = 90       # a full query (live fetch included) slower than this is reported as an error
ORIGIN_RECHECK_S = 15
IDLE_STOP_S = 900           # a dashboard nobody has looked at for this long stops polling
KEEPALIVE_S = 15
PAGE = Path(__file__).with_name("page.html")
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class DbFactory:
    """Opens the reader engine on first use (and retries after a failure, so a database that was down at launch
    is picked up later)."""

    def __init__(self):
        self._factory = None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._factory is None:
                engine = database.make_engine(rcfg.database_url())
                migrate.ensure_current(engine)
                self._factory = database.make_session_factory(engine)
            return self._factory


# ---- one dashboard's live state ----------------------------------------------------------------------------------

class Runner:
    def __init__(self, name, core, poll_seconds=POLL_SECONDS, on_idle=None):
        self.name, self.core, self.poll = name, core, poll_seconds
        self.on_idle = on_idle
        self.cond = threading.Condition()
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"recce-{name}")
        self.cfg = None
        self.cfg_stamp = "unset"
        self.cfg_error = None
        self.cfg_version = 0
        self.data = None
        self.data_version = 0
        self.status_version = 0
        self._status_key = None
        self.gate = RefreshGate(20)
        self.inflight = None            # (future, token, started monotonic, cfg_version)
        self.error = None               # last failure message, cleared by the next success
        self.error_kind = None          # "check", "snapshot" or "config": which success clears it
        self.slow_reported = False
        self.last_check_at = None
        self.last_refresh_at = None
        self.last_attempt_at = None
        self.db_newest = None
        self.last_access = time.monotonic()
        self.current_system = None
        self._origin_token = None
        self._origin_checked = 0.0
        self.thread = threading.Thread(target=self._loop, name=f"recce-poll-{name}", daemon=True)

    def start(self):
        self._reload_config()           # so /api/config answers at once, before the first poll
        self.thread.start()
        return self

    def touch(self):
        self.last_access = time.monotonic()

    def shutdown(self):
        self.stop.set()
        with self.cond:
            self.cond.notify_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- published state
    def versions(self):
        return (self.cfg_version, self.data_version, self.status_version)

    def config_payload(self):
        cfg = self.cfg
        return {"name": self.name, "kind": kinds.for_cfg(cfg).name if cfg else "recce", "config": cfg,
                "labels": kinds.for_cfg(cfg).labels(cfg) if cfg else {},
                "error": self.cfg_error, "dashboards": rcfg.list_names()}

    def status_payload(self):
        now = now_utc()
        refresh = self.cfg["refresh_seconds"] if self.cfg else None
        state = "live"
        if self.error:
            state = "error"
        elif self.data is None:
            state = "loading"
        elif self.db_newest and self.cfg:
            age_min = (now - parse_ts(self.db_newest)).total_seconds() / 60
            if age_min > self.cfg["stale_after_minutes"]:
                state = "stale"
        return {"state": state, "error": self.error, "refresh_seconds": refresh,
                "last_check_at": iso(self.last_check_at), "last_refresh_at": iso(self.last_refresh_at),
                "last_attempt_at": iso(self.last_attempt_at), "db_newest_faction_update": self.db_newest,
                "db_newest": self.db_newest,
                "current_system": self.current_system,
                "server_time": iso(now)}

    def data_payload(self):
        out = dict(self.data) if self.data else {"dashboard": self.name, "rows": [], "count": 0}
        out["status"] = self.status_payload()
        return out

    def _bump(self, what):
        with self.cond:
            if what == "config":
                self.cfg_version += 1
            elif what == "data":
                self.data_version += 1
            self.status_version += 1
            self.cond.notify_all()

    def _publish_status(self):
        s = self.status_payload()
        key = (s["state"], s["error"], s["db_newest_faction_update"], s["current_system"])
        if key != self._status_key:
            self._status_key = key
            with self.cond:
                self.status_version += 1
                self.cond.notify_all()

    def _fail(self, msg, kind):
        self.error, self.error_kind = msg, kind

    def _clear(self, *kinds):
        if not kinds or self.error_kind in kinds:
            self.error = self.error_kind = None

    # -- the poll loop
    def _loop(self):
        while not self.stop.is_set():
            try:
                self._tick()
            except Exception as e:                      # never let the poller die
                self._fail(f"{type(e).__name__}: {e}", "check")
                self._publish_status()
            if time.monotonic() - self.last_access > IDLE_STOP_S:
                self.stop.set()
                if self.on_idle:
                    self.on_idle(self)
                break
            self.stop.wait(ACTIVE_POLL_SECONDS if self.inflight else self.poll)

    def _call(self, fn, *args, timeout=CHECK_TIMEOUT):
        return self.pool.submit(fn, *args).result(timeout=timeout)

    def _reload_config(self):
        """True when the config (re)loaded, which forces a fresh query and tells open pages to reload it."""
        st = rcfg.stamp(self.name)
        if st == self.cfg_stamp:
            return False
        self.cfg_stamp = st
        if st is None:
            self.cfg_error = f"dashboard {self.name!r} no longer exists"
            self._fail(self.cfg_error, "config")
            self._bump("config")
            return False
        try:
            self.cfg = rcfg.load(self.name)
            self.cfg_error = None
            self._clear("config")
            self.gate.set_interval(self.cfg["refresh_seconds"])
        except rcfg.ConfigError as e:
            self.cfg_error = str(e)
            self._fail(f"invalid config: {e}", "config")      # the last good config keeps running
        self._bump("config")
        return self.cfg_error is None

    def _tick(self):
        changed_cfg = self._reload_config()
        if self.cfg is None:
            self._publish_status()
            return
        self._harvest()
        if self.inflight:
            self._publish_status()
            return
        mono = time.monotonic()
        try:
            stamps = self._call(self.core.stamps)
        except Exception as e:
            self._fail(f"database check failed: {_brief(e)}", "check")
            self._publish_status()
            return
        self._clear("check")
        self.last_check_at = now_utc()
        kind = kinds.for_cfg(self.cfg)
        self.db_newest = stamps[kind.stamp_key]
        if mono - self._origin_checked >= ORIGIN_RECHECK_S:
            self._origin_checked = mono
            try:
                self.current_system = self._call(self.core.current_system)
            except Exception:
                pass                                        # keep the last known one; the snapshot reports problems
        follow = kind.follows_position(self.cfg)
        self._origin_token = self.current_system if follow else None       # an anchored dashboard never moves
        token = (stamps[kind.stamp_key], self._origin_token)
        if changed_cfg or self.gate.should_refresh(token, mono):
            future = self.pool.submit(kind.snapshot, self.core, self.cfg)
            self.inflight = (future, token, mono, self.cfg_version)
            self.slow_reported = False
            self.last_attempt_at = now_utc()
        self._publish_status()

    def _harvest(self):
        if not self.inflight:
            return
        future, token, started, version = self.inflight
        mono = time.monotonic()
        if not future.done():
            if mono - started > SNAPSHOT_TIMEOUT and not self.slow_reported:
                self.slow_reported = True
                self._fail(f"the query is taking longer than {SNAPSHOT_TIMEOUT} s; showing the last good data",
                           "snapshot")
            return
        self.inflight = None
        try:
            data = future.result()
        except Exception as e:
            self.gate.failed(mono)
            self._fail(_brief(e), "snapshot")
            return
        if version != self.cfg_version:                     # config changed while it ran: run again with the new one
            self.gate.last_at = None
            return
        self.data = data
        self.gate.done(token, mono)
        self._clear("snapshot", "check")
        self.last_refresh_at = now_utc()
        self.db_newest = data["db"][kinds.for_cfg(self.cfg).stamp_key]
        self._bump("data")


def _brief(e):
    text = str(e).strip() or type(e).__name__
    if not isinstance(e, (CoreError, FutureTimeout)):
        text = f"{type(e).__name__}: {text}"
    if isinstance(e, FutureTimeout):
        text = "timed out waiting for the database"
    return text[:400]


class Manager:
    def __init__(self, core, poll_seconds=POLL_SECONDS):
        self.core, self.poll = core, poll_seconds
        self.runners = {}
        self.lock = threading.Lock()

    def get(self, name):
        if not rcfg.exists(name):
            return None
        with self.lock:
            r = self.runners.get(name)
            if r is None or r.stop.is_set():
                r = self.runners[name] = Runner(name, self.core, self.poll, on_idle=self.forget).start()
            r.touch()
            return r

    def forget(self, runner):
        with self.lock:
            if self.runners.get(runner.name) is runner:
                del self.runners[runner.name]
        runner.shutdown()

    def shutdown(self):
        with self.lock:
            runners = list(self.runners.values())
            self.runners.clear()
        for r in runners:
            r.shutdown()


# ---- HTTP --------------------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Recce/1"

    def log_message(self, *args):
        pass

    # helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in LOCAL_HOSTS            # refuses DNS-rebinding style requests

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        name = (q.get("d") or [None])[0]
        path = url.path
        if path == "/api/health":
            return self._send(200, self.server.health())
        if path == "/api/dashboards":
            return self._send(200, [{"name": n, "url": self.server.url_for(n)} for n in rcfg.list_names()])
        if path in ("/", "/index.html"):
            if not name:
                default = rcfg.default_name()
                if default is None:
                    return self._send(404, {"error": "no dashboards yet; run: elite-dash new <name>"})
                return self._send(302, b"", "text/plain", {"Location": f"/?d={quote(default)}"})
            page = PAGE
            try:
                page = kinds.get(rcfg.kind_of(name)).page
            except (rcfg.ConfigError, KeyError):
                pass                                    # unknown kind or missing dashboard: the page reports it
            try:
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": f"{page.name} is missing"})
        if path in ("/api/config", "/api/data", "/events"):
            runner = self.server.manager.get(name) if name else None
            if runner is None:
                return self._send(404, {"error": f"no dashboard named {name!r}" if name else "add ?d=<dashboard>"})
            if path == "/api/config":
                return self._send(200, runner.config_payload())
            if path == "/api/data":
                return self._send(200, runner.data_payload())
            return self._events(runner)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if urlparse(self.path).path == "/api/shutdown" and self.headers.get("X-Recce") == "1":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if urlparse(self.path).path == "/api/radius" and self.headers.get("X-Recce") == "1":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("d") or [None])[0]
            if not name or not rcfg.exists(name):
                return self._send(404, {"error": f"no dashboard named {name!r}" if name else "add ?d=<dashboard>"})
            try:
                ly = float((q.get("ly") or [""])[0])
            except ValueError:
                return self._send(400, {"error": "radius_ly: must be a number"})
            try:
                cfg = rcfg.update(name, {"radius_ly": int(ly) if ly.is_integer() else ly})
            except rcfg.ConfigError as e:
                return self._send(400, {"error": "; ".join(e.errors)})
            return self._send(200, {"name": name, "radius_ly": cfg["radius_ly"]})
        if urlparse(self.path).path == "/api/player-only" and self.headers.get("X-Recce") == "1":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("d") or [None])[0]
            if not name or not rcfg.exists(name):
                return self._send(404, {"error": f"no dashboard named {name!r}" if name else "add ?d=<dashboard>"})
            on = {"1": True, "true": True, "0": False, "false": False}.get((q.get("on") or [""])[0].lower())
            if on is None:
                return self._send(400, {"error": "on: must be 1 or 0"})
            try:
                cfg = rcfg.update(name, {"filters": {"player_faction_only": on}})
            except rcfg.ConfigError as e:
                return self._send(400, {"error": "; ".join(e.errors)})
            return self._send(200, {"name": name, "player_faction_only": cfg["filters"]["player_faction_only"]})
        if urlparse(self.path).path == "/api/recentre" and self.headers.get("X-Recce") == "1":
            name = (parse_qs(urlparse(self.path).query).get("d") or [None])[0]
            runner = self.server.manager.get(name) if name else None
            if runner is None:
                return self._send(404, {"error": f"no dashboard named {name!r}" if name else "add ?d=<dashboard>"})
            try:
                return self._send(200, runner._call(self.server.manager.core.recentre, name))
            except (CoreError, rcfg.ConfigError, KeyError) as e:
                return self._send(409, {"error": str(e.args[0]) if isinstance(e, KeyError) else str(e)})
            except FutureTimeout:
                return self._send(504, {"error": "timed out reading the journal"})
        return self._send(404, {"error": "not found"})

    def _events(self, runner):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = (-1, -1, -1)
        try:
            while not runner.stop.is_set() and not self.server.stopping:
                with runner.cond:
                    runner.cond.wait_for(lambda: runner.versions() != seen or runner.stop.is_set(),
                                         timeout=KEEPALIVE_S)
                    cur = runner.versions()
                runner.touch()
                out = []
                if cur[0] != seen[0]:
                    out.append(_sse("config", runner.config_payload()))
                if cur[1] != seen[1] and runner.data is not None:
                    out.append(_sse("data", runner.data_payload()))
                elif cur[2] != seen[2]:
                    out.append(_sse("status", runner.status_payload()))
                self.wfile.write("".join(out).encode("utf-8") if out else b": keepalive\n\n")
                self.wfile.flush()
                seen = cur
        except (BrokenPipeError, ConnectionError, OSError):
            return


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


class RecceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, core, poll_seconds=POLL_SECONDS):
        super().__init__(("127.0.0.1", port), Handler)
        self.manager = Manager(core, poll_seconds)
        self.started = iso(now_utc())
        self.stopping = False

    @property
    def port(self):
        return self.server_address[1]

    def url_for(self, name):
        return f"http://127.0.0.1:{self.port}/?d={quote(name)}"

    def health(self):
        return {"app": "recce", "pid": os.getpid(), "port": self.port, "started": self.started}

    def shutdown(self):
        self.stopping = True
        self.manager.shutdown()
        super().shutdown()


# ---- running one server per machine ------------------------------------------------------------------------------

def info_path():
    return rcfg.root_dir() / "server.json"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))      # never route localhost via a proxy


def probe(port, timeout=1.5):
    """The /api/health reply of a Recce server on this port, or None."""
    try:
        with _opener().open(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if data.get("app") == "recce" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def running_server():
    """{'pid','port','url'} of the live server recorded in server.json, or None (stale files are ignored)."""
    try:
        info = json.loads(info_path().read_text(encoding="utf-8"))
        port = int(info["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    health = probe(port)
    if health is None:
        return None
    return {"pid": health.get("pid"), "port": port, "base_url": f"http://127.0.0.1:{port}"}


def dashboard_url(base_url, name):
    return f"{base_url}/?d={quote(name)}"


def _write_info(srv):
    p = info_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("server.json.tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "port": srv.port, "started": srv.started}), encoding="utf-8")
    os.replace(tmp, p)


def serve(port=0, quiet=False, out=print):
    """Run the server in the foreground. Returns 0; if one is already running that one is reused and this returns."""
    rcfg.ensure_default()
    existing = running_server()
    if existing:
        out(f"Recce is already running at {existing['base_url']}/")
        return 0
    core = Core(DbFactory())
    try:
        srv = RecceServer(int(port), core, float(os.environ.get("ELITE_HELPER_RECCE_POLL", POLL_SECONDS)))
    except OSError as e:
        out(f"Cannot listen on port {port}: {e}")
        return 1
    _write_info(srv)
    if not quiet:
        out(f"Recce dashboards at http://127.0.0.1:{srv.port}/ (Ctrl+C to stop)")
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping = True
        srv.manager.shutdown()
        srv.server_close()
        try:
            info = json.loads(info_path().read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError):
            pass
    return 0


def ensure_server(port=0, wait=15.0):
    """Reuse the running server or start a detached one. Returns {'base_url','port','pid','started'}; raises
    RuntimeError if it does not come up. A start lock keeps two simultaneous launches from starting two servers."""
    found = running_server()
    if found:
        return {**found, "started": False}
    rcfg.root_dir().mkdir(parents=True, exist_ok=True)
    lock = rcfg.root_dir() / "server.lock"
    deadline = time.monotonic() + wait
    have_lock = False
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            have_lock = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()                       # left by a launcher that died
                    continue
            except OSError:
                pass
            found = running_server()
            if found:
                return {**found, "started": False}
            time.sleep(0.2)
    if not have_lock:
        raise RuntimeError("another launch is still starting the Recce server")
    try:
        found = running_server()
        if found:
            return {**found, "started": False}
        log = open(rcfg.root_dir() / "server.log", "ab")
        cmd = [sys.executable, "-m", "eddn_helper.recce", "serve", "--quiet"] + (["--port", str(port)] if port else [])
        kw = {"cwd": str(Path(__file__).resolve().parents[2]), "stdin": subprocess.DEVNULL, "stdout": log,
              "stderr": subprocess.STDOUT, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kw)
        log.close()
        while time.monotonic() < deadline:
            found = running_server()
            if found:
                return {**found, "started": True}
            if proc.poll() is not None:
                raise RuntimeError(f"the Recce server exited with code {proc.returncode}; see "
                                   f"{rcfg.root_dir() / 'server.log'}")
            time.sleep(0.2)
        raise RuntimeError(f"the Recce server did not start within {wait:.0f} s; see {rcfg.root_dir() / 'server.log'}")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


SERVE_RE = re.compile(r"eddn_helper\.recce\s+serve\b")


def _cim_time(value):
    """ConvertTo-Json writes dates as '/Date(1791889570752)/' (UTC milliseconds); show them as local time."""
    m = re.search(r"-?\d+", str(value or ""))
    if not m:
        return ""
    return datetime.fromtimestamp(int(m.group()) / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _process_table():
    """[{pid, parent, started, cmd}] for every process on the machine. Windows: PowerShell/CIM; elsewhere: ps."""
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine "
              "| ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        rows = json.loads(out or "[]")
        rows = rows if isinstance(rows, list) else [rows]
        return [{"pid": int(r["ProcessId"]), "parent": int(r["ParentProcessId"] or 0),
                 "started": _cim_time(r.get("CreationDate")), "cmd": r.get("CommandLine") or ""}
                for r in rows if r.get("ProcessId") is not None]
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,lstart=,args="], capture_output=True, text=True, timeout=30).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[0].isdigit() and parts[1].isdigit():
            rows.append({"pid": int(parts[0]), "parent": int(parts[1]), "started": " ".join(parts[2:7]),
                         "cmd": parts[7]})
    return rows


def find_servers(table=None):
    """
    Every running `eddn_helper.recce serve` as a group: {"root", "pids", "started", "registered"}. On Windows a venv
    python starts the real interpreter as a child, so a server is one group of two processes; the root is the one
    whose parent is not itself a Recce server. `registered` is true for the group holding the pid in server.json.
    """
    table = _process_table() if table is None else table
    procs = {p["pid"]: p for p in table if SERVE_RE.search(p["cmd"]) and p["pid"] != os.getpid()}
    try:
        reg = int(json.loads(info_path().read_text(encoding="utf-8")).get("pid"))
    except (OSError, ValueError, TypeError):
        reg = None
    groups = []
    for pid, p in sorted(procs.items()):
        if p["parent"] in procs:
            continue
        pids = [pid] + [c for c, q in procs.items() if q["parent"] == pid]
        groups.append({"root": pid, "pids": pids, "started": p["started"], "registered": reg in pids})
    return groups


def kill_server(group):
    """Force-stop a server group and its children."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(group["root"]), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        for pid in reversed(group["pids"]):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def stop_server(wait=8.0):
    """Ask the running server to exit. Returns True if one was running."""
    found = running_server()
    if not found:
        return False
    req = urllib.request.Request(f"{found['base_url']}/api/shutdown", data=b"", method="POST",
                                 headers={"X-Recce": "1"})
    try:
        _opener().open(req, timeout=3).read()
    except (OSError, urllib.error.URLError):
        pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if probe(found["port"], timeout=0.5) is None:
            break
        time.sleep(0.2)
    try:
        info_path().unlink()
    except OSError:
        pass
    return True
