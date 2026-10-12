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
