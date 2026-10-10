"""Pure functions turning raw EDDN messages into normalized rows."""

import re
from datetime import datetime, timedelta, timezone

JOURNAL_EVENTS = ("FSDJump", "Location", "CarrierJump")
HAPPINESS = {"1": "Elated", "2": "Happy", "3": "Discontented", "4": "Unhappy", "5": "Despondent"}
CARRIER_RE = re.compile(r"^[A-Z0-9]{3}-[A-Z0-9]{3}$")


def clean_token(value):
    """'$economy_HighTech;' -> 'HighTech'; '$government_None;' / None -> None."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"\$.*?_([^_;]+);", value)
    out = m.group(1) if m else value
    return None if out == "None" else out


def norm_ts(ts):
    """Return 'YYYY-MM-DDTHH:MM:SSZ' or None if malformed / implausibly in the future."""
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if dt > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _states(items):
    """[{"State": "Boom", "Trend": 0}, ...] -> [{"state": "Boom", "trend": 0}, ...] (trend None when absent)."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            name, trend = it.get("State"), it.get("Trend")
        else:
            name, trend = it, None
        if name:
            out.append({"state": name, "trend": trend if isinstance(trend, int) else None})
    return out


def state_names(items):
    """Names only, from either the current {"state","trend"} objects or legacy plain strings."""
    return [(it.get("state") if isinstance(it, dict) else it) for it in items or [] if it]


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_message(obj):
    """Dispatch a decoded EDDN envelope. Returns a dict or None if not of interest."""
    if not isinstance(obj, dict):
        return None
    schema = obj.get("$schemaRef", "")
    msg = obj.get("message")
    if not isinstance(msg, dict) or schema.endswith("/test"):
        return None
    if schema.endswith("/commodity/3"):
        parsed = parse_commodity(msg)
    elif schema.endswith("/journal/1"):
        parsed = parse_journal(msg)
    else:
        return None
    if parsed is not None:
        header = obj.get("header") if isinstance(obj.get("header"), dict) else {}
        parsed["uploader"] = str(header.get("uploaderID") or "")[:64]  # obfuscated by the EDDN relay
        parsed["software"] = str(header.get("softwareName") or "")[:40]
    return parsed


def parse_commodity(msg):
    ts = norm_ts(msg.get("timestamp"))
    market_id = _int(msg.get("marketId"))
    station, system = msg.get("stationName"), msg.get("systemName")
    commodities = msg.get("commodities")
    if not (ts and market_id and station and system and isinstance(commodities, list) and commodities):
        return None
    prices = []
    for c in commodities:
        name = c.get("name") if isinstance(c, dict) else None
        if not name:
            continue
        prices.append({
            "commodity": name.lower(),
            "buy_price": _int(c.get("buyPrice")), "sell_price": _int(c.get("sellPrice")),
            "mean_price": _int(c.get("meanPrice")),
            "stock": _int(c.get("stock")), "demand": _int(c.get("demand")),
            "stock_bracket": _int(c.get("stockBracket")), "demand_bracket": _int(c.get("demandBracket")),
        })
    if not prices:
        return None
    stype = msg.get("stationType")
    market = {
        "market_id": market_id, "station": station, "system_name": system, "timestamp": ts,
        "station_type": stype,
        "is_carrier": stype == "FleetCarrier" or bool(CARRIER_RE.match(station)),
        "economies": [e.get("name") for e in msg.get("economies", []) if isinstance(e, dict)],
        "prohibited": msg.get("prohibited") or [],
    }
    return {"kind": "market", "market": market, "prices": prices}


def parse_journal(msg):
    if msg.get("event") not in JOURNAL_EVENTS:
        return None
    ts = norm_ts(msg.get("timestamp"))
    id64, name, pos = _int(msg.get("SystemAddress")), msg.get("StarSystem"), msg.get("StarPos")
    if not (ts and id64 and name and isinstance(pos, list) and len(pos) == 3):
        return None
    controlling = msg.get("SystemFaction")
    controlling = controlling.get("Name") if isinstance(controlling, dict) else None
    system = {
        "id64": id64, "name": name, "timestamp": ts,
        "x": pos[0], "y": pos[1], "z": pos[2],
        "population": _int(msg.get("Population")),
        "allegiance": msg.get("SystemAllegiance") or None,
        "government": clean_token(msg.get("SystemGovernment")),
        "security": clean_token(msg.get("SystemSecurity")),
        "economy": clean_token(msg.get("SystemEconomy")),
        "second_economy": clean_token(msg.get("SystemSecondEconomy")),
        "controlling_faction": controlling,
        "conflicts": None,
    }
    if isinstance(msg.get("Conflicts"), list):
        system["conflicts"] = [
            {"type": c.get("WarType"), "status": c.get("Status"),
             "faction1": (c.get("Faction1") or {}).get("Name"),
             "stake1": (c.get("Faction1") or {}).get("Stake"),
             "won1": (c.get("Faction1") or {}).get("WonDays"),
             "faction2": (c.get("Faction2") or {}).get("Name"),
             "stake2": (c.get("Faction2") or {}).get("Stake"),
             "won2": (c.get("Faction2") or {}).get("WonDays")}
            for c in msg["Conflicts"] if isinstance(c, dict)]
    factions = None
    raw = msg.get("Factions")
    if isinstance(raw, list):
        factions = []
        for f in raw:
            fname, infl = f.get("Name"), f.get("Influence")
            if not fname or not isinstance(infl, (int, float)) or not 0 <= infl <= 1:
                continue
            hap = f.get("Happiness") or ""
            hm = re.search(r"(\d)", hap)
            factions.append({
                "name": fname,
                "government": clean_token(f.get("Government")),
                "allegiance": f.get("Allegiance") or None,
                "influence": float(infl),
                "state": f.get("FactionState") or "None",
                "happiness": HAPPINESS.get(hm.group(1)) if hm else None,
                "active_states": _states(f.get("ActiveStates")),
                "pending_states": _states(f.get("PendingStates")),
                "recovering_states": _states(f.get("RecoveringStates")),
                "is_controlling": fname == controlling,
            })
        if not factions:
            factions = None
    return {"kind": "system", "system": system, "factions": factions}
