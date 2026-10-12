"""On-demand EDSM lookups, used when EDDN has not (yet) supplied a system or market."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import parsers, store

EDSM = "https://www.edsm.net"
USER_AGENT = "elite-helper/0.1 (personal EDDN/EDSM lookup tool)"
MIN_INTERVAL_S = 1.0
_last_call = 0.0


class LiveLookupError(Exception):
    pass


def _get(path, **params):
    global _last_call
    wait = MIN_INTERVAL_S - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    url = f"{EDSM}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise LiveLookupError(f"EDSM request failed: {e}") from e
    finally:
        _last_call = time.time()
    return data


def _edsm_ts(s):
    """'2026-10-10 04:53:20' -> '2026-10-10T04:53:20Z'."""
    return s.replace(" ", "T") + "Z" if isinstance(s, str) and len(s) >= 19 else None


def _epoch_ts(e):
    return datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if e else None


def _player_flags(fac_list):
    """{faction name: bool} for the EDSM faction objects that say whether they are a player faction."""
    return {f["name"]: f["isPlayer"] for f in fac_list or []
            if f.get("name") and isinstance(f.get("isPlayer"), bool)}


def fetch_system(session, name):
    """Fetch system info + factions from EDSM and store them. Returns True if found."""
    info = _get("/api-v1/system", systemName=name, showId=1, showCoordinates=1, showInformation=1)
    if not isinstance(info, dict) or not info.get("id64"):
        return False
    facs = _get("/api-system-v1/factions", systemName=name)
    fac_list = facs.get("factions") if isinstance(facs, dict) else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    coords = info.get("coords") or {}
    i = info.get("information") or {}
    ctrl = (facs.get("controllingFaction") or {}).get("name") if isinstance(facs, dict) else None
    # Use the newest faction update EDSM reports as the data timestamp, so ages stay honest.
    stamps = [_epoch_ts(f.get("lastUpdate")) for f in (fac_list or [])]
    ts = max([t for t in stamps if t] or [now])
    system = {
        "id64": info["id64"], "name": info["name"], "timestamp": ts,
        "x": coords.get("x"), "y": coords.get("y"), "z": coords.get("z"),
        "population": i.get("population"), "allegiance": i.get("allegiance"),
        "government": i.get("government"), "security": i.get("security"),
        "economy": i.get("economy"), "second_economy": i.get("secondEconomy"),
        "controlling_faction": ctrl or i.get("faction"), "conflicts": None,
    }
    factions = None
    if fac_list:
        factions = [{
            "name": f["name"], "government": f.get("government"), "allegiance": f.get("allegiance"),
            "influence": f.get("influence"), "state": f.get("state") or "None",
            "happiness": f.get("happiness"),
            "active_states": parsers._states(
                [{"State": s["state"]} for s in f.get("activeStates", [])]),
            "pending_states": parsers._states(
                [{"State": s["state"], "Trend": s.get("trend")} for s in f.get("pendingStates", [])]),
            "recovering_states": parsers._states(
                [{"State": s["state"], "Trend": s.get("trend")} for s in f.get("recoveringStates", [])]),
            "is_controlling": f["name"] == (ctrl or i.get("faction")),
        } for f in fac_list if f.get("name")]
    store.upsert_system(session, system, factions, source="edsm")
    store.upsert_faction_info(session, _player_flags(fac_list))
    session.commit()
    return True


def _station_economies(s):
    """EDSM repeats the economy when a station has no second one: ['Refinery', 'Refinery'] -> ['Refinery']."""
    return list(dict.fromkeys(e for e in (s.get("economy"), s.get("secondEconomy")) if e))


def _parse_station(s):
    """One EDSM station object -> our dict (keeps the keys the economy view already used)."""
    name, typ = s.get("name"), s.get("type")
    body = s.get("body") or {}
    return {
        "station": name, "station_type": typ, "market_id": s.get("marketId"),
        "is_carrier": typ == "Fleet Carrier" or bool(parsers.CARRIER_RE.match(name or "")),
        "body": body.get("name"), "distance_to_arrival_ls": s.get("distanceToArrival"),
        "economies": _station_economies(s), "economy": s.get("economy"), "second_economy": s.get("secondEconomy"),
        "allegiance": s.get("allegiance"), "government": s.get("government"),
        "controlling_faction": (s.get("controllingFaction") or {}).get("name"),
        "has_market": bool(s.get("haveMarket")), "has_shipyard": bool(s.get("haveShipyard")),
        "has_outfitting": bool(s.get("haveOutfitting")), "services": list(s.get("otherServices") or []),
        "info_updated_at": _edsm_ts((s.get("updateTime") or {}).get("information")),
    }


def fetch_station_list(system, include_carriers=False):
    """
    EDSM's stations of a system: {'id64', 'system', 'stations': [...]} or None when EDSM does not know the system.
    Includes planetary outposts, Odyssey settlements and every other station type; fleet carriers only on request.
    """
    data = _get("/api-system-v1/stations", systemName=system)
    if not isinstance(data, dict) or not data.get("name"):
        return None
    stations = [_parse_station(s) for s in data.get("stations") or [] if s.get("name")]
    if not include_carriers:
        stations = [s for s in stations if not s["is_carrier"]]
    return {"id64": data.get("id64"), "system": data["name"], "stations": stations}


def fetch_stations(system, include_carriers=False):
    """The station dicts only (not stored); see fetch_station_list."""
    data = fetch_station_list(system, include_carriers)
    return data["stations"] if data else []


def refresh_stations(session, system, system_id64=None):
    """
    Fetch a system's stations from EDSM and replace what is stored for it (also marks the system as synced).
    Returns the number of stations stored, or None if EDSM does not know the system or the system is not in our database.
    """
    from .models import System
    data = fetch_station_list(system)
    if data is None:
        return None
    id64 = system_id64 or data["id64"]
    if id64 is None or session.get(System, id64) is None:
        return None
    n = store.replace_stations(session, id64, data["stations"])
    try:  # also learn which of the system's factions are player factions; the stations are stored either way
        facs = _get("/api-system-v1/factions", systemName=data["system"])
        store.upsert_faction_info(session, _player_flags(facs.get("factions") if isinstance(facs, dict) else None))
    except LiveLookupError:
        pass
    session.commit()
    return n


def unchecked_factions(session, controlling_only=False, near=None, ly=None):
    """
    Factions with no player status yet, as {lower(name): {"name": str, "systems": {system_id64: system name}}}: the
    systems each is present in, so one EDSM request per system can settle every unchecked faction there.
    controlling_only: only factions that control at least one system; near + ly: only systems within ly of `near`
    (a system name). Raises ValueError for an unknown `near`.
    """
    from sqlalchemy import func, select
    from .models import Faction, FactionInfo, System
    from . import queries
    origin = None
    if near:
        origin, err = queries._resolve_origin(session, near)
        if err:
            raise ValueError(err["error"])
    q = (select(Faction.name, Faction.is_controlling, System.id64, System.name, System.x, System.y, System.z)
         .join(System, System.id64 == Faction.system_id64)
         .outerjoin(FactionInfo, func.lower(FactionInfo.name) == func.lower(Faction.name))
         .where(FactionInfo.name.is_(None)))
    found = {}
    for name, controls, sid, sname, x, y, z in session.execute(q):
        if origin is not None:
            d = queries.distance(origin, {"x": x, "y": y, "z": z})
            if d is None or (ly is not None and d > ly):
                continue
        e = found.setdefault(name.lower(), {"name": name, "systems": {}, "controls": False})
        e["systems"][sid] = sname
        e["controls"] = e["controls"] or bool(controls)
    return {k: {"name": v["name"], "systems": v["systems"]} for k, v in found.items()
            if v["controls"] or not controlling_only}


def refresh_player_flags(session, limit=50, controlling_only=False, near=None, ly=None, dry_run=False, log=None):
    """
    Fetch player status from EDSM for every faction that has none yet, a few systems at a time: each request returns
    all the factions of one system, and the system covering the most unchecked factions is asked next.
    At most `limit` requests (1 per second). Returns {"unchecked", "requests", "stored", "remaining", "error"}.
    A faction EDSM does not report a status for stays unchecked (and is not asked about again in this run).
    """
    todo = unchecked_factions(session, controlling_only, near, ly)
    out = {"unchecked": len(todo), "requests": 0, "stored": 0, "remaining": len(todo), "error": None}
    names_of, tried = {}, set()
    for key, f in todo.items():
        for sid, sname in f["systems"].items():
            names_of.setdefault((sid, sname), set()).add(key)
    if dry_run:
        out["requests_needed_at_most"] = len(names_of)
        return out
    pending = set(todo)
    while out["requests"] < limit and pending:
        best = max((k for k in names_of if k not in tried), key=lambda k: len(names_of[k] & pending), default=None)
        if best is None or not names_of[best] & pending:
            break
        tried.add(best)
        out["requests"] += 1
        try:
            facs = _get("/api-system-v1/factions", systemName=best[1])
            flags = _player_flags(facs.get("factions") if isinstance(facs, dict) else None)
            out["stored"] += store.upsert_faction_info(session, flags)
            session.commit()
        except LiveLookupError as e:
            out["error"] = str(e)
            break
        except Exception as e:      # a read-only connection: say so instead of a stack trace
            from sqlalchemy.exc import DBAPIError
            if not isinstance(e, DBAPIError):
                raise
            session.rollback()
            out["error"] = "database connection is read-only (or write failed); use the writer URL with --db"
            break
        pending -= {n.lower() for n in flags}
        if log:
            log(f"{best[1]}: {len(flags)} factions checked, {len(pending)} unchecked left")
    out["remaining"] = len(pending)
    return out


def fetch_market(session, station, system=None):
    """Fetch a station's market from EDSM (needs the system name). Returns True if stored."""
    if not system:
        raise LiveLookupError("a system name is required for a live market lookup")
    data = _get("/api-system-v1/stations", systemName=system)
    stations = data.get("stations") if isinstance(data, dict) else None
    match = next((s for s in stations or [] if s.get("name", "").lower() == station.lower()
                  and s.get("marketId")), None)
    if match is None:
        return False
    mk = _get("/api-system-v1/stations/market", marketId=match["marketId"])
    commodities = mk.get("commodities") if isinstance(mk, dict) else None
    ts = _edsm_ts((match.get("updateTime") or {}).get("market"))
    if not commodities or not ts:
        return False
    market = {
        "market_id": match["marketId"], "station": match["name"], "system_name": data.get("name", system),
        "timestamp": ts, "station_type": match.get("type"),
        "is_carrier": match.get("type") == "Fleet Carrier" or bool(parsers.CARRIER_RE.match(match["name"])),
        "economies": _station_economies(match),
        "prohibited": [],
    }
    prices = [{
        "commodity": c["id"].lower(), "buy_price": c.get("buyPrice"), "sell_price": c.get("sellPrice"),
        "mean_price": None, "stock": c.get("stock"), "demand": c.get("demand"),
        "stock_bracket": c.get("stockBracket"), "demand_bracket": None,
    } for c in commodities if c.get("id")]
    store.upsert_market(session, market, prices, source="edsm")
    session.commit()
    return True
