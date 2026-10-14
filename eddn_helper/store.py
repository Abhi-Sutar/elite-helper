"""Write side: apply parsed EDDN/EDSM data to the database (dialect-neutral SQLAlchemy Core)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, null, select, text, update

from .database import insert_for
from .models import (Commodity, CommodityPrice, Faction, FactionHistory, FactionInfo, Market, MarketUpdate, Meta,
                     PriceHistory, Station, StationSync, System, SystemUpdate)

RETENTION_DAYS = 30             # commodity price history and per-commodity snapshot rows (a market's last report is kept)
EVENT_RETENTION_DAYS = 365      # market_update / system_update (one small row per EDDN message)
HISTORY_MIN_INTERVAL_S = 3600   # thresholded mode: at most one history row per commodity per station per hour
PRICE_CHANGE_PCT = 0.05         # thresholded mode: record when buy/sell price moves by >= 5%
AMOUNT_CHANGE_PCT = 0.25        # thresholded mode: record when stock/demand moves by >= 25%
INFLUENCE_EPSILON = 0.0005      # influence moves smaller than this (0.05 percentage points) are not changes
ANCHOR_HOURS_ENV = "ELITE_HELPER_ANCHOR_HOURS"
DEFAULT_ANCHOR_HOURS = 24

HISTORY_MODE_ENV = "ELITE_HELPER_HISTORY_MODE"
HISTORY_MODES = ("changes", "full", "thresholded")
PRICE_FIELDS = ("buy_price", "sell_price", "mean_price", "stock", "demand", "stock_bracket", "demand_bracket")


def history_mode(bind_or_session=None):
    """
    How price_history / faction_history are written (the market_update / system_update event logs are always
    written):
      changes      - a row only when a value differs from the last logged one (plus new items and a daily
                     anchor row). Lossless and small. The default.
      full         - a row for every reading (~40M rows/day at current EDDN volume; PostgreSQL/Timescale only).
      thresholded  - legacy: only meaningful changes (5% price, 25% amount), at most hourly.
    Env ELITE_HELPER_HISTORY_MODE overrides.
    """
    import os
    mode = (os.environ.get(HISTORY_MODE_ENV) or "").strip().lower()
    if mode:
        if mode not in HISTORY_MODES:
            raise ValueError(f"{HISTORY_MODE_ENV} must be one of {HISTORY_MODES}, got {mode!r}")
        return mode
    return "changes"


def anchor_interval():
    """How long an unchanged item may go without a history row before an 'anchor' row is written (0 = never)."""
    import os
    try:
        hours = float(os.environ.get(ANCHOR_HOURS_ENV, DEFAULT_ANCHOR_HOURS))
    except ValueError:
        hours = DEFAULT_ANCHOR_HOURS
    return timedelta(hours=hours) if hours > 0 else None


def _anchor_due(logged_at, ts, interval):
    if logged_at is None:
        return True
    return interval is not None and (ts - logged_at) >= interval


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

def upsert_faction_info(session, players, checked_at=None, source="edsm"):
    """
    Record whether factions are player factions: `players` is {faction name: bool}. Names match case-insensitively,
    and the stored spelling is kept. Returns the number of factions written.
    """
    players = {n[:120]: bool(p) for n, p in players.items() if n and p is not None}
    if not players:
        return 0
    checked_at = checked_at or datetime.now(timezone.utc)
    existing = {n.lower(): n for (n,) in session.execute(
        select(FactionInfo.name).where(func.lower(FactionInfo.name).in_([n.lower() for n in players])))}
    for name, is_player in players.items():
        stored = existing.get(name.lower())
        if stored is None:
            session.execute(insert_for(session)(FactionInfo.__table__).values(
                name=name, is_player=is_player, checked_at=checked_at, source=source))
        else:
            session.execute(update(FactionInfo.__table__).where(FactionInfo.name == stored).values(
                is_player=is_player, checked_at=checked_at, source=source))
    return len(players)


def replace_stations(session, system_id64, stations, fetched_at=None, source="edsm"):
    """
    Replace the stored stations of one system with `stations` (dicts from external._parse_station; fleet carriers are
    skipped) and record the sync, so a system with no stations is still known to have been fetched. Returns the count.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    rows = {}
    for st in stations:
        if st.get("is_carrier") or not st.get("station"):
            continue
        rows[st["station"]] = {
            "system_id64": system_id64, "name": st["station"][:120], "market_id": st.get("market_id"),
            "station_type": (st.get("station_type") or None) and st["station_type"][:40],
            "body": st.get("body"), "distance_to_arrival_ls": st.get("distance_to_arrival_ls"),
            "economy": st.get("economy"), "second_economy": st.get("second_economy"),
            "allegiance": st.get("allegiance"), "government": st.get("government"),
            "controlling_faction": st.get("controlling_faction"), "has_market": bool(st.get("has_market")),
            "has_shipyard": bool(st.get("has_shipyard")), "has_outfitting": bool(st.get("has_outfitting")),
            "services": st.get("services") or [],
            "info_updated_at": to_dt(st["info_updated_at"]) if st.get("info_updated_at") else None,
            "fetched_at": fetched_at, "source": source}
    session.execute(delete(Station.__table__).where(Station.system_id64 == system_id64))
    if rows:
        session.execute(insert_for(session)(Station.__table__), list(rows.values()))
    sync = insert_for(session)(StationSync.__table__).values(
        system_id64=system_id64, fetched_at=fetched_at, station_count=len(rows), source=source)
    session.execute(sync.on_conflict_do_update(index_elements=["system_id64"], set_={
        "fetched_at": sync.excluded.fetched_at, "station_count": sync.excluded.station_count,
        "source": sync.excluded.source}))
    return len(rows)


