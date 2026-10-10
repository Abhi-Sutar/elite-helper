"""Read-side logic shared by the CLI and the MCP server. Every function returns plain dicts."""

import math
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import config, database, lock, store
from .models import (Commodity, CommodityPrice, Faction, FactionHistory, Market, PriceHistory, System)


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
    history, observations = [], len(rows)
    for pts in by_faction.values():
        history.extend(_thin(pts, max_points))
    return {"found": True, "system": s.name, "days": days, "observations": observations,
            "returned": len(history), "history": history}


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
    return {"found": True, "system": s.name, "faction": rows[0].faction, "days": days,
            "observations": len(rows), "first_observation": iso(rows[0].ts), "last_observation": iso(rows[-1].ts),
            "note": "intervals start at the first observation inside the window; 'None' main state is omitted",
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
    return {"found": True, "station": m.station, "system": m.system_name, "market_id": m.market_id,
            "station_type": m.station_type, "is_carrier": bool(m.is_carrier),
            "economies": m.economies or [], "prohibited": m.prohibited or [],
            "updated_at": iso(m.updated_at), "age_hours": age_hours(m.updated_at), "source": m.source,
            "commodities": [{"commodity": sym, "buy_price": p.buy_price, "sell_price": p.sell_price,
                             "mean_price": p.mean_price, "stock": p.stock, "demand": p.demand,
                             "stock_bracket": p.stock_bracket, "demand_bracket": p.demand_bracket}
                            for sym, p in prices]}


def _stats(values):
    vals = [v for v in values if v]
    return {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals))} if vals else None


def _bucketed(rows, minutes):
    """
    Group time-ordered history rows into fixed buckets. Prices: min/avg/max over rows where that side was active
    (price > 0). Amounts: min/avg/max over every row, since 0 stock or 0 demand is a real value.
    """
    width = minutes * 60
    out, cur, cur_key = [], [], None

    def mm(values, drop_zero):
        vals = [v for v in values if v is not None and (v or not drop_zero)]
        if not vals:
            return None, None, None
        return min(vals), round(sum(vals) / len(vals)), max(vals)

    def flush():
        if not cur:
            return
        b = {"ts": iso(datetime.fromtimestamp(cur_key * width, timezone.utc)), "updates": len(cur)}
        for name, drop_zero in (("buy_price", True), ("sell_price", True), ("stock", False), ("demand", False)):
            lo, avg, hi = mm([r[name] for r in cur], drop_zero)
            short = name.replace("_price", "")
            b[f"{short}_min"], b[f"{short}_avg"], b[f"{short}_max"] = lo, avg, hi
        b["mean_avg"] = mm([r["mean_price"] for r in cur], True)[1]
        b["stock_bracket"], b["demand_bracket"] = cur[-1]["stock_bracket"], cur[-1]["demand_bracket"]
        out.append(b)
    for r in rows:
        key = int(r["dt"].timestamp()) // width
        if key != cur_key:
            flush()
            cur, cur_key = [], key
        cur.append(r)
    flush()
    return out


