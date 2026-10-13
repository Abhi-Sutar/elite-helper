"""The shared Recce core: turns a dashboard config into plain JSON using the existing queries.

The HTTP server, the CLI and the MCP tools all call this; the search itself is queries.find_systems and the history
reading is queries.faction_history, so there is no query code of our own here, only row shaping.
"""

import time
from datetime import datetime, timedelta, timezone

from .. import database, live, queries
from ..journal import context as journal_context
from . import config as rcfg

MAX_ROWS = 200
HISTORY_DAYS = 14
HISTORY_TTL_S = 600
LIVE_COOLDOWN_S = 600       # after a live fetch that left factions unknown, wait before asking EDSM again
STALE_ROW_HOURS = 24

KIND_RANK = {"active": 0, "pending": 1, "recovering": 2, "other": 3}


class CoreError(Exception):
    """A snapshot could not be built (unknown origin, database trouble...). The message is shown to the user."""


# ---- time helpers ------------------------------------------------------------------------------------------------

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_ts(text):
    if not text:
        return None
    dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_hours(seconds):
    """Same wording as the page: '25 min', '3.4 h', '31 h'."""
    seconds = max(0.0, seconds)
    h = seconds / 3600
    if h < 1:
        return f"{max(1, round(seconds / 60))} min"
    return f"{h:.1f} h" if h < 10 else f"{round(h)} h"


# ---- the "pending for" run logic ---------------------------------------------------------------------------------

def _has_state(point, kind, state_key):
    """Does one faction_history point carry the state in the slot this row is about?"""
    if not point["present"]:
        return False
    if kind == "active":
        return (any(queries.norm_key(s) == state_key for s in point["active_states"])
                or queries.norm_key(point["state"]) == state_key)
    slot = point["pending_states"] if kind == "pending" else point["recovering_states"]
    return any(queries.norm_key(s["state"]) == state_key for s in slot)


def state_run(points, kind, state_key):
    """
    The current unbroken run of the state in one faction's history points (any order), or None if the newest point
    does not have it. Returns {"first_seen": datetime, "last_absent": datetime|None}: the run began after
    `last_absent` (the newest reading WITHOUT the state, None when history starts inside the run) and no later than
    `first_seen` (the oldest reading of the run).
    """
    pts = sorted(points, key=lambda p: p["ts"])
    if not pts or not _has_state(pts[-1], kind, state_key):
        return None
    i = len(pts) - 1
    while i > 0 and _has_state(pts[i - 1], kind, state_key):
        i -= 1
    return {"first_seen": parse_ts(pts[i]["ts"]), "last_absent": parse_ts(pts[i - 1]["ts"]) if i > 0 else None}


def run_text(run, kind, now):
    """'at least 5.2 h' (history starts inside the run), '5.2 h to 9 h' (previous reading known), 'unknown'."""
    if kind not in ("pending", "active", "recovering"):
        return "-"
    prefix = {"active": "active for ", "recovering": "recovering for "}.get(kind, "")
    if run is None:
        return "unknown"
    young = (now - run["first_seen"]).total_seconds()
    if run["last_absent"] is None:
        return f"{prefix}at least {fmt_hours(young)}"
    return f"{prefix}{fmt_hours(young)} to {fmt_hours((now - run['last_absent']).total_seconds())}"


def is_highlighted(row, rules, now):
    """True when any highlight rule matches the row (the page re-checks this every second as time passes)."""
    run = row.get("run")
    if run is None:
        return False
    started = parse_ts(run["first_seen"])
    for r in rules:
        hours = (now - started).total_seconds() / 3600
        if r["when"] == "active_for_under_hours" and row["kind"] == "active" and hours <= r["hours"]:
            return True
        if r["when"] == "pending_for_over_hours" and row["kind"] == "pending" and hours >= r["hours"]:
            return True
    return False


# ---- search + row shaping ----------------------------------------------------------------------------------------

def _as_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def search_kwargs(cfg, origin):
    f = cfg["filters"]
    return dict(
        economies=_as_list(f["economy"]) or None, economy_scope="system", include_second=False,
        faction_state=f["faction_state"], state_in=f["state_in"], controlling_only=f["controlling_only"],
        near=origin, ly=cfg["radius_ly"], min_population=f["min_population"],
        player_faction=True if f["player_faction_only"] else None, sort="distance", limit=MAX_ROWS)


def classify(faction, state_key):
    """active > pending > recovering > other, from where find_systems found the state."""
    if not state_key:
        return "other"
    where = {h["in"] for h in faction.get("found_in") or []}
    if where & {"active", "state"}:
        return "active"
    if "pending" in where:
        return "pending"
    if "recovering" in where:
        return "recovering"
    return "other"


