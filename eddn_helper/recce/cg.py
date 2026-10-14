"""The "cg" dashboard kind: a community-goal supply board.

For each tab (one commodity) it lists the large-pad stations within a few laden jumps of the delivery system that
sell the commodity, ranked by how easy they are to haul from. The query is queries.find_commodity; what is added here
is the large-pad filter (station types the database often lacks are looked up on EDSM and cached), the distance to the
arrival star (EDSM, cached), and per-tab stock baselines so the page can show how much stock changed.
"""

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .. import database, queries
from ..models import Market
from . import config as rcfg

NAME = "cg"
LARGE_PAD_TYPES = ["Coriolis", "Orbis", "Ocellus", "Bernal", "Dodec", "AsteroidBase"]
EDSM_BUDGET = 6              # EDSM requests per refresh (one per system); the rest wait for the next refresh
EDSM_RETRY_S = 3600          # after a failed lookup, wait this long before asking again
STATE_KEY = "_target"        # in a baseline file: the system the baselines were measured around

DEFAULT_CONFIG = {
    "kind": "cg",
    "name": NAME,
    "title": "CG supply board",
    "refresh_seconds": 30,
    "max_age_hours": 72,
    "target": {"name": "Delivery station", "system": "current"},
    "ship": {"name": "Ship", "laden_ly": 30.0, "unladen_ly": 40.0, "cargo": 100},
    "station_types": LARGE_PAD_TYPES,
    "min_stock": 1,
    "lookup_ls": True,
    "stale_after_minutes": 60,
    "tabs": [],
}
TOP_KEYS = tuple(DEFAULT_CONFIG)
TAB_KEYS = ("key", "label", "commodity", "cg_price", "max_jumps", "max_ly", "min_stock", "station_types",
            "max_age_hours")
TAB_KEY_RE = rcfg.NAME_RE
_lock = threading.Lock()     # the baseline and EDSM cache files are shared by every cg dashboard


# ---- validation --------------------------------------------------------------------------------------------------

def _strings(errors, path, v, allow_empty=False):
    if not isinstance(v, list) or (not v and not allow_empty) or not all(isinstance(x, str) and x.strip() for x in v):
        errors.append(f"{path}: must be a list of names" + ("" if allow_empty else " with at least one"))


