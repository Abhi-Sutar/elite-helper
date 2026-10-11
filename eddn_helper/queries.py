"""Read-side logic shared by the CLI and the MCP server. Every function returns plain dicts."""

import math
import re
import time
from datetime import datetime, timedelta, timezone

import bisect

from sqlalchemy import case, func, select

from . import config, database, lock, store
from .models import (Commodity, CommodityPrice, Faction, FactionHistory, Market, MarketUpdate, PriceHistory,
                     System, SystemUpdate)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def age_hours(dt):
    """Hours since a datetime, rounded to 0.1; None if missing."""
    if not dt:
        return None
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)


def norm_commodity(name):
    """'Liquid Oxygen' / 'liquid_oxygen' -> 'liquidoxygen' (EDDN symbol form)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def distance(a, b):
    if None in (a["x"], a["y"], a["z"], b["x"], b["y"], b["z"]):
        return None
    return math.sqrt((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2 + (a["z"] - b["z"]) ** 2)


def _pct(v):
    return round(v * 100, 1) if v is not None else None


def find_system(session, name):
    return session.execute(select(System).where(func.lower(System.name) == name.lower())).scalars().first()


def _state_items(items):
    """Normalise a stored state list (objects, or legacy plain strings) to [{"state", "trend"}]."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            if it.get("state"):
                out.append({"state": it["state"], "trend": it.get("trend")})
        elif it:
            out.append({"state": it, "trend": None})
    return out


def _faction_dict(f):
    active, pending, recovering = (_state_items(f.active_states), _state_items(f.pending_states),
                                   _state_items(f.recovering_states))
    return {
        "name": f.name, "influence_pct": _pct(f.influence), "state": f.state, "government": f.government,
        "allegiance": f.allegiance, "happiness": f.happiness,
        "active_states": [s["state"] for s in active],
        "pending_states": [s["state"] for s in pending],
        "recovering_states": [s["state"] for s in recovering],
        "pending_detail": pending, "recovering_detail": recovering,
        "is_controlling": bool(f.is_controlling), "source": f.source,
    }


def system_info(session, name):
    s = find_system(session, name)
    if s is None:
        return {"found": False, "system": name}
    facs = session.execute(select(Faction).where(Faction.system_id64 == s.id64)
                           .order_by(Faction.influence.desc())).scalars().all()
    return {
        "found": True, "system": s.name, "id64": s.id64,
        "coords": {"x": s.x, "y": s.y, "z": s.z},
        "population": s.population, "allegiance": s.allegiance, "government": s.government,
        "security": s.security, "economy": s.economy, "second_economy": s.second_economy,
        "controlling_faction": s.controlling_faction, "conflicts": s.conflicts or [],
        "info_updated_at": iso(s.updated_at), "factions_updated_at": iso(s.factions_updated_at),
        "factions_age_hours": age_hours(s.factions_updated_at), "source": s.source,
        "factions": [_faction_dict(f) for f in facs],
    }


def _history_point(r):
    return {"faction": r.faction, "ts": iso(r.ts), "present": bool(r.present),
            "influence_pct": _pct(r.influence), "state": r.state, "happiness": r.happiness,
            "active_states": [s["state"] for s in _state_items(r.active_states)],
            "pending_states": _state_items(r.pending_states),
            "recovering_states": _state_items(r.recovering_states)}


def _signature(p):
    """What counts as a change between two observations of the same faction (for thinning)."""
    return (p["present"], p["state"], p["happiness"], tuple(p["active_states"]),
            tuple((s["state"], s["trend"]) for s in p["pending_states"]),
            tuple((s["state"], s["trend"]) for s in p["recovering_states"]))