def set_meta(session, key, value):
    stmt = insert_for(session)(Meta.__table__).values(key=key, value=str(value))
    session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value}))


def get_meta(session, key, default=None):
    v = session.execute(select(Meta.value).where(Meta.key == key)).scalar_one_or_none()
    return default if v is None else v


# ---- systems / factions ----------------------------------------------------------------------

def _tracked_equal(prev, f):
    """True if the stored faction row `prev` and the incoming faction dict `f` agree on everything tracked."""
    new_infl = f.get("influence")
    if prev.influence is None or new_infl is None:
        infl_same = prev.influence is None and new_infl is None
    else:
        infl_same = abs(prev.influence - new_infl) < INFLUENCE_EPSILON
    return (infl_same and prev.state == f.get("state") and prev.happiness == f.get("happiness")
            and _state_key(prev.active_states) == _state_key(f.get("active_states"))
            and _state_key(prev.pending_states) == _state_key(f.get("pending_states"))
            and _state_key(prev.recovering_states) == _state_key(f.get("recovering_states")))


def upsert_system(session, s, factions, source="eddn", uploader="", software=""):
    """
    Apply system info (dict from parsers) and, if `factions` is not None, replace that system's faction
    list. Older data never overwrites newer data. Returns True if anything was written.

    Every accepted message with factions is recorded in system_update (traffic). Faction history depends on
    the mode: 'changes' logs a row only when something tracked changed (new faction, influence/state/happiness/
    state lists, or an anchor when nothing was logged for a day); 'full' logs every observation; 'thresholded'
    logs when something tracked changed since the last row. A message older than the stored snapshot never
    touches the snapshot (in 'full' mode it is still logged). A faction that disappeared gets a present=False
    marker row.
    """
    ts = to_dt(s["timestamp"])
    id64 = s["id64"]
    mode = history_mode(session)
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

    if factions is None:
        return wrote
    if fac_ts is not None and ts < fac_ts:  # late upload: snapshot stays as it is
        n = 0
        if mode == "full":
            _insert_faction_history(session, id64, ts, factions)
            n = len(factions)
        _log_system_event(session, id64, ts, uploader, software, len(factions), n)
        return True

    prev = {r.name: r for r in session.execute(select(Faction.__table__).where(Faction.system_id64 == id64))}
    anchor = anchor_interval()
    written = []  # factions that got a history row from this message
    if mode == "full":
        written = list(factions)
        _insert_faction_history(session, id64, ts, factions)
    elif mode == "thresholded":
        for f in factions:
            if _record_faction_history(session, id64, ts, f):
                written.append(f)
    else:  # changes
        for f in factions:
            p = prev.get(f["name"])
            if p is None or not _tracked_equal(p, f) or _anchor_due(p.logged_at, ts, anchor):
                written.append(f)
        if written:
            _insert_faction_history(session, id64, ts, written)
    written_names = {f["name"] for f in written}

    session.execute(delete(Faction.__table__).where(Faction.system_id64 == id64))
    session.execute(insert_for(session)(Faction.__table__), [{
        "system_id64": id64, "name": f["name"], "government": f.get("government"),
        "allegiance": f.get("allegiance"), "influence": f.get("influence"), "state": f.get("state"),
        "happiness": f.get("happiness"), "active_states": f.get("active_states") or [],
        "pending_states": f.get("pending_states") or [],
        "recovering_states": f.get("recovering_states") or [],
        "is_controlling": bool(f.get("is_controlling")), "updated_at": ts, "source": source,
        "logged_at": ts if f["name"] in written_names else (prev[f["name"]].logged_at if f["name"] in prev else None),
    } for f in factions])

    gone = sorted(set(prev) - {f["name"] for f in factions})
    if gone:  # the faction left (or was retreated from) this system
        session.execute(insert_for(session)(FactionHistory.__table__).on_conflict_do_nothing(), [
            {"system_id64": id64, "faction": name, "ts": ts, "influence": None, "state": None, "happiness": None,
             "active_states": [], "pending_states": [], "recovering_states": [], "present": False}
            for name in gone])
    _log_system_event(session, id64, ts, uploader, software, len(factions), len(written) + len(gone))
    session.execute(update(System.__table__).where(System.id64 == id64).values(factions_updated_at=ts))
    return True