def validate(raw):
    """Fill defaults and check a cg config. Returns the complete config; raises ConfigError listing every problem."""
    if not isinstance(raw, dict):
        raise rcfg.ConfigError("config: must be a JSON object")
    errors = [f"{k}: unknown setting (allowed: {', '.join(TOP_KEYS)})" for k in raw if k not in TOP_KEYS]
    for key in ("target", "ship"):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            errors.append(f"{key}: must be an object")
    if errors:
        raise rcfg.ConfigError(errors)
    cfg = rcfg._deep_merge(DEFAULT_CONFIG, raw)

    name = cfg["name"]
    if not isinstance(name, str) or not rcfg.NAME_RE.match(name):
        errors.append(f"name: must be 1-40 lowercase letters, digits, '-' or '_'; got {name!r}")
    if not isinstance(cfg["title"], str) or not cfg["title"].strip() or len(cfg["title"]) > 120:
        errors.append("title: must be a non-empty string of at most 120 characters")
    rcfg._check_number(errors, "refresh_seconds", cfg["refresh_seconds"], lo=5, hi=86400)
    rcfg._check_number(errors, "stale_after_minutes", cfg["stale_after_minutes"], lo=5, hi=10080)
    rcfg._check_number(errors, "max_age_hours", cfg["max_age_hours"], lo=0.1, hi=24 * 365)
    rcfg._check_number(errors, "min_stock", cfg["min_stock"], lo=0, integer=True)
    rcfg._check_bool(errors, "lookup_ls", cfg["lookup_ls"])
    _strings(errors, "station_types", cfg["station_types"])

    t = cfg["target"]
    for k in t:
        if k not in ("name", "system"):
            errors.append(f"target.{k}: unknown setting (allowed: name, system)")
    for k in ("name", "system"):
        if not isinstance(t.get(k), str) or not t[k].strip():
            errors.append(f"target.{k}: must be a non-empty string" + (
                " (a system name, or \"current\" to follow your journal position)" if k == "system" else ""))
    s = cfg["ship"]
    for k in s:
        if k not in ("name", "laden_ly", "unladen_ly", "cargo"):
            errors.append(f"ship.{k}: unknown setting (allowed: name, laden_ly, unladen_ly, cargo)")
    for k in ("laden_ly", "unladen_ly", "cargo"):
        rcfg._check_number(errors, f"ship.{k}", s.get(k), lo=0.1)

    tabs = cfg["tabs"]
    if not isinstance(tabs, list) or not tabs:
        errors.append("tabs: must be a non-empty list of {key, commodity, ...}")
    else:
        seen = set()
        for i, tab in enumerate(tabs):
            p = f"tabs[{i}]"
            if not isinstance(tab, dict):
                errors.append(f"{p}: must be an object like {{\"key\": \"super\", \"commodity\": \"superconductors\"}}")
                continue
            for k in tab:
                if k not in TAB_KEYS:
                    errors.append(f"{p}.{k}: unknown setting (allowed: {', '.join(TAB_KEYS)})")
            key = tab.get("key")
            if not isinstance(key, str) or not TAB_KEY_RE.match(key):
                errors.append(f"{p}.key: must be 1-40 lowercase letters, digits, '-' or '_'; got {key!r}")
            elif key in seen:
                errors.append(f"{p}.key: {key!r} is used by another tab")
            seen.add(key)
            if not isinstance(tab.get("commodity"), str) or not tab["commodity"].strip():
                errors.append(f"{p}.commodity: must be a commodity name such as \"superconductors\"")
            if "label" in tab and not (isinstance(tab["label"], str) and tab["label"].strip()):
                errors.append(f"{p}.label: must be a non-empty string")
            if tab.get("cg_price") is not None:
                rcfg._check_number(errors, f"{p}.cg_price", tab["cg_price"], lo=0)
            if tab.get("max_jumps") is not None:
                rcfg._check_number(errors, f"{p}.max_jumps", tab["max_jumps"], lo=1, hi=50, integer=True)
            if tab.get("max_ly") is not None:
                rcfg._check_number(errors, f"{p}.max_ly", tab["max_ly"], lo=1, hi=2000)
            if tab.get("min_stock") is not None:
                rcfg._check_number(errors, f"{p}.min_stock", tab["min_stock"], lo=0, integer=True)
            if tab.get("max_age_hours") is not None:
                rcfg._check_number(errors, f"{p}.max_age_hours", tab["max_age_hours"], lo=0.1, hi=24 * 365)
            if tab.get("station_types") is not None:
                _strings(errors, f"{p}.station_types", tab["station_types"])
    if errors:
        raise rcfg.ConfigError(errors)
    return cfg


# ---- state files (baselines, EDSM cache) -------------------------------------------------------------------------

def state_dir():
    return rcfg.root_dir() / "cg"


def baseline_path(name):
    return state_dir() / f"{name}.baseline.json"


def ls_cache_path():
    return state_dir() / "edsm-cache.json"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def apply_target_change(baselines, system):
    """Baselines only mean something for one target. They remember which system they were measured around under
    "_target"; when the target moves they restart from scratch. A file without the marker is just stamped."""
    prev = baselines.get(STATE_KEY)
    if prev is not None and prev.lower() != system.lower():
        baselines = {}
    baselines[STATE_KEY] = system
    return baselines


def reset_baselines(name, tab=None):
    """Restart the "Change" baseline of one tab, or of every tab. The next refresh measures from then."""
    with _lock:
        b = load_json(baseline_path(name), {})
        if tab:
            b.pop(tab, None)
        else:
            b = {k: v for k, v in b.items() if k == STATE_KEY}
        save_json(baseline_path(name), b)