def _thin(points, max_points):
    """Reduce a list of observations to ~max_points, always keeping the first, last and every change."""
    if not max_points or len(points) <= max_points:
        return points
    keep = {0, len(points) - 1}
    prev = None
    for i, p in enumerate(points):
        sig = _signature(p)
        if sig != prev:
            keep.add(i)
        prev = sig
    spare = max_points - len(keep)
    if spare > 0:
        step = max(1, len(points) // spare)
        keep.update(range(0, len(points), step))
    return [points[i] for i in sorted(keep)]


def faction_history(session, system, days=30, faction=None, max_points=500):
    """
    Every stored observation of faction influence, state, happiness and active/pending/recovering states in a
    system (one row per observation in full mode). Long series are thinned per faction to ~max_points, keeping
    every change; a present=False point means the faction left the system.
    """
    s = find_system(session, system)
    if s is None:
        return {"found": False, "system": system}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(FactionHistory).where(FactionHistory.system_id64 == s.id64, FactionHistory.ts >= since)
    if faction:
        q = q.where(func.lower(FactionHistory.faction) == faction.lower())
    rows = session.execute(q.order_by(FactionHistory.faction, FactionHistory.ts)).scalars().all()
    by_faction = {}
    for r in rows:
        by_faction.setdefault(r.faction, []).append(_history_point(r))
    history = []
    for pts in by_faction.values():
        history.extend(_thin(pts, max_points))
    updates = session.execute(select(func.count()).select_from(SystemUpdate).where(
        SystemUpdate.system_id64 == s.id64, SystemUpdate.ts >= since)).scalar_one()
    return {"found": True, "system": s.name, "days": days, "history_mode": store.history_mode(session),
            "observations": updates or len(rows), "updates_source": "update log" if updates else "history rows",
            "rows": len(rows), "returned": len(history), "history": history}


def faction_timeline(session, system, faction, days=30, max_points=500):
    """
    Chart-ready view of one faction in one system: the influence series plus intervals for every state-like
    value - main state, happiness, and each individual active / pending / recovering state.
    An interval runs from the first observation that had the value to `ended_at`, the first observation where
    it was no longer there (null while still present); `last_seen` is the last observation that had it.
    """
    s = find_system(session, system)
    if s is None:
        return {"found": False, "system": system}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(FactionHistory).where(FactionHistory.system_id64 == s.id64, FactionHistory.ts >= since,
                                     func.lower(FactionHistory.faction) == faction.lower())
        .order_by(FactionHistory.ts)).scalars().all()
    if not rows:
        return {"found": False, "system": s.name, "faction": faction,
                "note": "no faction history in this window for that faction"}

    open_, done, influence, left = {}, [], [], []

    def close_all(at):
        for key, iv in list(open_.items()):
            iv["ended_at"] = at
            done.append(open_.pop(key))

    for r in rows:
        t = iso(r.ts)
        if not r.present:
            close_all(t)
            left.append(t)
            continue
        if r.influence is not None:
            influence.append({"ts": t, "influence_pct": _pct(r.influence)})
        now = {}
        if r.state and r.state != "None":
            now[("state", r.state)] = None
        if r.happiness:
            now[("happiness", r.happiness)] = None
        for field, items in (("active", r.active_states), ("pending", r.pending_states),
                             ("recovering", r.recovering_states)):
            for st in _state_items(items):
                now[(field, st["state"])] = st["trend"]
        for key in list(open_):
            if key not in now:
                open_[key]["ended_at"] = t
                done.append(open_.pop(key))
        for key, trend in now.items():
            if key in open_:
                open_[key]["last_seen"] = t
                open_[key]["trend"] = trend
            else:
                open_[key] = {"field": key[0], "value": key[1], "trend": trend, "start": t,
                              "last_seen": t, "ended_at": None}
    intervals = sorted(done + list(open_.values()), key=lambda iv: (iv["start"], iv["field"], iv["value"]))
    seen = [iso(t) for (t,) in session.execute(select(SystemUpdate.ts).where(
        SystemUpdate.system_id64 == s.id64, SystemUpdate.ts >= since).order_by(SystemUpdate.ts))]
    for iv in intervals:  # an unchanged value is not re-logged, so take last_seen from the update log
        lo = bisect.bisect_left(seen, iv["start"])
        hi = bisect.bisect_left(seen, iv["ended_at"]) if iv["ended_at"] else len(seen)
        if hi > lo and seen[hi - 1] > iv["last_seen"]:
            iv["last_seen"] = seen[hi - 1]
    return {"found": True, "system": s.name, "faction": rows[0].faction, "days": days,
            "observations": len(seen) or len(rows), "rows": len(rows),
            "first_observation": iso(rows[0].ts), "last_observation": iso(rows[-1].ts),
            "note": "intervals start at the first stored row inside the window; 'None' main state is omitted; "
                    "last_seen comes from the update log",
            "influence": _thin_influence(influence, max_points), "intervals": intervals, "left_events": left}


def _thin_influence(points, max_points):
    if not max_points or len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = {int(i * step) for i in range(max_points)} | {len(points) - 1}
    return [points[i] for i in sorted(picked)]


def faction_presence(session, name):
    """Every system a faction is currently in, with how much history exists there (to pick systems to drill into)."""
    base = (select(Faction, System.name.label("system_name"), System.factions_updated_at.label("fac_ts"))
            .join(System, System.id64 == Faction.system_id64))
    rows = session.execute(base.where(func.lower(Faction.name) == name.lower())
                           .order_by(Faction.influence.desc())).all()
    if not rows:  # fall back to substring match
        rows = session.execute(base.where(Faction.name.ilike(f"%{name}%"))
                               .order_by(Faction.influence.desc())).all()
    stats = {}
    if rows:
        names = {r.Faction.name for r in rows}
        for sid, fac, n, first in session.execute(
                select(FactionHistory.system_id64, FactionHistory.faction, func.count(), func.min(FactionHistory.ts))
                .where(FactionHistory.faction.in_(names), FactionHistory.present.is_(True))
                .group_by(FactionHistory.system_id64, FactionHistory.faction)):
            stats[(sid, fac)] = (n, first)
    return {"faction": name, "found": bool(rows), "systems": [
        {"system": r.system_name, "faction": r.Faction.name, "influence_pct": _pct(r.Faction.influence),
         "state": r.Faction.state, "is_controlling": bool(r.Faction.is_controlling),
         "age_hours": age_hours(r.fac_ts),
         "history_rows": stats.get((r.Faction.system_id64, r.Faction.name), (0, None))[0],
         "first_seen": iso(stats.get((r.Faction.system_id64, r.Faction.name), (0, None))[1])} for r in rows]}


def station_market(session, station, system=None):
    q = select(Market).where(func.lower(Market.station) == station.lower())
    if system:
        q = q.where(func.lower(Market.system_name) == system.lower())
    ms = session.execute(q).scalars().all()
    if not ms:
        return {"found": False, "station": station, "system": system}
    if len(ms) > 1:
        return {"found": True, "ambiguous": True, "station": station,
                "candidates": [{"station": m.station, "system": m.system_name, "market_id": m.market_id}
                               for m in ms]}
    m = ms[0]
    prices = session.execute(
        select(Commodity.symbol, CommodityPrice).join(Commodity, Commodity.id == CommodityPrice.commodity_id)
        .where(CommodityPrice.market_id == m.market_id).order_by(Commodity.symbol)).all()
    stale_before = m.updated_at - COMMODITY_FRESH_WINDOW
    return {"found": True, "station": m.station, "system": m.system_name, "market_id": m.market_id,
            "station_type": m.station_type, "is_carrier": bool(m.is_carrier),
            "economies": m.economies or [], "prohibited": m.prohibited or [],
            "updated_at": iso(m.updated_at), "age_hours": age_hours(m.updated_at), "source": m.source,
            "commodities": [{"commodity": sym, "buy_price": p.buy_price, "sell_price": p.sell_price,
                             "mean_price": p.mean_price, "stock": p.stock, "demand": p.demand,
                             "stock_bracket": p.stock_bracket, "demand_bracket": p.demand_bracket,
                             "seen_at": iso(p.seen_at), "seen_age_hours": age_hours(p.seen_at),
                             "stale": bool(p.seen_at and p.seen_at < stale_before)}
                            for sym, p in prices]}


