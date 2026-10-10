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


def _faction_dict(f):
    return {
        "name": f.name, "influence_pct": _pct(f.influence), "state": f.state, "government": f.government,
        "allegiance": f.allegiance, "happiness": f.happiness,
        "active_states": f.active_states or [], "pending_states": f.pending_states or [],
        "recovering_states": f.recovering_states or [],
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


def faction_history(session, system, days=30, faction=None):
    s = find_system(session, system)
    if s is None:
        return {"found": False, "system": system}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(FactionHistory).where(FactionHistory.system_id64 == s.id64, FactionHistory.ts >= since)
    if faction:
        q = q.where(func.lower(FactionHistory.faction) == faction.lower())
    rows = session.execute(q.order_by(FactionHistory.faction, FactionHistory.ts)).scalars().all()
    return {"found": True, "system": s.name, "days": days,
            "history": [{"faction": r.faction, "ts": iso(r.ts), "influence_pct": _pct(r.influence),
                         "state": r.state} for r in rows]}


def faction_presence(session, name):
    base = (select(Faction, System.name.label("system_name"), System.factions_updated_at.label("fac_ts"))
            .join(System, System.id64 == Faction.system_id64))
    rows = session.execute(base.where(func.lower(Faction.name) == name.lower())
                           .order_by(Faction.influence.desc())).all()
    if not rows:  # fall back to substring match
        rows = session.execute(base.where(Faction.name.ilike(f"%{name}%"))
                               .order_by(Faction.influence.desc())).all()
    return {"faction": name, "found": bool(rows), "systems": [
        {"system": r.system_name, "faction": r.Faction.name, "influence_pct": _pct(r.Faction.influence),
         "state": r.Faction.state, "is_controlling": bool(r.Faction.is_controlling),
         "age_hours": age_hours(r.fac_ts)} for r in rows]}


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


def price_history(session, station, commodity, system=None, days=30):
    """Time series of one commodity at one station (rows are recorded on meaningful change)."""
    cur = station_market(session, station, system)
    if not cur["found"] or cur.get("ambiguous"):
        return cur
    sym = norm_commodity(commodity)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(PriceHistory).join(Commodity, Commodity.id == PriceHistory.commodity_id)
        .where(PriceHistory.market_id == cur["market_id"], Commodity.symbol == sym, PriceHistory.ts >= since)
        .order_by(PriceHistory.ts)).scalars().all()
    series = [{"ts": iso(r.ts), "buy_price": r.buy_price, "sell_price": r.sell_price,
               "stock": r.stock, "demand": r.demand} for r in rows]

    def stats(key):
        vals = [r[key] for r in series if r[key]]
        return {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals))} if vals else None
    return {"found": True, "station": cur["station"], "system": cur["system"], "commodity": sym,
            "days": days, "points": len(series), "buy_price_stats": stats("buy_price"),
            "sell_price_stats": stats("sell_price"), "series": series}


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
    running, pid = lock.holder(config.runtime_dir(url) / "listener.lock")
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
