"""Write side: apply parsed EDDN/EDSM data to the database (dialect-neutral SQLAlchemy Core)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from .database import insert_for
from .models import (Commodity, CommodityPrice, Faction, FactionHistory, Market, Meta, PriceHistory,
                     System)

RETENTION_DAYS = 30
HISTORY_MIN_INTERVAL_S = 3600   # at most one history row per commodity per station per hour
PRICE_CHANGE_PCT = 0.05         # record when buy/sell price moves by >= 5%
AMOUNT_CHANGE_PCT = 0.25        # record when stock/demand moves by >= 25%

_SYSTEM_FIELDS = ("name", "x", "y", "z", "population", "allegiance", "government", "security",
                  "economy", "second_economy", "controlling_faction", "conflicts")


def to_dt(ts):
    """'YYYY-MM-DDTHH:MM:SSZ' -> aware UTC datetime (datetimes pass through)."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def reset_caches(session):
    """Call after a rollback: cached commodity ids may refer to rows that were never committed."""
    session.info.pop("commodity_cache", None)


def commodity_ids(session, symbols):
    cache = session.info.setdefault("commodity_cache", {})
    missing = sorted({s for s in symbols if s not in cache})
    if missing:
        stmt = insert_for(session)(Commodity.__table__).values([{"symbol": s} for s in missing])
        session.execute(stmt.on_conflict_do_nothing(index_elements=["symbol"]))
        for cid, sym in session.execute(select(Commodity.id, Commodity.symbol).where(Commodity.symbol.in_(missing))):
            cache[sym] = cid
    return {s: cache[s] for s in symbols}


# ---- meta ------------------------------------------------------------------------------------

def set_meta(session, key, value):
    stmt = insert_for(session)(Meta.__table__).values(key=key, value=str(value))
    session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value}))


def get_meta(session, key, default=None):
    v = session.execute(select(Meta.value).where(Meta.key == key)).scalar_one_or_none()
    return default if v is None else v


# ---- systems / factions ----------------------------------------------------------------------

def upsert_system(session, s, factions, source="eddn"):
    """
    Apply system info (dict from parsers) and, if `factions` is not None, replace that system's faction
    list. Older data never overwrites newer data. Returns True if anything was written.
    """
    ts = to_dt(s["timestamp"])
    id64 = s["id64"]
    row = session.execute(select(System.updated_at, System.factions_updated_at)
                          .where(System.id64 == id64)).first()
    vals = {k: s[k] for k in _SYSTEM_FIELDS if s.get(k) is not None}
    wrote = False
    fac_ts = None
    if row is None:
        session.execute(insert_for(session)(System.__table__).values(id64=id64, updated_at=ts, source=source, **vals))
        wrote = True
    else:
        fac_ts = row.factions_updated_at
        if ts >= row.updated_at:
            session.execute(update(System.__table__).where(System.id64 == id64).values(updated_at=ts, source=source, **vals))
            wrote = True

    if factions is not None and (fac_ts is None or ts >= fac_ts):
        session.execute(delete(Faction.__table__).where(Faction.system_id64 == id64))
        session.execute(insert_for(session)(Faction.__table__), [{
            "system_id64": id64, "name": f["name"], "government": f.get("government"),
            "allegiance": f.get("allegiance"), "influence": f.get("influence"), "state": f.get("state"),
            "happiness": f.get("happiness"), "active_states": f.get("active_states") or [],
            "pending_states": f.get("pending_states") or [],
            "recovering_states": f.get("recovering_states") or [],
            "is_controlling": bool(f.get("is_controlling")), "updated_at": ts, "source": source,
        } for f in factions])
        for f in factions:
            _record_faction_history(session, id64, f["name"], ts, f.get("influence"), f.get("state"))
        session.execute(update(System.__table__).where(System.id64 == id64).values(factions_updated_at=ts))
        wrote = True
    return wrote


def _record_faction_history(session, id64, faction, ts, influence, state):
    last = session.execute(select(FactionHistory.influence, FactionHistory.state)
                           .where(FactionHistory.system_id64 == id64, FactionHistory.faction == faction)
                           .order_by(FactionHistory.ts.desc()).limit(1)).first()
    if last is not None and last.state == state and last.influence is not None \
            and influence is not None and abs(last.influence - influence) < 0.0005:
        return
    session.execute(insert_for(session)(FactionHistory.__table__).values(
        system_id64=id64, faction=faction, ts=ts, influence=influence, state=state
    ).on_conflict_do_nothing())


# ---- markets / prices ------------------------------------------------------------------------

