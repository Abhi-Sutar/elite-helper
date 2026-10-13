#!/usr/bin/env python3
"""CG supply board: a small local server that powers the dashboard.

It calls the eddn_helper package (the same queries the elite-helper MCP server
uses, so SQLite or PostgreSQL both work), works out which large-pad stations near
your delivery target sell each commodity, writes
data.json, and serves index.html + data.json on http://127.0.0.1:<port>.

Run it with the elite-helper virtualenv python (it has sqlalchemy):
    .venv/Scripts/python.exe cg_board/cg_board.py --open   (from the repo root)

Usage:
    python cg_board.py                 # serve the dashboard and refresh in the background
    python cg_board.py once            # refresh data.json one time and exit
    python cg_board.py reset [tab]     # restart the "Change" baseline (all tabs, or one tab key)
    python cg_board.py --open          # also open the browser
    python cg_board.py --config other.json

Everything about the target, ship and commodities lives in config.json.
Only the Python standard library is needed.
"""
import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_FILE = HERE / "data.json"
BASELINE_FILE = HERE / "baseline.json"
LS_CACHE_FILE = HERE / "ls_cache.json"

LARGE_PAD_TYPES = ["Coriolis", "Orbis", "Ocellus", "Bernal", "Dodec", "AsteroidBase"]

DEFAULTS = {
    "helper_dir": str(HERE.parent),  # folder containing the eddn_helper package (the repo root)
    "database_url": None,  # null = whatever eddn_helper uses by default (or ELITE_HELPER_DATABASE_URL)
    "port": 8765,
    "refresh_seconds": 30,
    "max_age_hours": 72,
    "title": "CG supply board",
    "target": {"name": "Zero Squared", "system": "HIP 74116"},
    "ship": {"name": "Le Big Plipper", "laden_ly": 31.34, "unladen_ly": 45.22, "cargo": 1147},
    "station_types": LARGE_PAD_TYPES,
    "min_stock": 1,
    "lookup_ls": True,  # fetch distance-to-star from EDSM for new stations (cached)
    "tabs": [],
}


# ---------------------------------------------------------------- helpers
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def load_config(path):
    user = load_json(path, None)
    if user is None:
        sys.exit("Could not read config file: %s" % path)
    cfg = dict(DEFAULTS)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(cfg[k])
            merged.update(v)
            cfg[k] = merged
        else:
            cfg[k] = v
    if not cfg["tabs"]:
        sys.exit("config has no tabs")
    return cfg


_H = {}  # lazily imported eddn_helper pieces + engine


def helper(cfg):
    """Import the eddn_helper package (the same code the MCP server uses) and open its engine once."""
    if _H:
        return _H
    hd = Path(cfg["helper_dir"]).expanduser()
    if not (hd / "eddn_helper").is_dir():
        sys.exit("eddn_helper not found in %s (set \"helper_dir\" in config.json)" % hd)
    sys.path.insert(0, str(hd))
    if cfg.get("database_url"):
        os.environ["ELITE_HELPER_DATABASE_URL"] = cfg["database_url"]
    try:
        from eddn_helper import database, migrate, models, queries
        from sqlalchemy import select
    except ImportError as e:
        sys.exit("Cannot import eddn_helper (%s). Run this script with the elite-helper virtualenv, e.g.\n"
                 "  %s" % (e, hd / ".venv" / "Scripts" / "python.exe"))
    engine = database.make_engine()
    migrate.ensure_current(engine)  # raises SchemaError with the fix if the schema is out of date
    _H.update(database=database, models=models, queries=queries, select=select,
              factory=database.make_session_factory(engine))
    return _H


def large_pad_stations(h, session, types):
    """({(station, system) lowercase} whose type is in `types`, {same} whose type is unknown), non-carrier markets.
    EDDN market messages carry no station type, so many markets have none until something else reports it."""
    M = h["models"].Market
    q = h["select"](M.station, M.system_name).where(M.is_carrier.is_(False), M.station_type.in_(list(types)))
    known = {(a.lower(), b.lower()) for a, b in session.execute(q)}
    q = h["select"](M.station, M.system_name).where(
        M.is_carrier.is_(False), (M.station_type.is_(None)) | (M.station_type == ""))
    return known, {(a.lower(), b.lower()) for a, b in session.execute(q)}


