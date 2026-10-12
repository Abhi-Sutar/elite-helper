"""
Current-state views (the ED Discovery panels): each rebuilds one piece of state by folding the stored journal events
from the most recent full snapshot the game wrote (Cargo, Materials, Loadout, StoredShips, Missions, ...) forward
through the events that change it. Same conventions as queries.py: session first, plain dicts, freshness gate.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import JournalEvent
from . import files as jfiles
from .queries import event_rows, fresh_query, no_commander, resolve_commander


def _latest(session, fid, names, limit=1, where=None):
    """Newest rows (newest first) of the given event names; `where` filters on the data dict in Python."""
    out = []
    for r in event_rows(session, fid, names, ascending=False, limit=200 if where else limit):
        if where is None or where(r.data or {}):
            out.append(r)
            if len(out) >= limit:
                break
    return out


def _after(session, fid, snap, names):
    """Rows of `names` strictly after the snapshot row, in order."""
    rows = event_rows(session, fid, names, start=snap.ts)
    seen = False
    out = []
    for r in rows:
        if not seen:
            seen = (r.file_id, r.line_no) == (snap.file_id, snap.line_no)
            continue
        out.append(r)
    return out if seen else [r for r in rows if r.ts > snap.ts]


def _key(name):
    """'$platinum_name;' / 'Gold' / 'gold' -> 'platinum' / 'gold' / 'gold'."""
    n = str(name or "").strip().lower()
    if n.startswith("$"):
        n = n[1:]
    for suffix in (";", "_name"):
        n = n.removesuffix(suffix)
    return n


# ---- cargo ---------------------------------------------------------------------------------------------------------

@fresh_query
def cargo(session, commander=None):
    """What the ship is carrying: the last Cargo snapshot moved forward through buys, sales, ejections, mining, transfers."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    snaps = _latest(session, fid, ["Cargo"], where=lambda d: d.get("Vessel", "Ship") == "Ship" and "Inventory" in d)
    if not snaps:
        return {"found": False, "commander": name, "error": "no Cargo snapshot with an inventory in the journals"}
    snap = snaps[0]
    items = {_key(i.get("Name")): {"name": _key(i.get("Name")), "localised": i.get("Name_Localised"), "count": i.get("Count", 0)}
             for i in snap.data.get("Inventory", [])}

    def add(key, count, localised=None):
        it = items.setdefault(key, {"name": key, "localised": localised, "count": 0})
        it["count"] += count
        it["localised"] = it["localised"] or localised

    as_of = snap.ts
    for r in _after(session, fid, snap, ["MarketBuy", "MarketSell", "EjectCargo", "CollectCargo", "MiningRefined",
                                         "CargoTransfer", "MissionCompleted", "Died"]):
        d, ev = r.data or {}, r.event
        as_of = r.ts
        if ev == "MarketBuy":
            add(_key(d.get("Type")), d.get("Count", 0), d.get("Type_Localised"))
        elif ev == "MarketSell":
            add(_key(d.get("Type")), -d.get("Count", 0))
        elif ev == "EjectCargo":
            add(_key(d.get("Type")), -d.get("Count", 0))
        elif ev == "CollectCargo":
            add(_key(d.get("Type")), 1, d.get("Type_Localised"))
        elif ev == "MiningRefined":
            add(_key(d.get("Type")), 1, d.get("Type_Localised"))
        elif ev == "CargoTransfer":
            for t in d.get("Transfers", []):
                add(_key(t.get("Type")), t.get("Count", 0) * (1 if t.get("Direction") == "toship" else -1),
                    t.get("Type_Localised"))
        elif ev == "MissionCompleted":
            for c in d.get("CommodityReward", []):
                add(_key(c.get("Name")), c.get("Count", 0), c.get("Name_Localised"))
        elif ev == "Died":
            items.clear()
    rows = sorted((i for i in items.values() if i["count"] > 0), key=lambda i: -i["count"])
    cap = _latest(session, fid, ["Loadout"])
    return {"found": True, "commander": name, "as_of": jfiles.iso(as_of), "snapshot_at": jfiles.iso(snap.ts),
            "total": sum(i["count"] for i in rows), "capacity": (cap[0].data or {}).get("CargoCapacity") if cap else None,
            "items": rows,
            "note": "rebuilt from the last Cargo snapshot plus later trades; cargo moved by other means may be missing"}


# ---- materials -----------------------------------------------------------------------------------------------------