def upsert_market(session, m, prices, source="eddn"):
    """Replace a market's price list if the message is not older than what we hold."""
    ts = to_dt(m["timestamp"])
    if ts < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS):
        return False  # would be pruned straight away; some clients keep re-uploading very old market data
    mid = m["market_id"]
    existing = session.execute(select(Market.updated_at).where(Market.market_id == mid)).scalar_one_or_none()
    if existing is not None and ts < existing:
        return False
    system_id = session.execute(select(System.id64).where(func.lower(System.name) == m["system_name"].lower())
                                ).scalar_one_or_none()
    stmt = insert_for(session)(Market.__table__).values(
        market_id=mid, station=m["station"], system_name=m["system_name"], system_id64=system_id,
        station_type=m.get("station_type"), is_carrier=bool(m.get("is_carrier")),
        economies=m.get("economies") or [], prohibited=m.get("prohibited") or [],
        updated_at=ts, source=source)
    ex = stmt.excluded
    session.execute(stmt.on_conflict_do_update(index_elements=["market_id"], set_={
        "station": ex.station, "system_name": ex.system_name, "system_id64": ex.system_id64,
        "station_type": func.coalesce(ex.station_type, Market.station_type), "is_carrier": ex.is_carrier,
        "economies": ex.economies, "prohibited": ex.prohibited, "updated_at": ex.updated_at,
        "source": ex.source}))

    ids = commodity_ids(session, [p["commodity"] for p in prices])
    _record_price_history(session, mid, ts, prices, ids)
    session.execute(delete(CommodityPrice.__table__).where(CommodityPrice.market_id == mid))
    session.execute(insert_for(session)(CommodityPrice.__table__).on_conflict_do_nothing(), [{
        "market_id": mid, "commodity_id": ids[p["commodity"]], "buy_price": p.get("buy_price"),
        "sell_price": p.get("sell_price"), "mean_price": p.get("mean_price"), "stock": p.get("stock"),
        "demand": p.get("demand"), "stock_bracket": p.get("stock_bracket"),
        "demand_bracket": p.get("demand_bracket")} for p in prices])
    return True


def _changed(old, new, pct):
    old, new = old or 0, new or 0
    if old == new:
        return False
    if old == 0 or new == 0:
        return True
    return abs(new - old) / old >= pct


def _record_price_history(session, market_id, ts, prices, ids):
    """Append a time-series row per commodity when it is new or has changed meaningfully."""
    latest = (select(PriceHistory.commodity_id, func.max(PriceHistory.ts).label("mts"))
              .where(PriceHistory.market_id == market_id).group_by(PriceHistory.commodity_id).subquery())
    last = {r.commodity_id: r for r in session.execute(
        select(PriceHistory).join(latest, (PriceHistory.commodity_id == latest.c.commodity_id)
                                  & (PriceHistory.ts == latest.c.mts))
        .where(PriceHistory.market_id == market_id)).scalars()}
    rows = []
    for p in prices:
        cid = ids[p["commodity"]]
        prev = last.get(cid)
        if prev is not None:
            if (ts - prev.ts).total_seconds() < HISTORY_MIN_INTERVAL_S:
                continue
            if not (_changed(prev.buy_price, p.get("buy_price"), PRICE_CHANGE_PCT)
                    or _changed(prev.sell_price, p.get("sell_price"), PRICE_CHANGE_PCT)
                    or _changed(prev.stock, p.get("stock"), AMOUNT_CHANGE_PCT)
                    or _changed(prev.demand, p.get("demand"), AMOUNT_CHANGE_PCT)):
                continue
        rows.append({"market_id": market_id, "commodity_id": cid, "ts": ts, "buy_price": p.get("buy_price"),
                     "sell_price": p.get("sell_price"), "stock": p.get("stock"), "demand": p.get("demand")})
    if rows:
        session.execute(insert_for(session)(PriceHistory.__table__).on_conflict_do_nothing(), rows)


# ---- maintenance -----------------------------------------------------------------------------

def prune(session, days=RETENTION_DAYS):
    """Delete commodity data older than `days`: history rows, and markets (with prices) not updated since."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stale = select(Market.market_id).where(Market.updated_at < cutoff)
    out = {}
    out["price_history"] = session.execute(delete(PriceHistory.__table__).where(
        (PriceHistory.ts < cutoff) | PriceHistory.market_id.in_(stale))).rowcount
    out["commodity_prices"] = session.execute(delete(CommodityPrice.__table__).where(
        CommodityPrice.market_id.in_(stale))).rowcount
    out["markets"] = session.execute(delete(Market.__table__).where(Market.updated_at < cutoff)).rowcount
    session.commit()
    return out


def link_markets_to_systems(session):
    """Resolve markets.system_id64 for markets whose system became known after the market message."""
    sub = select(System.id64).where(func.lower(System.name) == func.lower(Market.system_name)).scalar_subquery()
    n = session.execute(update(Market.__table__).where(Market.system_id64.is_(None)).values(system_id64=sub)).rowcount
    session.commit()
    return n


def apply_parsed(session, parsed, source="eddn"):
    """Write one parsers.parse_message() result. Returns True if the DB changed."""
    if parsed is None:
        return False
    if parsed["kind"] == "market":
        return upsert_market(session, parsed["market"], parsed["prices"], source)
    if parsed["kind"] == "system":
        return upsert_system(session, parsed["system"], parsed["factions"], source)
    return False