# ---- EDSM: distance to star, station type ------------------------------------------------------------------------

def norm_type(t):
    return "".join((t or "").split()).lower()


def is_large(edsm_type, types):
    """EDSM names ("Coriolis Starport", "Asteroid base") against the configured types ("Coriolis", "AsteroidBase")."""
    n = norm_type(edsm_type)
    return any(norm_type(t) in n for t in types)


def edsm_station(cache, system, station, budget, enabled, need_type=False, fetch=None):
    """Distance-to-star (Ls) and station type are not reliably in the local database, so ask EDSM once per system and
    remember the answers in the cache. Returns the cache entry ({"ls", "type"}) or None while unknown."""
    key = system + "|" + station
    hit = cache.get(key)
    if hit and hit.get("ls") is not None and (not need_type or hit.get("type")):
        return hit
    if not enabled or budget[0] <= 0:
        return hit
    if hit and time.time() - hit.get("tried", 0) < EDSM_RETRY_S:
        return hit
    budget[0] -= 1
    try:
        payload = (fetch or _fetch_edsm)(system)
        for st in payload.get("stations", []) or []:
            d = st.get("distanceToArrival")
            cache[system + "|" + st["name"]] = {"ls": round(d) if d is not None else None, "type": st.get("type")}
    except Exception:       # network trouble is fine, the column just stays blank
        pass
    got = cache.get(key)
    if got is None or (got.get("ls") is None and not got.get("type")):
        cache[key] = {"ls": None, "type": None, "tried": time.time()}
    return cache[key]