def _log_system_event(session, id64, ts, uploader, software, factions, changed):
    session.execute(insert_for(session)(SystemUpdate.__table__).on_conflict_do_nothing(), [{
        "system_id64": id64, "ts": ts, "uploader": uploader or "", "software": software or "",
        "factions": factions, "changed": changed}])


def _history_row(id64, ts, f):
    return {"system_id64": id64, "faction": f["name"], "ts": ts, "influence": f.get("influence"),
            "state": f.get("state"), "happiness": f.get("happiness"),
            "active_states": f.get("active_states") or [], "pending_states": f.get("pending_states") or [],
            "recovering_states": f.get("recovering_states") or [], "present": True}


def _insert_faction_history(session, id64, ts, factions):
    """One row per faction for this observation (same-timestamp duplicates are ignored)."""
    session.execute(insert_for(session)(FactionHistory.__table__).on_conflict_do_nothing(),
                    [_history_row(id64, ts, f) for f in factions])


def _state_key(items):
    """Order-insensitive comparison key for a state list in either the object or legacy string format."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            out.append((it.get("state"), it.get("trend")))
        else:
            out.append((it, None))
    return tuple(sorted(out, key=lambda x: (str(x[0]), str(x[1]))))


def _record_faction_history(session, id64, ts, f):
    """Thresholded mode: write a row only if something tracked changed since the last row. Returns True if written."""
    last = session.execute(select(FactionHistory).where(FactionHistory.system_id64 == id64,
                                                        FactionHistory.faction == f["name"])
                           .order_by(FactionHistory.ts.desc()).limit(1)).scalar_one_or_none()
    new = _history_row(id64, ts, f)
    if last is not None and last.present and _tracked_equal(last, f):
        return False
    session.execute(insert_for(session)(FactionHistory.__table__).on_conflict_do_nothing(), [new])
    return True


# ---- markets / prices ------------------------------------------------------------------------

def upsert_market(session, m, prices, source="eddn", uploader="", software=""):
    """
    Apply one market message. Every accepted message is recorded in market_update (traffic). The current snapshot
    is replaced unless the message is older than the one we hold (a late upload). What goes into price_history
    depends on the mode - see history_mode(). In 'changes' mode a late upload only produces the traffic event
    (an interim state between two readings can be missed).
    """
    ts = to_dt(m["timestamp"])
    mid = m["market_id"]
    mode = history_mode(session)
    existing = session.execute(select(Market.updated_at).where(Market.market_id == mid)).scalar_one_or_none()
    if ts < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS) and existing is not None and ts < existing:
        return False  # old and not the market's latest report: nothing to keep (some clients re-upload old data)
    if existing is not None and ts < existing:
        n = 0
        if mode == "full":
            ids = commodity_ids(session, [p["commodity"] for p in prices])
            _insert_history(session, mid, ts, prices, ids)
            n = len(prices)
        _log_market_event(session, mid, ts, uploader, software, len(prices), n)
        return True
    system_id = session.execute(select(System.id64).where(func.lower(System.name) == m["system_name"].lower())
                                ).scalar_one_or_none()
    stmt = insert_for(session)(Market.__table__).values(
        market_id=mid, station=m["station"], system_name=m["system_name"], system_id64=system_id,
        station_type=m.get("station_type"), is_carrier=bool(m.get("is_carrier")),
        economies=m.get("economies") or [], economy_shares=m.get("economy_shares") or null(),
        prohibited=m.get("prohibited") or [], updated_at=ts, source=source)
    ex = stmt.excluded
    session.execute(stmt.on_conflict_do_update(index_elements=["market_id"], set_={
        "station": ex.station, "system_name": ex.system_name, "system_id64": ex.system_id64,
        "station_type": func.coalesce(ex.station_type, Market.station_type), "is_carrier": ex.is_carrier,
        "economies": ex.economies, "economy_shares": func.coalesce(ex.economy_shares, Market.economy_shares),
        "prohibited": ex.prohibited, "updated_at": ex.updated_at,
        "source": ex.source}))

    ids = commodity_ids(session, [p["commodity"] for p in prices])
    prev = {r.commodity_id: r for r in session.execute(
        select(CommodityPrice.__table__).where(CommodityPrice.market_id == mid))}
    anchor = anchor_interval()
    if mode == "full":
        written = {ids[p["commodity"]] for p in prices}
        _insert_history(session, mid, ts, prices, ids)
    elif mode == "thresholded":
        written = _record_price_history(session, mid, ts, prices, ids)
    else:  # changes: only new items, differences from the last logged reading, or a due anchor
        due = []
        for p in prices:
            cid = ids[p["commodity"]]
            old = prev.get(cid)
            if old is None or any(getattr(old, f) != p.get(f) for f in PRICE_FIELDS) \
                    or _anchor_due(old.logged_at, ts, anchor):
                due.append(p)
        written = {ids[p["commodity"]] for p in due}
        _insert_history(session, mid, ts, due, ids)

    # Current snapshot: upsert in place; logged_at only moves when a history row was written, seen_at always moves.
    # Commodities missing from this message are left alone: EDDN reports can be partial (a station is reported with
    # 368 items by most uploaders and with 4 by another), so absence proves nothing. Readers use seen_at.
    snap = insert_for(session)(CommodityPrice.__table__)
    ex = snap.excluded
    session.execute(snap.on_conflict_do_update(
        index_elements=["market_id", "commodity_id"],
        set_={**{c: getattr(ex, c) for c in PRICE_FIELDS}, "seen_at": ex.seen_at,
              "logged_at": func.coalesce(ex.logged_at, CommodityPrice.logged_at)}), [{
        "market_id": mid, "commodity_id": ids[p["commodity"]], "buy_price": p.get("buy_price"),
        "sell_price": p.get("sell_price"), "mean_price": p.get("mean_price"), "stock": p.get("stock"),
        "demand": p.get("demand"), "stock_bracket": p.get("stock_bracket"),
        "demand_bracket": p.get("demand_bracket"),
        "seen_at": ts, "logged_at": ts if ids[p["commodity"]] in written else None} for p in prices])
    _log_market_event(session, mid, ts, uploader, software, len(prices), len(written))
    return True


def _log_market_event(session, market_id, ts, uploader, software, commodities, changed):
    session.execute(insert_for(session)(MarketUpdate.__table__).on_conflict_do_nothing(), [{
        "market_id": market_id, "ts": ts, "uploader": uploader or "", "software": software or "",
        "commodities": commodities, "changed": changed}])


def _price_row(market_id, commodity_id, ts, p):
    return {"market_id": market_id, "commodity_id": commodity_id, "ts": ts, "buy_price": p.get("buy_price"),
            "sell_price": p.get("sell_price"), "stock": p.get("stock"), "demand": p.get("demand"),
            "mean_price": p.get("mean_price"), "stock_bracket": p.get("stock_bracket"),
            "demand_bracket": p.get("demand_bracket")}


def _insert_history(session, market_id, ts, prices, ids):
    """One time-series row per commodity given (same-timestamp duplicates are ignored)."""
    rows = [_price_row(market_id, ids[p["commodity"]], ts, p) for p in prices]
    if rows:
        session.execute(insert_for(session)(PriceHistory.__table__).on_conflict_do_nothing(), rows)


def _changed(old, new, pct):
    old, new = old or 0, new or 0
    if old == new:
        return False
    if old == 0 or new == 0:
        return True
    return abs(new - old) / old >= pct


def _record_price_history(session, market_id, ts, prices, ids):
    """Thresholded mode: append a row per commodity when new or meaningfully changed. Returns the commodity ids written."""
    latest = (select(PriceHistory.commodity_id, func.max(PriceHistory.ts).label("mts"))
              .where(PriceHistory.market_id == market_id).group_by(PriceHistory.commodity_id).subquery())
    last = {r.commodity_id: r for r in session.execute(
        select(PriceHistory).join(latest, (PriceHistory.commodity_id == latest.c.commodity_id)
                                  & (PriceHistory.ts == latest.c.mts))
        .where(PriceHistory.market_id == market_id)).scalars()}
    rows, written = [], set()
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
        rows.append(_price_row(market_id, cid, ts, p))
        written.add(cid)
    if rows:
        session.execute(insert_for(session)(PriceHistory.__table__).on_conflict_do_nothing(), rows)
    return written


# ---- maintenance -----------------------------------------------------------------------------

def history_is_hypertable(session, table="price_history"):
    """True when the history table is a TimescaleDB hypertable, whose retention policy drops old chunks itself."""
    if session.get_bind().dialect.name != "postgresql":
        return False
    if session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is None:
        return False
    return session.execute(text(
        "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :t"
    ), {"t": table}).first() is not None


def prune(session, days=RETENTION_DAYS):
    """
    Delete commodity data older than `days`: price history rows and per-commodity snapshot rows. Markets are kept
    for ever, and so is each market's last report (the snapshot rows seen at markets.updated_at), however old.
    Systems, factions, faction history and the name tables are never pruned; the update logs expire after a year
    except the newest row of each market / system.
    On a TimescaleDB hypertable the retention policy drops expired price history chunks, so that is left to it.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = {}
    if history_is_hypertable(session, "price_history"):
        out["price_history"] = 0
    else:
        out["price_history"] = session.execute(delete(PriceHistory.__table__).where(
            PriceHistory.ts < cutoff)).rowcount
    event_cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
    for model, key in ((MarketUpdate, "market_id"), (SystemUpdate, "system_id64")):
        t = model.__table__
        other = t.alias()
        newest = select(func.max(other.c.ts)).where(other.c[key] == t.c[key]).scalar_subquery()
        out[t.name] = session.execute(delete(t).where((t.c.ts < event_cutoff) & (t.c.ts < newest))).rowcount
    latest = select(Market.updated_at).where(Market.market_id == CommodityPrice.market_id).scalar_subquery()
    out["commodity_prices"] = session.execute(delete(CommodityPrice.__table__).where(
        (CommodityPrice.seen_at < cutoff) & (CommodityPrice.seen_at < latest))).rowcount
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
    who = {"uploader": parsed.get("uploader", ""), "software": parsed.get("software", "")}
    if parsed["kind"] == "market":
        return upsert_market(session, parsed["market"], parsed["prices"], source, **who)
    if parsed["kind"] == "system":
        return upsert_system(session, parsed["system"], parsed["factions"], source, **who)
    return False