@fresh_query
def materials(session, commander=None, category=None):
    """Raw / manufactured / encoded materials: the last Materials snapshot plus collection, discards, trades and crafting."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    snaps = _latest(session, fid, ["Materials"])
    if not snaps:
        return {"found": False, "commander": name, "error": "no Materials snapshot in the journals"}
    snap = snaps[0]
    cats = {"Raw": {}, "Manufactured": {}, "Encoded": {}}
    where = {}
    for cat in cats:
        for m in snap.data.get(cat, []):
            key = _key(m.get("Name"))
            cats[cat][key] = {"name": key, "localised": m.get("Name_Localised"), "count": m.get("Count", 0)}
            where[key] = cat

    def add(cat, key, count, localised=None):
        cat = (cat or where.get(key) or "Raw").capitalize()
        if cat not in cats:
            return
        it = cats[cat].setdefault(key, {"name": key, "localised": localised, "count": 0})
        it["count"] += count
        it["localised"] = it["localised"] or localised
        where[key] = cat

    as_of = snap.ts
    for r in _after(session, fid, snap, ["MaterialCollected", "MaterialDiscarded", "MaterialTrade", "EngineerCraft",
                                         "Synthesis", "TechnologyBroker", "EngineerContribution"]):
        d, ev = r.data or {}, r.event
        as_of = r.ts
        if ev == "MaterialCollected":
            add(d.get("Category"), _key(d.get("Name")), d.get("Count", 0))
        elif ev == "MaterialDiscarded":
            add(d.get("Category"), _key(d.get("Name")), -d.get("Count", 0))
        elif ev == "MaterialTrade":
            p, g = d.get("Paid") or {}, d.get("Received") or {}
            add(p.get("Category"), _key(p.get("Material")), -p.get("Quantity", 0), p.get("Material_Localised"))
            add(g.get("Category"), _key(g.get("Material")), g.get("Quantity", 0), g.get("Material_Localised"))
        elif ev in ("EngineerCraft", "Synthesis"):
            for i in d.get("Ingredients", d.get("Materials", [])):
                add(None, _key(i.get("Name")), -i.get("Count", 0), i.get("Name_Localised"))
        elif ev == "TechnologyBroker":
            for i in d.get("Materials", []):
                add(i.get("Category"), _key(i.get("Name")), -i.get("Count", 0))
        elif ev == "EngineerContribution" and d.get("Type") == "Material":
            add(d.get("Category"), _key(d.get("Material")), -d.get("Quantity", 0))
    wanted = category.capitalize() if category else None
    out = {c: sorted((m for m in v.values() if m["count"] > 0), key=lambda m: (-m["count"], m["name"]))
           for c, v in cats.items() if not wanted or c == wanted}
    return {"found": True, "commander": name, "as_of": jfiles.iso(as_of), "snapshot_at": jfiles.iso(snap.ts),
            "totals": {c: sum(m["count"] for m in v) for c, v in out.items()}, "materials": out}


# ---- ships ---------------------------------------------------------------------------------------------------------

@fresh_query
def ships(session, commander=None, modules=True):
    """The ship you are in (last Loadout) and the fleet (last StoredShips snapshot, here and elsewhere)."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    out = {"found": True, "commander": name, "current": None, "stored": None}
    lo = _latest(session, fid, ["Loadout"])
    if lo:
        d = lo[0].data
        ship = {"type": d.get("Ship"), "id": d.get("ShipID"), "name": d.get("ShipName"), "ident": d.get("ShipIdent"),
                "hull_value": d.get("HullValue"), "modules_value": d.get("ModulesValue"), "rebuy": d.get("Rebuy"),
                "hull_health": d.get("HullHealth"), "unladen_mass": d.get("UnladenMass"),
                "cargo_capacity": d.get("CargoCapacity"), "max_jump_range": d.get("MaxJumpRange"),
                "fuel_capacity": d.get("FuelCapacity"), "as_of": jfiles.iso(lo[0].ts)}
        if modules:
            ship["modules"] = [{"slot": m.get("Slot"), "item": m.get("Item"), "on": m.get("On"), "priority": m.get("Priority"),
                                "health": m.get("Health"), "value": m.get("Value"),
                                "engineering": ({"blueprint": m["Engineering"].get("BlueprintName"),
                                                 "level": m["Engineering"].get("Level"),
                                                 "quality": m["Engineering"].get("Quality"),
                                                 "experimental": m["Engineering"].get("ExperimentalEffect_Localised")}
                                                if m.get("Engineering") else None)}
                               for m in d.get("Modules", [])]
        out["current"] = ship
    st = _latest(session, fid, ["StoredShips"])
    if st:
        d = st[0].data

        def row(s, here):
            return {"id": s.get("ShipID"), "type": s.get("ShipType_Localised") or s.get("ShipType"), "name": s.get("Name"),
                    "value": s.get("Value"), "hot": s.get("Hot"), "system": s.get("StarSystem") or d.get("StarSystem"),
                    "station": d.get("StationName") if here else None,
                    "transfer_price": s.get("TransferPrice"), "transfer_seconds": s.get("TransferTime")}
        out["stored"] = {"as_of": jfiles.iso(st[0].ts), "station": d.get("StationName"), "system": d.get("StarSystem"),
                         "here": [row(s, True) for s in d.get("ShipsHere", [])],
                         "remote": [row(s, False) for s in d.get("ShipsRemote", [])]}
    if out["current"] is None and out["stored"] is None:
        return {"found": False, "commander": name, "error": "no Loadout or StoredShips events in the journals"}
    return out