def _fetch_edsm(system):
    url = "https://www.edsm.net/api-system-v1/stations?systemName=" + urllib.parse.quote(system)
    req = urllib.request.Request(url, headers={"User-Agent": "elite-helper-cg/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


# ---- the snapshot ------------------------------------------------------------------------------------------------

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pad_sets(session, types):
    """(known, unknown): {(station, system) lowercase} of non-carrier markets whose station type is in `types`, and of
    those whose type is unknown (EDDN market messages carry none, so many markets have none until something reports it)."""
    M = Market
    q = select(M.station, M.system_name).where(M.is_carrier.is_(False), M.station_type.in_(list(types)))
    known = {(a.lower(), b.lower()) for a, b in session.execute(q)}
    q = select(M.station, M.system_name).where(M.is_carrier.is_(False),
                                               (M.station_type.is_(None)) | (M.station_type == ""))
    return known, {(a.lower(), b.lower()) for a, b in session.execute(q)}


def snapshot(core, cfg, now=None, fetch=None):
    from .core import CoreError
    now = now or datetime.now(timezone.utc)
    target, _ = core.resolve_origin({"origin": cfg["target"]["system"]})
    laden = float(cfg["ship"]["laden_ly"])
    with _lock:
        baselines = apply_target_change(load_json(baseline_path(cfg["name"]), {}), target)
        ls_cache = load_json(ls_cache_path(), {})
    budget = [EDSM_BUDGET]
    tabs_out, order, types_cache = {}, [], {}

    with database.session_scope(core.factory()) as session:
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
                types_cache[types] = pad_sets(session, types)
            allowed, unknown_type = types_cache[types]
            min_stock = tab.get("min_stock", cfg["min_stock"])
            max_age = tab.get("max_age_hours", cfg["max_age_hours"])
            cg = tab.get("cg_price")
            base = baselines.setdefault(key, {"baselineAt": iso(now), "base": {}})

            res = queries.find_commodity(
                session, tab["commodity"], mode="buy", near=target, ly=max_ly, min_amount=0,
                max_age_hours=max_age, include_carriers=False, limit=1000000)
            if "error" in res:
                raise CoreError("%s: %s" % (key, res["error"]))

            rows = {}
            for r in res["results"]:
                st, sysname = r["station"], r["system"]
                ident = (st.lower(), sysname.lower())
                info, pad_unknown = None, False
                if ident not in allowed:
                    if ident not in unknown_type:
                        continue
                    info = edsm_station(ls_cache, sysname, st, budget, cfg["lookup_ls"], need_type=True, fetch=fetch)
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
                ls = (info or edsm_station(ls_cache, sysname, st, budget, cfg["lookup_ls"], fetch=fetch) or {}).get("ls")
                rows[st] = {"system": sysname, "ly": r["distance_ly"], "ls": ls, "pad_unknown": pad_unknown,
                            "stock": stock, "price": price, "age": round(r["age_hours"], 1), "base": base["base"][st]}
            tabs_out[key] = {"label": tab.get("label", tab["commodity"]), "commodity": tab["commodity"], "cg": cg,
                             "max_ly": max_ly, "max_jumps": max_jumps, "baselineAt": base["baselineAt"], "rows": rows}

    with _lock:
        save_json(baseline_path(cfg["name"]), baselines)
        merged = load_json(ls_cache_path(), {})
        merged.update(ls_cache)
        save_json(ls_cache_path(), merged)
    return {
        "dashboard": cfg["name"], "generated_at": iso(now), "updated": iso(now), "order": order,
        "db": core.stamps(),
        "config": {"title": cfg["title"], "target": {**cfg["target"], "system": target}, "ship": cfg["ship"],
                   "station_types": cfg["station_types"]},
        "tabs": tabs_out}


LEGACY_DROP = ("port", "helper_dir", "database_url")      # settings of the old standalone script


def import_legacy(legacy_dir, name=NAME, force=False):
    """One-off import of the old standalone board (config.json, baseline.json, ls_cache.json in `legacy_dir`) into a
    cg dashboard. The old files are copied to <state dir>/legacy/ first and are never modified. Returns the stored
    config; raises ConfigError (nothing written) when the old config does not validate or the dashboard exists."""
    legacy = Path(legacy_dir)
    old = load_json(legacy / "config.json", None)
    if not isinstance(old, dict):
        raise rcfg.ConfigError(f"{legacy / 'config.json'}: cannot read the old board config")
    if rcfg.exists(name) and not force:
        raise rcfg.ConfigError(f"dashboard {name!r} already exists (use --force to replace it)")
    cfg = {k: v for k, v in old.items() if k not in LEGACY_DROP}
    cfg.update({"kind": "cg", "name": name})
    stored = validate(cfg)                       # fail before anything is written
    backup = state_dir() / "legacy"
    backup.mkdir(parents=True, exist_ok=True)
    for f in ("config.json", "baseline.json", "ls_cache.json"):
        if (legacy / f).is_file():
            (backup / f).write_bytes((legacy / f).read_bytes())
    baselines = load_json(legacy / "baseline.json", {})
    if isinstance(baselines, dict):
        baselines.setdefault(STATE_KEY, stored["target"]["system"])
        with _lock:
            save_json(baseline_path(name), baselines)
    cache = load_json(legacy / "ls_cache.json", {})
    if isinstance(cache, dict) and cache:
        with _lock:
            merged = load_json(ls_cache_path(), {})
            merged.update(cache)
            save_json(ls_cache_path(), merged)
    return rcfg.save(stored)


class CgKind:
    name = NAME
    page = Path(__file__).with_name("cg.html")
    stamp_key = "newest_market_update"

    def validate(self, raw):
        return validate(raw)

    def snapshot(self, core, cfg):
        return snapshot(core, cfg)

    def labels(self, cfg):
        return {}

    def follows_position(self, cfg):
        return cfg["target"]["system"].strip().lower() == "current"

    def origin(self, cfg):
        return cfg["target"]["system"]

    def origin_patch(self, system):
        return {"target": {"system": system}}


KIND = CgKind()