PRICE_MARGIN = timedelta(days=3)   # look this far before the window for the state that was in force at its start
SERIES_STALE_AFTER = timedelta(hours=36)  # anchor interval (24 h) + slack: an older state is no longer trusted
COMMODITY_FRESH_WINDOW = timedelta(hours=48)  # a commodity not seen for this long before the market's last report is stale
MAX_SAMPLES = 5000


def _stats(values):
    vals = [v for v in values if v]
    return {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals))} if vals else None


def _mm(values, drop_zero):
    """(min, avg, max) of the values; prices drop 0 (side inactive), amounts keep 0 (a real value)."""
    vals = [v for v in values if v is not None and (v or not drop_zero)]
    if not vals:
        return None, None, None
    return min(vals), round(sum(vals) / len(vals)), max(vals)


def _listed(row):
    """False for a row with every value NULL (nothing known about the commodity)."""
    return any(row[k] is not None for k in ("buy_price", "sell_price", "stock", "demand"))


def _bucketed(rows, minutes, update_times=None, carried=None):
    """
    Summarise change rows into fixed time buckets. A bucket's statistics cover the state carried in from before
    it plus the rows inside it, so quiet buckets still describe the state in force. `updates` counts every EDDN
    update in the bucket (from the update log); `changes` counts the stored rows. With update_times=None
    (data from before the update log existed) `updates` falls back to the row count.
    """
    width = minutes * 60

    def key(dt):
        return int(dt.timestamp()) // width
    by_rows, by_updates = {}, {}
    for r in rows:
        by_rows.setdefault(key(r["dt"]), []).append(r)
    for t in update_times or ():
        by_updates[key(t)] = by_updates.get(key(t), 0) + 1
    keys = sorted(set(by_rows) | set(by_updates))
    out, state = [], carried
    for k in keys:
        rs = by_rows.get(k, [])
        bucket_start = datetime.fromtimestamp(k * width, timezone.utc)
        usable = state is not None and (bucket_start - state["dt"]) <= SERIES_STALE_AFTER
        sample = ([state] if usable else []) + rs
        listed = [r for r in sample if _listed(r)]
        b = {"ts": iso(datetime.fromtimestamp(k * width, timezone.utc)),
             "updates": by_updates.get(k, 0) if update_times is not None else len(rs), "changes": len(rs)}
        for name, drop_zero in (("buy_price", True), ("sell_price", True), ("stock", False), ("demand", False)):
            lo, avg, hi = _mm([r[name] for r in listed], drop_zero)
            short = name.replace("_price", "")
            b[f"{short}_min"], b[f"{short}_avg"], b[f"{short}_max"] = lo, avg, hi
        b["mean_avg"] = _mm([r["mean_price"] for r in listed], True)[1]
        last = listed[-1] if listed else None
        b["stock_bracket"] = last["stock_bracket"] if last else None
        b["demand_bracket"] = last["demand_bracket"] if last else None
        out.append(b)
        if rs:
            state = rs[-1]
    return out


def _resampled(rows, minutes, update_times, carried, start, end):
    """Evenly spaced samples: the last known state at each instant (carried forward), plus the updates in each step."""
    width = minutes * 60
    first, last = math.ceil(start.timestamp() / width), int(end.timestamp()) // width
    state, i, u, out = carried, 0, 0, []
    times = sorted(t.timestamp() for t in update_times or ())
    for k in range(first, last + 1):
        t = k * width
        n = 0
        while i < len(rows) and rows[i]["dt"].timestamp() <= t:
            state = rows[i]
            i += 1
            n += 1
        while u < len(times) and times[u] <= t - width:
            u += 1
        v = u
        while v < len(times) and times[v] <= t:
            v += 1
        ups, u = v - u, v
        if state is None:
            continue
        now_t = datetime.fromtimestamp(t, timezone.utc)
        stale = (now_t - state["dt"]) > SERIES_STALE_AFTER
        out.append({"ts": iso(now_t),
                    **{f: (None if stale else state[f]) for f in ("buy_price", "sell_price", "mean_price", "stock",
                                                                "demand", "stock_bracket", "demand_bracket")},
                    "listed": _listed(state) and not stale, "stale": stale, "updates": ups, "carried": n == 0})
    return out


def _point(r):
    return {"ts": iso(r["dt"]), "buy_price": r["buy_price"], "sell_price": r["sell_price"],
            "mean_price": r["mean_price"], "stock": r["stock"], "demand": r["demand"],
            "stock_bracket": r["stock_bracket"], "demand_bracket": r["demand_bracket"], "listed": _listed(r)}


def _series_reply(raw, carried, update_times, since, now, bucket_minutes, resample_minutes, max_points):
    """Summary + series for one commodity: `raw` = change rows inside the window, `carried` = row in force before it."""
    basis = ([carried] if carried is not None else []) + raw
    listed = [r for r in basis if _listed(r)]
    result = {"rows": len(raw), "first_update": iso(raw[0]["dt"]) if raw else None,
              "last_update": iso(raw[-1]["dt"]) if raw else None,
              "state_at_start": ({k: v for k, v in _point(carried).items() if k != "ts"} if carried else None),
              "buy_price_stats": _stats(r["buy_price"] for r in listed),
              "sell_price_stats": _stats(r["sell_price"] for r in listed),
              "stock_stats": _stats(r["stock"] for r in listed),
              "demand_stats": _stats(r["demand"] for r in listed)}
    if resample_minutes:
        minutes = resample_minutes
        while (now - since).total_seconds() / 60 / minutes > MAX_SAMPLES:
            minutes *= 2
        result["resample_minutes"] = minutes
        result["series"] = _resampled(raw, minutes, update_times, carried, since, now)
    else:
        if bucket_minutes is None and max_points and len(raw) > max_points:
            span = (raw[-1]["dt"] - raw[0]["dt"]).total_seconds() / 60
            bucket_minutes = max(1, math.ceil(span / max_points))
        if bucket_minutes:
            result["bucket_minutes"] = bucket_minutes
            result["series"] = _bucketed(raw, bucket_minutes, update_times, carried)
        else:
            result["series"] = [_point(r) for r in raw]
    result["points"] = len(result["series"])
    return result