# ---- missions ------------------------------------------------------------------------------------------------------

@fresh_query
def missions(session, commander=None, recent_days=14):
    """Active missions (expired ones separated) and recently completed / failed / abandoned ones."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    now = datetime.now(timezone.utc)
    known, active, finished = {}, set(), []
    for r in event_rows(session, fid, ["MissionAccepted", "MissionCompleted", "MissionFailed", "MissionAbandoned",
                                       "MissionRedirected", "Missions"]):
        d, ev = r.data or {}, r.event
        mid = d.get("MissionID")
        if ev == "Missions":                       # the game's own list of what is active at login
            listed = {m.get("MissionID"): m for m in d.get("Active", [])}
            active &= set(listed)
            for i, m in listed.items():
                active.add(i)
                rec = known.setdefault(i, {"id": i})
                rec.setdefault("name", m.get("Name"))
                if isinstance(m.get("Expires"), (int, float)) and not rec.get("expires"):
                    rec["expires"] = jfiles.iso(r.ts + timedelta(seconds=m["Expires"]))   # seconds left at login
        elif ev == "MissionAccepted":
            known[mid] = {"id": mid, "name": d.get("LocalisedName") or d.get("Name"), "faction": d.get("Faction"),
                          "accepted": jfiles.iso(r.ts), "expires": d.get("Expiry"), "reward": d.get("Reward"),
                          "kind": d.get("Name"), "destination_system": d.get("DestinationSystem"),
                          "destination_station": d.get("DestinationStation"), "commodity": d.get("Commodity_Localised"),
                          "count": d.get("Count"), "target": d.get("Target"), "kill_count": d.get("KillCount"),
                          "influence": d.get("Influence"), "reputation": d.get("Reputation"), "wing": d.get("Wing")}
            active.add(mid)
        elif ev in ("MissionCompleted", "MissionFailed", "MissionAbandoned"):
            rec = known.get(mid, {"id": mid, "name": d.get("LocalisedName") or d.get("Name")})
            active.discard(mid)
            finished.append({"id": mid, "name": rec.get("name") or d.get("LocalisedName"), "faction": d.get("Faction") or rec.get("faction"),
                             "result": {"MissionCompleted": "completed", "MissionFailed": "failed",
                                        "MissionAbandoned": "abandoned"}[ev],
                             "ts": jfiles.iso(r.ts), "reward": d.get("Reward"), "fine": d.get("Fine")})
        elif ev == "MissionRedirected" and mid in known:
            known[mid]["destination_system"] = d.get("NewDestinationSystem")
            known[mid]["destination_station"] = d.get("NewDestinationStation")
    live, expired = [], []
    for mid in active:
        rec = known.get(mid, {"id": mid})
        exp = rec.get("expires")
        try:
            gone = bool(exp) and jfiles.parse_timestamp(exp) < now
        except ValueError:
            gone = False
        (expired if gone else live).append(rec)
    live.sort(key=lambda m: m.get("expires") or "9999")
    cutoff = jfiles.iso(now - timedelta(days=recent_days))
    recent = [m for m in reversed(finished) if m["ts"] >= cutoff]
    return {"found": True, "commander": name, "active": live, "active_count": len(live), "expired_unresolved": expired,
            "recent": recent, "recent_days": recent_days,
            "recent_reward_total": sum(m["reward"] or 0 for m in recent if m["result"] == "completed")}


# ---- ranks, reputation, statistics, carrier --------------------------------------------------------------------

RANK_NAMES = {
    "Combat": ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert", "Master", "Dangerous", "Deadly", "Elite",
               "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "Trade": ["Penniless", "Mostly Penniless", "Peddler", "Dealer", "Merchant", "Broker", "Entrepreneur", "Tycoon", "Elite",
              "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "Explore": ["Aimless", "Mostly Aimless", "Scout", "Surveyor", "Trailblazer", "Pathfinder", "Ranger", "Pioneer", "Elite",
                "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "Soldier": ["Defenceless", "Mostly Defenceless", "Rookie", "Soldier", "Gunslinger", "Warrior", "Gladiator", "Deadeye",
                "Elite", "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "Exobiologist": ["Directionless", "Mostly Directionless", "Compiler", "Collector", "Cataloguer", "Taxonomist",
                     "Ecologist", "Geneticist", "Elite", "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "CQC": ["Helpless", "Mostly Helpless", "Amateur", "Semi Professional", "Professional", "Champion", "Hero", "Legend",
            "Elite", "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"],
    "Federation": ["None", "Recruit", "Cadet", "Midshipman", "Petty Officer", "Chief Petty Officer", "Warrant Officer",
                   "Ensign", "Lieutenant", "Lieutenant Commander", "Post Commander", "Post Captain", "Rear Admiral",
                   "Vice Admiral", "Admiral"],
    "Empire": ["None", "Outsider", "Serf", "Master", "Squire", "Knight", "Lord", "Baron", "Viscount", "Count", "Earl",
               "Marquis", "Duke", "Prince", "King"],
}


@fresh_query
def reputation(session, commander=None):
    """Ranks (with progress to the next) and superpower reputation, from the latest Rank/Progress/Reputation events."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    rank = _latest(session, fid, ["Rank"])
    prog = _latest(session, fid, ["Progress"])
    rep = _latest(session, fid, ["Reputation"])
    if not (rank or rep):
        return {"found": False, "commander": name, "error": "no Rank or Reputation events in the journals"}
    values = dict((rank[0].data if rank else {}))
    if rank:
        for r in _after(session, fid, rank[0], ["Promotion"]):
            values.update({k: v for k, v in (r.data or {}).items() if k in RANK_NAMES})
    pdata = prog[0].data if prog else {}
    ranks = {}
    for k, names in RANK_NAMES.items():
        if k in values:
            v = values[k]
            ranks[k] = {"rank": v, "name": names[v] if 0 <= v < len(names) else None, "progress_pct": pdata.get(k)}
    skip = {"timestamp", "event"}
    return {"found": True, "commander": name, "ranks": ranks, "ranks_as_of": jfiles.iso(rank[0].ts) if rank else None,
            "reputation": {k: v for k, v in (rep[0].data if rep else {}).items() if k not in skip},
            "reputation_as_of": jfiles.iso(rep[0].ts) if rep else None}


@fresh_query
def commander_stats(session, commander=None, section=None):
    """The game's Statistics snapshot (bank, combat, trading, exploration, mining, ...); section picks one block."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    st = _latest(session, fid, ["Statistics"])
    if not st:
        return {"found": False, "commander": name, "error": "no Statistics event in the journals (the game writes it at login)"}
    data = {k: v for k, v in st[0].data.items() if k not in ("timestamp", "event")}
    if section:
        match = {k: v for k, v in data.items() if k.lower() == section.lower()}
        if not match:
            return {"found": False, "commander": name, "error": f"no section '{section}'", "sections": sorted(data)}
        data = match
    return {"found": True, "commander": name, "as_of": jfiles.iso(st[0].ts), "sections": sorted(data), "statistics": data}


@fresh_query
def carrier(session, commander=None):
    """Your fleet carrier as last reported (CarrierStats): fuel, balance, space usage, services, orders."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    st = _latest(session, fid, ["CarrierStats"])
    if not st:
        return {"found": False, "commander": name, "error": "no CarrierStats event (open carrier management in game)"}
    d = {k: v for k, v in st[0].data.items() if k not in ("timestamp", "event")}
    return {"found": True, "commander": name, "as_of": jfiles.iso(st[0].ts), "carrier": d}