def norm_type(t):
    return "".join((t or "").split()).lower()


def is_large(edsm_type, types):
    """EDSM names ("Coriolis Starport", "Asteroid base") against the configured types ("Coriolis", "AsteroidBase")."""
    n = norm_type(edsm_type)
    return any(norm_type(t) in n for t in types)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------- EDSM: distance to star, station type
def edsm_station(cache, system, station, budget, enabled, need_type=False):
    """Distance-to-star (Ls) and station type are not reliably in the local database, so ask EDSM once per system and
    remember the answers in ls_cache.json. Returns the cache entry ({"ls", "type"}) or None while unknown."""
    key = system + "|" + station
    hit = cache.get(key)
    if hit and hit.get("ls") is not None and (not need_type or hit.get("type")):
        return hit
    if not enabled or budget[0] <= 0:
        return hit
    if hit and time.time() - hit.get("tried", 0) < 3600:
        return hit
    budget[0] -= 1
    try:
        url = "https://www.edsm.net/api-system-v1/stations?systemName=" + urllib.parse.quote(system)
        req = urllib.request.Request(url, headers={"User-Agent": "cg-board/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.load(r)
        for st in payload.get("stations", []) or []:
            d = st.get("distanceToArrival")
            cache[system + "|" + st["name"]] = {"ls": round(d) if d is not None else None, "type": st.get("type")}
    except Exception as e:  # network trouble is fine, the column just stays blank
        log("EDSM lookup failed for %s: %s" % (system, e))
    got = cache.get(key)
    if got is None or (got.get("ls") is None and not got.get("type")):
        cache[key] = {"ls": None, "type": None, "tried": time.time()}
    return cache[key]


# --------------------------------------------------------------- refresh
def apply_target_change(baselines, system):
    """Baselines only mean something for one target. They remember which system they were measured around under
    "_target"; when the target moves they restart from scratch. A file without the marker is just stamped."""
    prev = baselines.get("_target")
    if prev is not None and prev.lower() != system.lower():
        log("baselines reset: target moved from %s to %s" % (prev, system))
        baselines = {}
    baselines["_target"] = system
    return baselines


def refresh(cfg):
    now = datetime.now(timezone.utc)
    h = helper(cfg)
    queries = h["queries"]
    baselines = apply_target_change(load_json(BASELINE_FILE, {}), cfg["target"]["system"])
    ls_cache = load_json(LS_CACHE_FILE, {})
    budget = [6]
    laden = float(cfg["ship"]["laden_ly"])
    tabs_out, order = {}, []
    types_cache = {}

    with h["database"].session_scope(h["factory"]) as session:
        for tab in cfg["tabs"]:
            key = tab["key"]
            order.append(key)
            if tab.get("max_ly"):
                max_ly = float(tab["max_ly"])
                max_jumps = int(math.ceil(max_ly / laden - 0.01))
            else:
                max_jumps = int(tab.get("max_jumps", 2))
                max_ly = math.ceil(max_jumps * laden * 10) / 10
            types = tuple(tab.get("station_types") or cfg["station_types"])
            if types not in types_cache:
                types_cache[types] = large_pad_stations(h, session, types)
            allowed, unknown_type = types_cache[types]
            min_stock = tab.get("min_stock", cfg["min_stock"])
            max_age = tab.get("max_age_hours", cfg["max_age_hours"])
            cg = tab.get("cg_price")
            base = baselines.setdefault(key, {"baselineAt": iso(now), "base": {}})

            res = queries.find_commodity(
                session, tab["commodity"], mode="buy", near=cfg["target"]["system"], ly=max_ly,
                min_amount=0, max_age_hours=max_age, include_carriers=False, limit=1000000)
            if "error" in res:
                raise RuntimeError("%s: %s" % (key, res["error"]))

            rows = {}
            for r in res["results"]:
                st, sysname = r["station"], r["system"]
                ident = (st.lower(), sysname.lower())
                info, pad_unknown = None, False
                if ident not in allowed:
                    if ident not in unknown_type:
                        continue
                    info = edsm_station(ls_cache, sysname, st, budget, cfg["lookup_ls"], need_type=True)
                    if info and info.get("type"):
                        if not is_large(info["type"], types):
                            continue
                    else:
                        pad_unknown = True  # EDSM has not answered yet: keep it, flagged, rather than hide it
                price, stock = r["price"], r["amount"] or 0
                if cg and price >= cg:
                    continue
                tracked = st in base["base"]
                if stock < min_stock and not tracked:
                    continue
                if not tracked:
                    base["base"][st] = stock
                rows[st] = {
                    "system": sysname,
                    "ly": r["distance_ly"],
                    "ls": (info or edsm_station(ls_cache, sysname, st, budget, cfg["lookup_ls"]) or {}).get("ls"),
                    "pad_unknown": pad_unknown,
                    "stock": stock,
                    "price": price,
                    "age": round(r["age_hours"], 1),
                    "base": base["base"][st],
                }
            tabs_out[key] = {
                "label": tab.get("label", tab["commodity"]),
                "commodity": tab["commodity"],
                "cg": cg,
                "max_ly": max_ly,
                "max_jumps": max_jumps,
                "baselineAt": base["baselineAt"],
                "rows": rows,
            }

    out = {
        "updated": iso(now),
        "order": order,
        "config": {
            "title": cfg["title"],
            "target": cfg["target"],
            "ship": cfg["ship"],
            "station_types": cfg["station_types"],
        },
        "tabs": tabs_out,
    }
    save_json(DATA_FILE, out)
    save_json(BASELINE_FILE, baselines)
    save_json(LS_CACHE_FILE, ls_cache)
    return out


def refresh_loop(cfg, stop, config_path=None):
    """Refresh forever. config_path is re-read every cycle so a changed target (or tab) applies without a restart;
    a broken file keeps the previous settings. port, database_url and helper_dir still need a restart."""
    first = True
    bad_config = False
    while not stop.is_set():
        if config_path:
            try:
                new = load_config(config_path)
                if new["target"]["system"] != cfg["target"]["system"]:
                    log("target changed: %s -> %s" % (cfg["target"]["system"], new["target"]["system"]))
                new["port"] = cfg["port"]
                cfg, bad_config = new, False
            except SystemExit as e:  # load_config exits on an unreadable file; keep running on the last good one
                if not bad_config:
                    log("config reload failed, keeping previous settings: %s" % e)
                bad_config = True
        try:
            out = refresh(cfg)
            if first:
                log("data ready: " + ", ".join(
                    "%s=%d stations" % (k, len(v["rows"])) for k, v in out["tabs"].items()))
                first = False
        except SystemExit:
            raise
        except Exception as e:
            log("refresh failed: %s" % e)
        stop.wait(max(5, int(cfg["refresh_seconds"])))


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            f, ctype = HERE / "index.html", "text/html; charset=utf-8"
        elif path == "/data.json":
            f, ctype = DATA_FILE, "application/json"
        else:
            self.send_error(404)
            return
        try:
            body = f.read_bytes()
        except OSError:
            self.send_error(503, "no data yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="Local server for the CG supply board.")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "once", "reset"])
    ap.add_argument("tab", nargs="?", help="tab key for reset")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port

    if args.command == "reset":
        b = load_json(BASELINE_FILE, {})
        if args.tab:
            b.pop(args.tab, None)
        else:
            b = {}
        save_json(BASELINE_FILE, b)
        log("baseline reset: " + (args.tab or "all tabs"))
        return
    if args.command == "once":
        out = refresh(cfg)
        log("wrote %s (%s)" % (DATA_FILE, ", ".join(
            "%s=%d" % (k, len(v["rows"])) for k, v in out["tabs"].items())))
        return

    stop = threading.Event()
    threading.Thread(target=refresh_loop, args=(cfg, stop, args.config), daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", int(cfg["port"])), Handler)
    except OSError as e:
        sys.exit("Cannot listen on port %s: %s (change \"port\" in config.json)" % (cfg["port"], e))
    url = "http://127.0.0.1:%d/" % cfg["port"]
    log("dashboard at " + url + "  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