def _split_commodities(commodity):
    """'gold' -> ['gold']; 'gold, tea' -> ['gold','tea']; 'all' / '*' / None -> None (every commodity)."""
    if commodity is None:
        return None
    if isinstance(commodity, (list, tuple)):
        names = list(commodity)
    else:
        names = [c for c in re.split(r"[,;]", commodity) if c.strip()]
    if any(n.strip().lower() in ("all", "*") for n in names):
        return None
    return [norm_commodity(n) for n in names]


def price_history(session, station, commodity, system=None, days=30, bucket_minutes=None, max_points=500,
                  all_commodities=False, max_commodities=25, resample_minutes=None):
    """
    Price and amount time series at one station. `commodity` is one name (single-commodity reply), a comma list,
    or 'all' (-> `series_by_commodity`, busiest first, capped at `max_commodities` unless all_commodities=True).
    Each point has buy/sell price, mean price, stock (supply), demand and the brackets. buy_price is what the
    station sells at (needs stock), sell_price what it pays (needs demand); an inactive side reports 0, and
    `listed: false` / `stale: true` mark resampled points whose last stored state is older than 36 hours (the
    daily anchor row should have refreshed it, so the commodity is probably no longer reported).

    In the default 'changes' mode the stored rows are change points (plus daily anchors): the value at any time is
    the latest row at or before it, `state_at_start` is the state in force when the window opens, and
    `updates` is the true number of EDDN updates (from the update log) while `rows` is the number of stored rows.
    Use `resample_minutes` for evenly spaced samples (carried forward), or `bucket_minutes` / `max_points` for
    min/avg/max summaries per time bucket.
    """
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    wanted = _split_commodities(commodity)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    market_id = cur["market_id"]
    base = (select(PriceHistory, Commodity.symbol).join(Commodity, Commodity.id == PriceHistory.commodity_id)
            .where(PriceHistory.market_id == market_id, PriceHistory.ts >= since - PRICE_MARGIN))
    truncated, available = False, None
    if wanted is not None:
        base = base.where(Commodity.symbol.in_(wanted))
    else:
        counts = session.execute(
            select(Commodity.symbol, func.count()).select_from(PriceHistory)
            .join(Commodity, Commodity.id == PriceHistory.commodity_id)
            .where(PriceHistory.market_id == market_id, PriceHistory.ts >= since)
            .group_by(Commodity.symbol).order_by(func.count().desc(), Commodity.symbol)).all()
        available = len(counts)
        chosen = [sym for sym, _ in counts]
        if not all_commodities and len(chosen) > max_commodities:
            chosen, truncated = chosen[:max_commodities], True
        base = base.where(Commodity.symbol.in_(chosen))
    raw_by, pre_by = {}, {}
    for r, sym in session.execute(base.order_by(Commodity.symbol, PriceHistory.ts)):
        row = {"dt": r.ts, "buy_price": r.buy_price, "sell_price": r.sell_price, "mean_price": r.mean_price,
               "stock": r.stock, "demand": r.demand, "stock_bracket": r.stock_bracket,
               "demand_bracket": r.demand_bracket}
        if r.ts < since:
            pre_by[sym] = row            # ordered by ts, so the last one wins = the state in force at the start
        else:
            raw_by.setdefault(sym, []).append(row)
    update_times = [t for (t,) in session.execute(
        select(MarketUpdate.ts).where(MarketUpdate.market_id == market_id, MarketUpdate.ts >= since)
        .order_by(MarketUpdate.ts))]
    source = "update log" if update_times else "history rows"
    times = update_times if update_times else None
    head = {"found": True, "station": cur["station"], "system": cur["system"], "days": days,
            "history_mode": store.history_mode(session), "updates": len(update_times),
            "updates_source": source}

    def reply(sym):
        return _series_reply(raw_by.get(sym, []), pre_by.get(sym), times, since, now, bucket_minutes,
                             resample_minutes, max_points)
    if wanted is not None and len(wanted) == 1:
        sym = wanted[0]
        body = reply(sym)
        if times is None:
            head["updates"] = body["rows"]
        return {**head, "commodity": sym, **body}
    syms = set(raw_by) | set(pre_by) | set(wanted or [])
    series = {sym: reply(sym) for sym in syms}
    if times is None:
        head["updates"] = max((b["rows"] for b in series.values()), default=0)
    out = {**head, "commodities": sorted(series), "series_by_commodity": {k: series[k] for k in sorted(series)}}
    if wanted is None:
        out.update({"commodities_available": available, "truncated": truncated})
    return out


def _bucket_series(times_rows, minutes):
    """times_rows: iterable of (datetime, uploader, changed) -> ordered bucket dicts with gaps filled with zeros."""
    width = minutes * 60
    buckets = {}
    for dt, uploader, changed in times_rows:
        k = int(dt.timestamp()) // width
        b = buckets.setdefault(k, {"updates": 0, "uploaders": set(), "changed": 0})
        b["updates"] += 1
        b["changed"] += changed or 0
        if uploader:
            b["uploaders"].add(uploader)
    if not buckets:
        return []
    return [{"ts": iso(datetime.fromtimestamp(k * width, timezone.utc)),
             "updates": buckets.get(k, {}).get("updates", 0),
             "distinct_uploaders": len(buckets.get(k, {}).get("uploaders", ())),
             "changed": buckets.get(k, {}).get("changed", 0)}
            for k in range(min(buckets), max(buckets) + 1)]


