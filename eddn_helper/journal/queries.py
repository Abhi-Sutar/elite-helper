"""
Journal history queries over the database (session first, plain dicts back - same shape as eddn_helper.queries).

Every public function goes through `fresh_query`: before answering it checks whether the journal files on disk are
ahead of the database and ingests the difference (see ingest.ensure_fresh). The reply carries `journal_freshness`.
Pass fresh=False to skip the check, directory= to read another journal folder.
"""

import functools
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, func, or_, select

from ..models import JournalCommander, JournalEvent, JournalFile
from . import files as jfiles
from . import income, ingest

JUMP_EVENTS = ("FSDJump", "CarrierJump")
LEDGER_EVENTS = tuple(sorted(set(income.MAINTENANCE_EVENTS) | {
    "MarketBuy", "MarketSell", "RedeemVoucher", "MissionCompleted", "SellExplorationData", "MultiSellExplorationData",
    "Resurrected", "NpcCrewPaidWage", "CarrierBuy", "CarrierDecommission", "CarrierBankTransfer", "ShipyardBuy",
    "ShipyardSell", "ShipyardTransfer", "ModuleBuy", "ModuleSell", "ModuleSellRemote", "PayFines", "PayBounties",
    "CommunityGoalReward", "BuyExplorationData", "BuyTradeData"}))


def fresh_query(fn):
    @functools.wraps(fn)
    def wrapper(session, *args, directory=None, fresh=True, **kwargs):
        freshness = ingest.ensure_fresh(session, directory) if fresh else None
        result = fn(session, *args, **kwargs)
        if freshness is not None and isinstance(result, dict):
            result["journal_freshness"] = freshness
            if result.get("error") and freshness.get("ingest_error"):   # explain an empty answer
                result["error"] += f" (journal ingest: {freshness['ingest_error']})"
        return result
    return wrapper


def resolve_commander(session, commander=None):
    """(fid, name) for a commander given by name or FID, else the one seen most recently; (None, None) if unknown."""
    q = select(JournalCommander)
    if commander:
        c = commander.lower()
        q = q.where(or_(func.lower(JournalCommander.fid) == c, func.lower(JournalCommander.name) == c))
    row = session.execute(q.order_by(JournalCommander.last_seen.desc())).scalars().first()
    return (row.fid, row.name) if row else (None, None)


def no_commander(commander):
    return {"found": False, "error": (f"commander '{commander}' not found in the ingested journals" if commander
                                      else "no commander in the database - are there journal files? (journal-status)")}


def window(days=None, since=None, until=None):
    """(start, end) datetimes from days back and/or ISO since/until; None = open."""
    start = parse_time(since) if since else (datetime.now(timezone.utc) - timedelta(days=days) if days else None)
    return start, (parse_time(until) if until else None)


def parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    value = str(value).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"cannot read time '{value}' (use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC)")