def _series_reply(raw, bucket_minutes, max_points):
    """Summary + series for one commodity's time-ordered raw rows."""
    result = {"updates": len(raw), "first_update": iso(raw[0]["dt"]) if raw else None,
              "last_update": iso(raw[-1]["dt"]) if raw else None,
              "buy_price_stats": _stats(r["buy_price"] for r in raw),
              "sell_price_stats": _stats(r["sell_price"] for r in raw),
              "stock_stats": _stats(r["stock"] for r in raw),
              "demand_stats": _stats(r["demand"] for r in raw)}
    if bucket_minutes is None and max_points and len(raw) > max_points:
        span = (raw[-1]["dt"] - raw[0]["dt"]).total_seconds() / 60
        bucket_minutes = max(1, math.ceil(span / max_points))
    if bucket_minutes:
        result["bucket_minutes"] = bucket_minutes
        result["series"] = _bucketed(raw, bucket_minutes)
    else:
        result["series"] = [{"ts": iso(r["dt"]), "buy_price": r["buy_price"], "sell_price": r["sell_price"],
                             "mean_price": r["mean_price"], "stock": r["stock"], "demand": r["demand"],
                             "stock_bracket": r["stock_bracket"], "demand_bracket": r["demand_bracket"]}
                            for r in raw]
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
                  all_commodities=False, max_commodities=25):
    """
    Price and amount time series at one station. `commodity` is one name (single-commodity reply, same shape as
    before), a comma list, or 'all' (-> `series_by_commodity`, busiest commodities first, capped at
    `max_commodities` unless all_commodities=True). Each point has buy/sell price, mean price, stock (supply),
    demand and their brackets. buy_price is what the station sells at (needs stock), sell_price what it pays
    (needs demand); an inactive side reports 0. Long series are summarised into time buckets.
    In full history mode there is a row per market update; in thresholded mode only meaningful changes.
    """
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    wanted = _split_commodities(commodity)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = (select(PriceHistory, Commodity.symbol).join(Commodity, Commodity.id == PriceHistory.commodity_id)
            .where(PriceHistory.market_id == cur["market_id"], PriceHistory.ts >= since))
    truncated, available = False, None
    if wanted is not None:
        base = base.where(Commodity.symbol.in_(wanted))
    else:
        counts = session.execute(
            select(Commodity.symbol, func.count()).select_from(PriceHistory)
            .join(Commodity, Commodity.id == PriceHistory.commodity_id)
            .where(PriceHistory.market_id == cur["market_id"], PriceHistory.ts >= since)
            .group_by(Commodity.symbol).order_by(func.count().desc(), Commodity.symbol)).all()
        available = len(counts)
        chosen = [sym for sym, _ in counts]
        if not all_commodities and len(chosen) > max_commodities:
            chosen, truncated = chosen[:max_commodities], True
        base = base.where(Commodity.symbol.in_(chosen))
    raw_by = {}
    for r, sym in session.execute(base.order_by(Commodity.symbol, PriceHistory.ts)):
        raw_by.setdefault(sym, []).append({
            "dt": r.ts, "buy_price": r.buy_price, "sell_price": r.sell_price, "mean_price": r.mean_price,
            "stock": r.stock, "demand": r.demand, "stock_bracket": r.stock_bracket,
            "demand_bracket": r.demand_bracket})
    head = {"found": True, "station": cur["station"], "system": cur["system"], "days": days}
    if wanted is not None and len(wanted) == 1:
        sym = wanted[0]
        return {**head, "commodity": sym, **_series_reply(raw_by.get(sym, []), bucket_minutes, max_points)}
    series = {sym: _series_reply(rows, bucket_minutes, max_points) for sym, rows in raw_by.items()}
    for sym in wanted or []:
        series.setdefault(sym, _series_reply([], bucket_minutes, max_points))
    reply = {**head, "commodities": sorted(series), "series_by_commodity": series}
    if wanted is None:
        reply.update({"commodities_available": available, "truncated": truncated})
    return reply


def station_activity(session, station, system=None, days=30):
    """How many market updates the database holds for a station, per day (distinct timestamps in price_history)."""
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stamps = session.execute(
        select(PriceHistory.ts).where(PriceHistory.market_id == cur["market_id"], PriceHistory.ts >= since)
        .distinct().order_by(PriceHistory.ts)).scalars().all()
    per_day = {}
    for t in stamps:
        day = t.strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
    mode = store.history_mode(session)
    return {"found": True, "station": cur["station"], "system": cur["system"], "days": days,
            "history_mode": mode,
            "note": ("every accepted market update is stored" if mode == "full" else
                     "thresholded mode: counts are meaningful changes only, not every update"),
            "updates": len(stamps), "first_update": iso(stamps[0]) if stamps else None,
            "last_update": iso(stamps[-1]) if stamps else None,
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
                Market.updated_at, System.x, System.y, System.z)
         .join(Market, Market.market_id == CommodityPrice.market_id)
         .join(Commodity, Commodity.id == CommodityPrice.commodity_id)
         .outerjoin(System, func.lower(System.name) == func.lower(Market.system_name))
         .where(Market.updated_at >= _fresh_cutoff(max_age_hours)))
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