def _traffic_reply(rows, first_ever, days, bucket_minutes, max_points):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = list(rows)
    observed_from = max(since, first_ever) if first_ever else since
    hours = max((now - observed_from).total_seconds() / 3600, 1 / 60)
    span_min = (now - observed_from).total_seconds() / 60
    if max_points and span_min / bucket_minutes > max_points:
        bucket_minutes = max(1, math.ceil(span_min / max_points))
    series = _bucket_series(rows, bucket_minutes)
    uploaders = {u for _, u, _ in rows if u}
    busiest = max(series, key=lambda b: b["updates"]) if series else None
    return {"days": days, "bucket_minutes": bucket_minutes, "updates": len(rows),
            "distinct_uploaders": len(uploaders), "changed": sum(c or 0 for _, _, c in rows),
            "blank_uploader_updates": sum(1 for _, u, _ in rows if not u),
            "observed_hours": round(hours, 1), "updates_per_hour": round(len(rows) / hours, 2),
            "first_update": iso(min((r[0] for r in rows), default=None)),
            "last_update": iso(max((r[0] for r in rows), default=None)),
            "busiest_bucket": ({"ts": busiest["ts"], "updates": busiest["updates"]} if busiest else None),
            "series": series}


def station_traffic(session, station, system=None, days=7, bucket_minutes=60, max_points=500):
    """How often a station is reported to EDDN: updates (and distinct uploaders) per time bucket, from the update log."""
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(select(MarketUpdate.ts, MarketUpdate.uploader, MarketUpdate.changed)
                           .where(MarketUpdate.market_id == cur["market_id"], MarketUpdate.ts >= since)
                           .order_by(MarketUpdate.ts)).all()
    first_ever = session.execute(select(func.min(MarketUpdate.ts)).where(
        MarketUpdate.market_id == cur["market_id"])).scalar_one()
    software = session.execute(
        select(MarketUpdate.software, func.count()).where(MarketUpdate.market_id == cur["market_id"],
                                                          MarketUpdate.ts >= since)
        .group_by(MarketUpdate.software).order_by(func.count().desc()).limit(5)).all()
    return {"found": True, "station": cur["station"], "system": cur["system"], "is_carrier": cur["is_carrier"],
            "software": [{"software": s or "(unknown)", "updates": n} for s, n in software],
            **_traffic_reply([tuple(r) for r in rows], first_ever, days, bucket_minutes, max_points)}


def system_traffic(session, system, days=7, bucket_minutes=60, max_points=500):
    """How often a system's factions are reported to EDDN (journal messages with faction data)."""
    s = find_system(session, system)
    if s is None:
        return {"found": False, "system": system}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(select(SystemUpdate.ts, SystemUpdate.uploader, SystemUpdate.changed)
                           .where(SystemUpdate.system_id64 == s.id64, SystemUpdate.ts >= since)
                           .order_by(SystemUpdate.ts)).all()
    first_ever = session.execute(select(func.min(SystemUpdate.ts)).where(
        SystemUpdate.system_id64 == s.id64)).scalar_one()
    return {"found": True, "system": s.name,
            **_traffic_reply([tuple(r) for r in rows], first_ever, days, bucket_minutes, max_points)}


def _uploaders(col):
    return func.count(func.distinct(case((col != "", col))))


def busiest_stations(session, days=1, limit=20, carriers=True):
    """Leaderboard of the most frequently reported stations (updates, distinct uploaders, share that changed prices)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = (select(Market.station, Market.system_name, Market.is_carrier, func.count().label("updates"),
                _uploaders(MarketUpdate.uploader).label("uploaders"),
                func.sum(MarketUpdate.changed).label("changed"))
         .join(Market, Market.market_id == MarketUpdate.market_id)
         .where(MarketUpdate.ts >= since).group_by(Market.market_id, Market.station, Market.system_name, Market.is_carrier)
         .order_by(func.count().desc()).limit(limit))
    if not carriers:
        q = q.where(Market.is_carrier.is_(False))
    total = session.execute(select(func.count()).select_from(MarketUpdate).where(MarketUpdate.ts >= since)).scalar_one()
    return {"days": days, "total_updates": total, "stations": [
        {"station": r.station, "system": r.system_name, "is_carrier": bool(r.is_carrier), "updates": r.updates,
         "updates_per_hour": round(r.updates / (days * 24), 2), "distinct_uploaders": r.uploaders,
         "price_rows_written": int(r.changed or 0), "share_of_all_updates_pct": round(100 * r.updates / total, 1)}
        for r in session.execute(q)]}


def busiest_systems(session, days=1, limit=20):
    """Leaderboard of systems whose factions are reported most often."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = (select(System.name, func.count().label("updates"), _uploaders(SystemUpdate.uploader).label("uploaders"),
                func.sum(SystemUpdate.changed).label("changed"))
         .join(System, System.id64 == SystemUpdate.system_id64).where(SystemUpdate.ts >= since)
         .group_by(System.id64, System.name).order_by(func.count().desc()).limit(limit))
    total = session.execute(select(func.count()).select_from(SystemUpdate).where(SystemUpdate.ts >= since)).scalar_one()
    return {"days": days, "total_updates": total, "systems": [
        {"system": r.name, "updates": r.updates, "updates_per_hour": round(r.updates / (days * 24), 2),
         "distinct_uploaders": r.uploaders, "faction_rows_written": int(r.changed or 0),
         "share_of_all_updates_pct": round(100 * r.updates / total, 1)} for r in session.execute(q)]}