def _sort_key(cfg):
    by, desc = cfg["sort"]["by"], cfg["sort"]["order"] == "desc"

    def value(r):
        if by == "state":
            return KIND_RANK.get(r["kind"], 9)
        if by == "pending_for":
            return parse_ts(r["run"]["first_seen"]).timestamp() if r["run"] else float("inf")
        if by == "last_update":
            return parse_ts(r["last_update_at"]).timestamp() if r["last_update_at"] else float("inf")
        if by == "distance":
            return r["distance_ly"] if r["distance_ly"] is not None else float("inf")
        if by == "population":
            return r["population"] or 0
        if by == "influence":
            return r["influence_pct"] if r["influence_pct"] is not None else -1
        return str(r[by]).lower()
    return value, desc


class Core:
    """Runs the dashboard queries. `factory()` returns the reader session factory; `writer()` an optional writer
    session (EDSM cache writes only); `where()` the journal position."""

    def __init__(self, factory, writer=None, where=None):
        self.factory = factory
        self.writer = (lambda: live.writer_session(rcfg.ingest_url())) if writer is None else writer
        self.where = journal_context.where_am_i if where is None else where
        self._history = {}
        self._last_current = None
        self._live_cooldown_until = 0.0

    # -- cheap change detection
    def stamps(self):
        with database.session_scope(self.factory()) as s:
            newest_m, newest_f = queries.newest_stamps(s)
        return {"newest_market_update": iso(newest_m), "newest_faction_update": iso(newest_f)}

    def resolve_origin(self, cfg):
        """(system name, source). 'current' reads the journal; if that fails the last known position is reused."""
        origin = cfg["origin"].strip()
        if origin.lower() != "current":
            return origin, "config"
        data = self.where()
        if isinstance(data, dict) and "error" not in data and data.get("system"):
            self._last_current = data["system"]
            return data["system"], "journal"
        if self._last_current:
            return self._last_current, "journal (last known)"
        reason = data.get("error") if isinstance(data, dict) else None
        raise CoreError("cannot work out your current position: " + (reason or "the journal names no system")
                        + ". Set a system name as origin to avoid this.")

    def current_system(self):
        """The journal position (None when unreadable); remembered as the last known one."""
        system, _ = rcfg.current_system(self.where)
        if system:
            self._last_current = system
        return system

    def recentre(self, name):
        """Anchor a dashboard on the commander's current system. The config watcher pushes the change to open pages.
        Returns {"name", "origin", "previous"}; raises CoreError (nothing changed) when the position is unknown."""
        system, reason = rcfg.current_system(self.where)
        if system is None:
            raise CoreError("cannot recentre: your current position is unknown (" + reason + ")")
        self._last_current = system
        previous = rcfg.load(name)["origin"]
        if previous != system:
            rcfg.update(name, {"origin": system})
        return {"name": name, "origin": system, "previous": previous}

    # -- the full snapshot
    def snapshot(self, cfg, now=None):
        now = now or datetime.now(timezone.utc)
        origin, origin_source = self.resolve_origin(cfg)
        kw = search_kwargs(cfg, origin)
        state_key = queries.norm_key(cfg["filters"]["faction_state"]) if cfg["filters"]["faction_state"] else None
        with database.session_scope(self.factory()) as s:
            res = queries.find_systems(s, **kw)
            if "error" in res:
                raise CoreError(res["error"])
            res, live_info = self._maybe_live(cfg, kw, res, now)
            rows = self._rows(s, cfg, res, state_key, now)
        unknown = res.get("faction_players_unknown", 0) if cfg["filters"]["player_faction_only"] else 0
        live_info["remaining_unknown"] = unknown
        rows = self._order(cfg, rows)
        return {
            "dashboard": cfg["name"], "generated_at": iso(now), "origin": {"name": origin, "source": origin_source},
            "db": self.stamps(), "count": len(rows), "truncated": res.get("count", 0) > res.get("shown", 0),
            "hidden_unknown_player": unknown, "live_fetch": live_info, "rows": rows}

    def _maybe_live(self, cfg, kw, res, now):
        """The limited EDSM fetch: only the player status of factions in this search result that lack it, at most
        live_fetch.max_requests (never more than 10) requests per refresh, through the writer connection."""
        lf = cfg["live_fetch"]
        info = {"enabled": bool(lf["enabled"]), "requests": 0, "max_requests": lf["max_requests"], "note": None}
        unknown = res.get("faction_players_unknown", 0) if cfg["filters"]["player_faction_only"] else 0
        if not unknown:
            return res, info
        if not lf["enabled"] or lf["max_requests"] <= 0:
            info["note"] = "live fetch is off"
            return res, info
        if time.monotonic() < self._live_cooldown_until:
            info["note"] = "live fetch is waiting before asking EDSM again"
            return res, info
        w = self.writer()
        if w is None:
            info["note"] = ("live fetch is off: no writer connection (set ELITE_HELPER_INGEST_DATABASE_URL, "
                            "or ingest_database_url in " + str(rcfg.settings_path()) + ")")
            return res, info
        limit = min(int(lf["max_requests"]), rcfg.MAX_LIVE_REQUESTS)
        try:
            with w:
                try:
                    out = live.refresh_missing(w, lambda: queries.find_systems(w, **kw), limit, 168, True,
                                               need_stations=False)
                finally:
                    w.rollback()
        except Exception as e:                  # the fetch is a bonus: never lose the dashboard over it
            info["note"] = f"live fetch failed: {type(e).__name__}: {e}"
            return res, info
        done = out.get("refreshed") or []
        info["requests"] = len(done)
        errors = [d["error"] for d in done if d.get("error")]
        if errors:
            info["note"] = "EDSM: " + errors[0]
        if out.get("refresh_pending"):
            self._live_cooldown_until = time.monotonic() + LIVE_COOLDOWN_S
        return (out if "error" not in out else res), info

    def _run_for(self, session, system, faction, kind, state_key, sig, now):
        """The state run for one row, cached per (system, faction) until the faction row changes (or 10 minutes)."""
        key = (system, faction)
        hit = self._history.get(key)
        t = time.monotonic()
        if hit and hit["sig"] == sig and t - hit["at"] < HISTORY_TTL_S:
            return hit["run"]
        hist = queries.faction_history(session, system, HISTORY_DAYS, faction)
        run = state_run(hist.get("history", []), kind, state_key) if hist.get("found") else None
        self._history[key] = {"sig": sig, "at": t, "run": run}
        return run

    def _rows(self, session, cfg, res, state_key, now):
        f = cfg["filters"]
        kinds = f["kinds"] if f["state_in"] == "any" else None
        rows = []
        for r in res["results"]:
            for fac in r["matching_factions"]:
                if f["player_faction_only"] and fac.get("is_player") is not True:
                    continue
                kind = classify(fac, state_key)
                if kinds is not None and state_key and kind not in kinds:
                    continue
                age = [a for a in (r.get("factions_age_hours"), r.get("info_age_hours")) if a is not None]
                last = now - timedelta(hours=min(age)) if age else None
                run = None
                if kind in ("pending", "active", "recovering"):
                    sig = (fac.get("state"), kind, fac.get("influence_pct"), bool(r.get("in_conflict")),
                           tuple(sorted((h["in"], h.get("trend")) for h in fac.get("found_in") or [])))
                    run = self._run_for(session, r["system"], fac["faction"], kind, state_key, sig, now)
                row = {
                    "key": f"{r['system']}|{fac['faction']}", "system": r["system"], "faction": fac["faction"],
                    "influence_pct": fac.get("influence_pct"), "main_state": fac.get("state"), "is_player": fac.get("is_player"),
                    "economy": r["economy"], "second_economy": r.get("second_economy"),
                    "population": r.get("population"), "distance_ly": r.get("distance_ly"),
                    "kind": kind, "war": bool(r.get("in_conflict")), "conflict_types": r.get("conflict_types") or [],
                    "run": None if run is None else {"first_seen": iso(run["first_seen"]),
                                                     "last_absent": iso(run["last_absent"])},
                    "pending_text": run_text(run, kind, now),
                    "last_update_at": iso(last),
                    "stale": last is None or (now - last) > timedelta(hours=STALE_ROW_HOURS)}
                row["highlight"] = is_highlighted(row, cfg["highlight_rules"], now)
                rows.append(row)
        return rows

    def _order(self, cfg, rows):
        value, desc = _sort_key(cfg)
        # tie-breakers first (stable sorts): system name, then the longest-running state, then the chosen key
        rows.sort(key=lambda r: r["system"].lower())
        rows.sort(key=lambda r: parse_ts(r["run"]["first_seen"]).timestamp() if r["run"] else float("inf"))
        rows.sort(key=value, reverse=desc)
        return rows


def column_labels(cfg):
    state = cfg["filters"].get("faction_state") or "State"
    return {
        "system": "System", "faction": "Player controlling faction" if cfg["filters"]["player_faction_only"]
        and cfg["filters"]["controlling_only"] else "Faction",
        "economy": "Economy", "pending_for": f"{state} pending for", "state": "Current state",
        "last_update": "Time since last update", "distance": "Distance (ly)", "population": "Population",
        "influence": "Influence"}