def _listed(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [v.strip() for v in value if str(v).strip()]


def summarize(name, d):
    """A short readable line for common events (None for the rest)."""
    try:
        if name in JUMP_EVENTS:
            dist = d.get("JumpDist")
            return f"to {d.get('StarSystem')}" + (f" ({dist:.1f} ly)" if dist else "")
        if name == "Docked":
            return f"docked at {d.get('StationName')}"
        if name == "Undocked":
            return f"left {d.get('StationName')}"
        if name == "Location":
            return f"in {d.get('StarSystem')}" + (f" at {d['StationName']}" if d.get("StationName") else "")
        if name == "MarketBuy":
            return f"bought {d.get('Count')} {d.get('Type_Localised', d.get('Type'))} for {d.get('TotalCost'):,} Cr"
        if name == "MarketSell":
            return f"sold {d.get('Count')} {d.get('Type_Localised', d.get('Type'))} for {d.get('TotalSale'):,} Cr"
        if name == "MissionCompleted":
            return f"{d.get('LocalisedName')} (+{d.get('Reward', 0):,} Cr)"
        if name == "MissionAccepted":
            return d.get("LocalisedName")
        if name == "RedeemVoucher":
            return f"{d.get('Type')} voucher {d.get('Amount'):,} Cr"
        if name == "Scan":
            return f"{d.get('BodyName')} ({d.get('PlanetClass') or d.get('StarType') or 'body'})"
        if name == "LoadGame":
            return f"{d.get('Commander')} in {d.get('Ship_Localised') or d.get('Ship')}"
        if name == "Died":
            return f"killed by {d.get('KillerName_Localised') or d.get('KillerName')}"
        if name in ("SupercruiseEntry", "SupercruiseExit"):
            return d.get("StarSystem")
    except (TypeError, ValueError):
        return None
    return None


def event_rows(session, fid, names=None, start=None, end=None, ascending=True, limit=None):
    """(ts, file_id, line_no, event, data) rows of one commander, ordered by time."""
    q = select(JournalEvent.ts, JournalEvent.file_id, JournalEvent.line_no, JournalEvent.event, JournalEvent.data)
    q = q.where(JournalEvent.fid == fid)
    if names:
        q = q.where(JournalEvent.event.in_(list(names)))
    if start is not None:
        q = q.where(JournalEvent.ts >= start)
    if end is not None:
        q = q.where(JournalEvent.ts <= end)
    order = (JournalEvent.ts, JournalEvent.file_id, JournalEvent.line_no)
    q = q.order_by(*(c if ascending else c.desc() for c in order))
    if limit:
        q = q.limit(limit)
    return session.execute(q).all()


# ---- history and search --------------------------------------------------------------------------------------------

@fresh_query
def journal_events(session, event=None, system=None, station=None, text=None, days=None, since=None, until=None,
                   commander=None, limit=100, include_data=False, oldest_first=False):
    """Search the event history. event: one name or comma list; text: case-insensitive match anywhere in the event JSON."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, end = window(days, since, until)
    except ValueError as e:
        return {"error": str(e)}
    q = select(JournalEvent).where(JournalEvent.fid == fid)
    if _listed(event):
        q = q.where(func.lower(JournalEvent.event).in_([e.lower() for e in _listed(event)]))
    if system:
        q = q.where(func.lower(JournalEvent.system_name) == system.lower())
    if station:
        q = q.where(func.lower(JournalEvent.station) == station.lower())
    if text:
        q = q.where(func.lower(cast(JournalEvent.data, String)).contains(text.lower()))
    if start is not None:
        q = q.where(JournalEvent.ts >= start)
    if end is not None:
        q = q.where(JournalEvent.ts <= end)
    total = session.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    order = (JournalEvent.ts, JournalEvent.file_id, JournalEvent.line_no)
    rows = session.execute(q.order_by(*(order if oldest_first else [c.desc() for c in order])).limit(limit)).scalars().all()
    events = []
    for r in rows:
        item = {"ts": jfiles.iso(r.ts), "event": r.event, "system": r.system_name, "station": r.station,
                "body": r.body, "summary": summarize(r.event, r.data or {})}
        if include_data:
            item["data"] = r.data
        events.append(item)
    return {"found": True, "commander": name, "matched": total, "returned": len(events), "truncated": total > len(events),
            "events": events}


@fresh_query
def travel_log(session, days=30, since=None, until=None, commander=None, limit=100):
    """FSD and carrier jumps (newest first) with distance, fuel and time spent before the next jump, plus totals."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, end = window(days, since, until)
    except ValueError as e:
        return {"error": str(e)}
    rows = event_rows(session, fid, JUMP_EVENTS, start, end)
    jumps = []
    for i, r in enumerate(rows):
        d = r.data or {}
        nxt = rows[i + 1].ts if i + 1 < len(rows) else None
        jumps.append({"ts": jfiles.iso(r.ts), "system": d.get("StarSystem"), "jump_ly": d.get("JumpDist"),
                      "fuel_used": d.get("FuelUsed"), "fuel_level": d.get("FuelLevel"), "carrier": r.event == "CarrierJump",
                      "star_pos": d.get("StarPos"), "stayed_minutes": round((nxt - r.ts).total_seconds() / 60, 1) if nxt else None})
    total_ly = round(sum(j["jump_ly"] or 0 for j in jumps), 1)
    return {"found": True, "commander": name, "jumps": len(jumps), "total_ly": total_ly,
            "distinct_systems": len({j["system"] for j in jumps}),
            "first": jumps[0]["ts"] if jumps else None, "last": jumps[-1]["ts"] if jumps else None,
            "log": list(reversed(jumps))[:limit], "truncated": len(jumps) > limit}


@fresh_query
def visited_systems(session, days=None, since=None, commander=None, limit=50, system=None):
    """Systems jumped to, most recent first, with visit counts and first/last visit."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, _ = window(days, since)
    except ValueError as e:
        return {"error": str(e)}
    base = select(JournalEvent.system_name, JournalEvent.system_id64, func.count().label("visits"),
                  func.min(JournalEvent.ts).label("first"), func.max(JournalEvent.ts).label("last")) \
        .where(JournalEvent.fid == fid, JournalEvent.event.in_(JUMP_EVENTS))
    if start is not None:
        base = base.where(JournalEvent.ts >= start)
    if system:
        base = base.where(func.lower(JournalEvent.system_name) == system.lower())
    grouped = base.group_by(JournalEvent.system_name, JournalEvent.system_id64)
    total = session.execute(select(func.count()).select_from(grouped.subquery())).scalar_one()
    rows = session.execute(grouped.order_by(func.max(JournalEvent.ts).desc()).limit(limit)).all()
    return {"found": True, "commander": name, "distinct_systems": total, "returned": len(rows),
            "systems": [{"system": r.system_name, "id64": r.system_id64, "visits": r.visits,
                         "first_visit": jfiles.iso(r.first), "last_visit": jfiles.iso(r.last)} for r in rows]}


@fresh_query
def visited_stations(session, days=None, since=None, commander=None, limit=50, system=None):
    """Stations and carriers docked at, most recent first, with docking counts."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, _ = window(days, since)
    except ValueError as e:
        return {"error": str(e)}
    base = select(JournalEvent.station, JournalEvent.system_name, JournalEvent.market_id, func.count().label("dockings"),
                  func.min(JournalEvent.ts).label("first"), func.max(JournalEvent.ts).label("last")) \
        .where(JournalEvent.fid == fid, JournalEvent.event == "Docked")
    if start is not None:
        base = base.where(JournalEvent.ts >= start)
    if system:
        base = base.where(func.lower(JournalEvent.system_name) == system.lower())
    grouped = base.group_by(JournalEvent.station, JournalEvent.system_name, JournalEvent.market_id)
    total = session.execute(select(func.count()).select_from(grouped.subquery())).scalar_one()
    rows = session.execute(grouped.order_by(func.max(JournalEvent.ts).desc()).limit(limit)).all()
    return {"found": True, "commander": name, "distinct_stations": total, "returned": len(rows),
            "stations": [{"station": r.station, "system": r.system_name, "market_id": r.market_id, "dockings": r.dockings,
                          "first_docked": jfiles.iso(r.first), "last_docked": jfiles.iso(r.last)} for r in rows]}


@fresh_query
def sessions(session, days=None, since=None, commander=None, limit=50):
    """Play sessions (one per journal file), newest first, with wall-clock duration and event counts."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, _ = window(days, since)
    except ValueError as e:
        return {"error": str(e)}
    q = select(JournalFile).where(or_(JournalFile.fid == fid, JournalFile.fid.is_(None)))
    if start is not None:
        q = q.where(JournalFile.last_ts >= start)
    files = session.execute(q.order_by(JournalFile.name.desc()).limit(limit)).scalars().all()
    counts = dict(session.execute(select(JournalEvent.file_id, func.count()).where(
        JournalEvent.file_id.in_([f.id for f in files])).group_by(JournalEvent.file_id)).all()) if files else {}
    return {"found": True, "commander": name, "sessions": [
        {"file": f.name, "start": jfiles.iso(f.first_ts), "end": jfiles.iso(f.last_ts),
         "hours": round((f.last_ts - f.first_ts).total_seconds() / 3600, 2) if f.first_ts and f.last_ts else 0,
         "events": counts.get(f.id, 0), "ended_cleanly": f.complete} for f in files]}


# ---- money -----------------------------------------------------------------------------------------------------------

@fresh_query
def ledger(session, days=7, since=None, until=None, commander=None, category=None, limit=100):
    """
    Every credit movement the journal records (newest first), with per-category totals. net/running are the sum over
    the window, not your bank balance (carrier market sales and some rewards are not journal events).
    """
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        start, end = window(days, since, until)
    except ValueError as e:
        return {"error": str(e)}
    rows = event_rows(session, fid, LEDGER_EVENTS, start, end)
    entries = income.ledger_entries([(r.ts, r.data or {}) for r in rows])
    wanted = {c.lower() for c in _listed(category)}
    if wanted:
        entries = [e for e in entries if e["category"] in wanted]
    totals = {}
    running = 0
    for e in entries:
        running += e["amount"]
        e["running"] = running
        t = totals.setdefault(e["category"], {"earned": 0, "spent": 0, "net": 0, "count": 0})
        t["earned" if e["amount"] > 0 else "spent"] += abs(e["amount"])
        t["net"] += e["amount"]
        t["count"] += 1
    return {"found": True, "commander": name, "entries_total": len(entries), "net": running,
            "earned": sum(t["earned"] for t in totals.values()), "spent": sum(t["spent"] for t in totals.values()),
            "by_category": dict(sorted(totals.items(), key=lambda kv: kv[1]["net"], reverse=True)),
            "entries": list(reversed(entries))[:limit], "truncated": len(entries) > limit}


@fresh_query
def credit_rate(session, since=None, session_only=False, max_gap_minutes=None, streams=None, crew_wages=False,
                commander=None):
    """Same analysis as the file-based credit_rate, computed from the stored events (fast over the full history)."""
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return no_commander(commander)
    try:
        since_dt = income.parse_since(since)
    except ValueError as e:
        return {"error": str(e)}
    q = select(JournalFile.id, JournalFile.name).where(or_(JournalFile.fid == fid, JournalFile.fid.is_(None)))
    if since_dt is not None:
        q = q.where(JournalFile.last_ts >= since_dt)
    files = session.execute(q.order_by(JournalFile.name)).all()
    if session_only:
        files = files[-1:]

    def sessions_iter():
        for file_id, fname in files:
            rows = session.execute(select(JournalEvent.ts, JournalEvent.data).where(JournalEvent.file_id == file_id)
                                   .order_by(JournalEvent.line_no)).all()
            yield fname, [(r.ts, r.data) for r in rows]

    result = income.credit_rate_sessions(sessions_iter(), streams, since_dt, max_gap_minutes, crew_wages)
    result["commander"] = name
    result["source"] = "database"
    return result