def station_activity(session, station, system=None, days=30):
    """How many market updates the database holds for a station, per day (from the update log)."""
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stamps = session.execute(
        select(MarketUpdate.ts).where(MarketUpdate.market_id == cur["market_id"], MarketUpdate.ts >= since)
        .order_by(MarketUpdate.ts)).scalars().all()
    source = "update log"
    if not stamps:  # data from before the update log existed: distinct history timestamps
        stamps = session.execute(
            select(PriceHistory.ts).where(PriceHistory.market_id == cur["market_id"], PriceHistory.ts >= since)
            .distinct().order_by(PriceHistory.ts)).scalars().all()
        source = "history rows"
    per_day = {}
    for t in stamps:
        day = t.strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
    rows = session.execute(select(func.count()).select_from(PriceHistory).where(
        PriceHistory.market_id == cur["market_id"], PriceHistory.ts >= since)).scalar_one()
    mode = store.history_mode(session)
    return {"found": True, "station": cur["station"], "system": cur["system"], "days": days,
            "history_mode": mode, "updates": len(stamps), "updates_source": source, "price_rows": rows,
            "note": ("every accepted EDDN update is in the update log; price rows are written only when a "
                     "value changed" if mode == "changes" else
                     "price rows are written for every update" if mode == "full" else
                     "thresholded mode: price rows are meaningful changes only"),
            "first_update": iso(stamps[0]) if stamps else None, "last_update": iso(stamps[-1]) if stamps else None,
            "current_snapshot_at": cur["updated_at"],
            "per_day": [{"date": d, "updates": n} for d, n in per_day.items()]}


def _fresh_cutoff(max_age_hours):
    if max_age_hours is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


def _candidate_rows(session, commodity_symbol, origin, ly, max_age_hours, include_carriers):
    """Prices joined to markets and systems, filtered by freshness/carrier/range. Returns [(dict, distance)]."""
    q = (select(CommodityPrice.market_id, Commodity.symbol.label("commodity"), CommodityPrice.buy_price,
                CommodityPrice.sell_price, CommodityPrice.mean_price, CommodityPrice.stock,
                CommodityPrice.demand, Market.station, Market.system_name, Market.is_carrier,
                func.coalesce(CommodityPrice.seen_at, Market.updated_at).label("updated_at"),
                System.x, System.y, System.z)
         .join(Market, Market.market_id == CommodityPrice.market_id)
         .join(Commodity, Commodity.id == CommodityPrice.commodity_id)
         .outerjoin(System, func.lower(System.name) == func.lower(Market.system_name))
         .where(func.coalesce(CommodityPrice.seen_at, Market.updated_at) >= _fresh_cutoff(max_age_hours)))
    if commodity_symbol:
        q = q.where(Commodity.symbol == commodity_symbol)
    if not include_carriers:
        q = q.where(Market.is_carrier.is_(False))
    out = []
    for row in session.execute(q):
        r = dict(row._mapping)
        d = None
        if origin is not None:
            d = distance(origin, r)
            if d is None or (ly is not None and d > ly):
                continue
        out.append((r, d))
    return out


def _resolve_origin(session, near):
    if not near:
        return None, None
    o = find_system(session, near)
    if o is None:
        return None, {"error": f"origin system '{near}' not in database (coordinates unknown); "
                               "try a live lookup first"}
    return {"name": o.name, "x": o.x, "y": o.y, "z": o.z}, None


def find_commodity(session, commodity, mode="sell", near=None, ly=None, min_amount=0,
                   max_age_hours=48, include_carriers=True, limit=15):
    """
    mode='sell': where you can SELL it for the most (needs demand).
    mode='buy' : where you can BUY it cheapest (needs stock).
    """
    sym = norm_commodity(commodity)
    origin, err = _resolve_origin(session, near)
    if err:
        return err
    rows = _candidate_rows(session, sym, origin, ly, max_age_hours, include_carriers)
    results = []
    for r, d in rows:
        if mode == "sell":
            if not r["sell_price"] or (r["demand"] or 0) < max(min_amount, 1):
                continue
        else:
            if not r["buy_price"] or (r["stock"] or 0) < max(min_amount, 1):
                continue
        results.append({
            "station": r["station"], "system": r["system_name"], "is_carrier": bool(r["is_carrier"]),
            "price": r["sell_price"] if mode == "sell" else r["buy_price"],
            "amount": r["demand"] if mode == "sell" else r["stock"],
            "mean_price": r["mean_price"],
            "distance_ly": round(d, 1) if d is not None else None,
            "age_hours": age_hours(r["updated_at"])})
    results.sort(key=lambda x: -x["price"] if mode == "sell" else x["price"])
    return {"commodity": sym, "mode": mode, "origin": near, "count": len(results),
            "results": results[:limit]}


def best_trades(session, origin_name, ly=30, cargo=None, max_age_hours=24, min_amount=100,
                include_carriers=False, limit=10, per_side=5):
    """Best buy-here / sell-there pairs among markets within `ly` of the origin system."""
    origin, err = _resolve_origin(session, origin_name)
    if err:
        return err
    if origin is None:
        return {"error": "origin system required"}
    rows = _candidate_rows(session, None, origin, ly, max_age_hours, include_carriers)
    by_commodity = {}
    for r, d in rows:
        by_commodity.setdefault(r["commodity"], []).append(r)
    trades = []
    for com, rs in by_commodity.items():
        buyers = sorted((r for r in rs if r["buy_price"] and (r["stock"] or 0) >= min_amount),
                        key=lambda r: r["buy_price"])[:per_side]
        sellers = sorted((r for r in rs if r["sell_price"] and (r["demand"] or 0) >= min_amount),
                         key=lambda r: -r["sell_price"])[:per_side]
        for b in buyers:
            for s in sellers:
                if b["market_id"] == s["market_id"]:
                    continue
                profit = s["sell_price"] - b["buy_price"]
                if profit <= 0:
                    continue
                qty = min(b["stock"], s["demand"], cargo) if cargo else min(b["stock"], s["demand"])
                leg = distance(b, s)
                trades.append({
                    "commodity": com, "profit_per_ton": profit, "tons": qty, "total_profit": profit * qty,
                    "buy_at": f"{b['station']} ({b['system_name']})", "buy_price": b["buy_price"],
                    "supply": b["stock"],
                    "sell_at": f"{s['station']} ({s['system_name']})", "sell_price": s["sell_price"],
                    "demand": s["demand"], "leg_distance_ly": round(leg, 1) if leg is not None else None,
                    "buy_age_hours": age_hours(b["updated_at"]), "sell_age_hours": age_hours(s["updated_at"])})
    trades.sort(key=lambda t: -(t["total_profit"] if cargo else t["profit_per_ton"]))
    return {"origin": origin["name"], "radius_ly": ly, "cargo": cargo,
            "markets_considered": len({r["market_id"] for r, _ in rows}), "trades": trades[:limit]}


def data_status(session):
    def n(model):
        return session.execute(select(func.count()).select_from(model)).scalar_one()
    hb = store.get_meta(session, "listener_heartbeat")
    last_msg = store.get_meta(session, "last_message_at")
    newest_m = session.execute(select(func.max(Market.updated_at)).where(Market.source == "eddn")).scalar_one()
    newest_s = session.execute(select(func.max(System.factions_updated_at))
                               .where(System.source == "eddn")).scalar_one()
    hb_age = round((time.time() - int(hb)) / 60, 1) if hb else None
    bind = session.get_bind()
    url = bind.url.render_as_string(hide_password=False)
    running, pid = lock.holder(config.lock_path(url))
    db_file = config.sqlite_path(url)
    return {"backend": bind.dialect.name, "database": database.masked_url(url),
            "size_mb": round(db_file.stat().st_size / 1e6, 1) if db_file and db_file.exists() else None,
            "systems": n(System), "factions": n(Faction), "markets": n(Market),
            "commodity_prices": n(CommodityPrice), "price_history_rows": n(PriceHistory),
            "faction_history_rows": n(FactionHistory),
            "newest_market_update": iso(newest_m), "newest_faction_update": iso(newest_s),
            "last_message_minutes_ago": round((time.time() - int(last_msg)) / 60, 1) if last_msg else None,
            "listener_heartbeat_minutes_ago": hb_age,
            "listener_running": running, "listener_pid": pid}


# ---- economies and system search ---------------------------------------------------------------------------------

_KEY_ALIASES = {"agriculture": "agri"}


def norm_key(text):
    """'High Tech' / 'hightech' / 'HighTech' -> 'hightech'; 'Agriculture' -> 'agri'. Used for economies and states."""
    key = re.sub(r"[\s_\-]+", "", str(text or "")).lower()
    return _KEY_ALIASES.get(key, key)


def _as_list(value):
    """Accept a list or a comma separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def _system_markets(session, system_ids=None, include_carriers=False):
    """{system_id64: [Market]}; a market belongs to its system_id64 or, when unlinked, to the system of that name."""
    q = (select(Market, func.coalesce(Market.system_id64, System.id64).label("sid"))
         .outerjoin(System, func.lower(System.name) == func.lower(Market.system_name)))
    if not include_carriers:
        q = q.where(Market.is_carrier.is_(False))
    out = {}
    for m, sid in session.execute(q):
        if sid is not None and (system_ids is None or sid in system_ids):
            out.setdefault(sid, []).append(m)
    return out


def _station_dict(m):
    return {"station": m.station, "station_type": m.station_type, "is_carrier": bool(m.is_carrier),
            "economies": m.economies or [], "market_age_hours": age_hours(m.updated_at)}


def system_economy(session, name):
    """A system's economy and second economy plus its stations (type, station economies). Snapshot only."""
    s = find_system(session, name)
    if s is None:
        return {"found": False, "system": name}
    markets = _system_markets(session, {s.id64}, include_carriers=True).get(s.id64, [])
    markets.sort(key=lambda m: (bool(m.is_carrier), m.station.lower()))
    return {"found": True, "system": s.name, "id64": s.id64, "economy": s.economy, "second_economy": s.second_economy,
            "population": s.population, "allegiance": s.allegiance, "government": s.government,
            "info_updated_at": iso(s.updated_at), "info_age_hours": age_hours(s.updated_at), "source": s.source,
            "stations": [_station_dict(m) for m in markets]}


def _faction_state_hits(f, state_key, state_in):
    """Where a faction has the given state: [{'in': 'state'|'active'|'pending'|'recovering', 'trend': ...}]
    ('state' = its main FactionState; trend is only set for pending/recovering states)."""
    hits = []
    if state_in in ("any", "state") and norm_key(f.state) == state_key:
        hits.append({"in": "state", "trend": None})
    for slot, items in (("active", f.active_states), ("pending", f.pending_states), ("recovering", f.recovering_states)):
        if state_in in ("any", slot):
            hits += [{"in": slot, "trend": s["trend"]} for s in _state_items(items) if norm_key(s["state"]) == state_key]
    return hits


def find_systems(session, economies=None, economy_scope="system", include_second=True, faction_state=None,
                 state_in="any", controlling_only=False, near=None, ly=None, min_population=0,
                 demand_commodities=None, include_carrier_markets=False, max_age_hours=None, limit=20):
    """
    Systems matching ALL given criteria, nearest first (or most populated first without `near`).
      economies        - names, e.g. ['Extraction', 'Refinery'] (case/space-insensitive; any of them matches)
      economy_scope    - 'system' (system economy and, with include_second, second economy), 'station' (any station's
                         economy list) or 'any'
      faction_state    - e.g. 'Expansion'; state_in: 'any' (main state or any list), 'state', 'active', 'pending' or
                         'recovering'; controlling_only restricts it to the controlling faction
      demand_commodities - keep systems with a station that currently has demand for any of these commodities
    Snapshot data: only systems the listener (or a live lookup) has seen, as fresh as the age fields say.
    """
    if economy_scope not in ("system", "station", "any"):
        return {"error": "economy_scope must be 'system', 'station' or 'any'"}
    if state_in not in ("any", "state", "active", "pending", "recovering"):
        return {"error": "state_in must be 'any', 'state', 'active', 'pending' or 'recovering'"}
    origin, err = _resolve_origin(session, near)
    if err:
        return err
    eco_keys = {norm_key(e) for e in _as_list(economies)}
    state_key = norm_key(faction_state) if faction_state else None
    commodities = [norm_commodity(c) for c in _as_list(demand_commodities)]

    markets = _system_markets(session, None, include_carriers=include_carrier_markets)
    station_hits = {}
    if eco_keys and economy_scope in ("station", "any"):
        for sid, ms in markets.items():
            hit = [m for m in ms if eco_keys & {norm_key(e) for e in (m.economies or [])}]
            if hit:
                station_hits[sid] = hit

    q = select(System)
    if min_population:
        q = q.where(System.population >= min_population)
    if max_age_hours is not None:
        q = q.where(func.coalesce(System.factions_updated_at, System.updated_at) >= _fresh_cutoff(max_age_hours))
    if origin is not None and ly is not None and None not in (origin["x"], origin["y"], origin["z"]):
        q = q.where(System.x.between(origin["x"] - ly, origin["x"] + ly),
                    System.y.between(origin["y"] - ly, origin["y"] + ly),
                    System.z.between(origin["z"] - ly, origin["z"] + ly))
    if eco_keys and economy_scope == "station":
        q = q.where(System.id64.in_(list(station_hits) or [-1]))

    candidates = []
    for s in session.execute(q).scalars():
        d = distance(origin, {"x": s.x, "y": s.y, "z": s.z}) if origin is not None else None
        if origin is not None and (d is None or (ly is not None and d > ly)):
            continue
        matched = None
        if eco_keys:
            if economy_scope in ("system", "any") and norm_key(s.economy) in eco_keys:
                matched = "economy"
            elif economy_scope in ("system", "any") and include_second and norm_key(s.second_economy) in eco_keys:
                matched = "second_economy"
            elif economy_scope in ("station", "any") and s.id64 in station_hits:
                matched = "station"
            if matched is None:
                continue
        candidates.append((s, d, matched))

    faction_hits = {}
    if state_key:
        ids = [s.id64 for s, _, _ in candidates]
        for i in range(0, len(ids), 500):
            fq = select(Faction).where(Faction.system_id64.in_(ids[i:i + 500]))
            if controlling_only:
                fq = fq.where(Faction.is_controlling.is_(True))
            for f in session.execute(fq).scalars():
                hits = _faction_state_hits(f, state_key, state_in)
                if hits:
                    faction_hits.setdefault(f.system_id64, []).append(
                        {"faction": f.name, "influence_pct": _pct(f.influence), "state": f.state,
                         "found_in": hits, "is_controlling": bool(f.is_controlling)})
        candidates = [c for c in candidates if c[0].id64 in faction_hits]

    demand_rows = {}
    if commodities:
        ids = {s.id64 for s, _, _ in candidates}
        fresh = _fresh_cutoff(48 if max_age_hours is None else max_age_hours)
        dq = (select(Commodity.symbol, CommodityPrice.demand, CommodityPrice.sell_price, Market.station,
                     func.coalesce(Market.system_id64, System.id64).label("sid"))
              .select_from(CommodityPrice)
              .join(Market, Market.market_id == CommodityPrice.market_id)
              .join(Commodity, Commodity.id == CommodityPrice.commodity_id)
              .outerjoin(System, func.lower(System.name) == func.lower(Market.system_name))
              .where(Commodity.symbol.in_(commodities), CommodityPrice.demand > 0,
                     func.coalesce(CommodityPrice.seen_at, Market.updated_at) >= fresh))
        if not include_carrier_markets:
            dq = dq.where(Market.is_carrier.is_(False))
        for sym, demand, sell, station, sid in session.execute(dq):
            if sid in ids:
                demand_rows.setdefault(sid, []).append(
                    {"commodity": sym, "station": station, "demand": demand, "sell_price": sell})
        candidates = [c for c in candidates if c[0].id64 in demand_rows]

    candidates.sort(key=lambda c: (c[1] is None, c[1] if c[1] is not None else 0, -(c[0].population or 0), c[0].name))
    results = []
    for s, d, matched in candidates[:limit]:
        results.append({
            "system": s.name, "distance_ly": round(d, 1) if d is not None else None, "population": s.population,
            "economy": s.economy, "second_economy": s.second_economy, "matched_economy": matched,
            "matching_factions": faction_hits.get(s.id64, []),
            "stations": [_station_dict(m) for m in sorted(markets.get(s.id64, []), key=lambda m: m.station.lower())],
            "demand": sorted(demand_rows.get(s.id64, []), key=lambda r: -(r["demand"] or 0)),
            "factions_age_hours": age_hours(s.factions_updated_at), "info_age_hours": age_hours(s.updated_at),
            "source": s.source})
    return {"criteria": {"economies": sorted(eco_keys), "economy_scope": economy_scope, "include_second": include_second,
                         "faction_state": faction_state, "state_in": state_in, "controlling_only": controlling_only,
                         "min_population": min_population, "demand_commodities": commodities, "ly": ly,
                         "max_age_hours": max_age_hours},
            "origin": near, "count": len(candidates), "shown": len(results), "results": results,
            "note": "snapshot of systems seen by the listener or a live lookup; check the age fields"}
