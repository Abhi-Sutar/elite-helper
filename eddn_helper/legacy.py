"""One-off import from the pre-SQLAlchemy SQLite file (string timestamps, per-row commodity symbols)."""

import json
import sqlite3
from pathlib import Path

from . import config, store
from .database import insert_for
from .models import CommodityPrice, Faction, FactionHistory, Market, PriceHistory, System

BATCH = 5000


def default_legacy_path():
    return config.data_dir() / "eddn.sqlite"


def _json(v, default):
    if v is None:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _batches(cur):
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            return
        yield rows


def _keep(rows, key, valid, skipped, table):
    kept = [r for r in rows if r[key] in valid]
    if len(kept) != len(rows):
        skipped[table] = skipped.get(table, 0) + len(rows) - len(kept)
    return kept


def _tables(src):
    return {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def import_legacy(session, old_path):
    """Copy all rows from an old-format database into the current schema. Returns per-table counts."""
    old_path = Path(old_path)
    if not old_path.exists():
        raise FileNotFoundError(old_path)
    src = sqlite3.connect(f"file:{old_path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    have = _tables(src)
    counts = {}
    skipped = {}
    ins = insert_for(session)
    # The old schema had no foreign keys, so child rows can be orphaned (e.g. history kept after its market
    # was pruned). Those can't be stored under the new constraints and are skipped, not guessed at.
    system_ids = {r[0] for r in src.execute("SELECT id64 FROM systems")} if "systems" in have else set()
    market_ids = {r[0] for r in src.execute("SELECT market_id FROM markets")} if "markets" in have else set()
    try:
        if "systems" in have:
            n = 0
            for rows in _batches(src.execute("SELECT * FROM systems")):
                session.execute(ins(System.__table__).on_conflict_do_nothing(), [{
                    "id64": r["id64"], "name": r["name"], "x": r["x"], "y": r["y"], "z": r["z"],
                    "population": r["population"], "allegiance": r["allegiance"], "government": r["government"],
                    "security": r["security"], "economy": r["economy"], "second_economy": r["second_economy"],
                    "controlling_faction": r["controlling_faction"], "conflicts": _json(r["conflicts"], None),
                    "updated_at": store.to_dt(r["updated_at"]),
                    "factions_updated_at": store.to_dt(r["factions_updated_at"]) if r["factions_updated_at"] else None,
                    "source": r["source"] or "eddn"} for r in rows])
                n += len(rows)
            counts["systems"] = n
        if "factions" in have:
            n = 0
            for rows in _batches(src.execute("SELECT * FROM factions")):
                rows = _keep(rows, "system_id64", system_ids, skipped, "factions")
                session.execute(ins(Faction.__table__).on_conflict_do_nothing(), [{
                    "system_id64": r["system_id64"], "name": r["name"], "government": r["government"],
                    "allegiance": r["allegiance"], "influence": r["influence"], "state": r["state"],
                    "happiness": r["happiness"], "active_states": _json(r["active_states"], []),
                    "pending_states": _json(r["pending_states"], []),
                    "recovering_states": _json(r["recovering_states"], []),
                    "is_controlling": bool(r["is_controlling"]), "updated_at": store.to_dt(r["updated_at"]),
                    "source": r["source"] or "eddn"} for r in rows])
                n += len(rows)
            counts["factions"] = n
        if "faction_history" in have:
            n = 0
            for rows in _batches(src.execute("SELECT * FROM faction_history")):
                rows = _keep(rows, "system_id64", system_ids, skipped, "faction_history")
                session.execute(ins(FactionHistory.__table__).on_conflict_do_nothing(), [{
                    "system_id64": r["system_id64"], "faction": r["faction"], "ts": store.to_dt(r["ts"]),
                    "influence": r["influence"], "state": r["state"]} for r in rows])
                n += len(rows)
            counts["faction_history"] = n
        if "markets" in have:
            n = 0
            for rows in _batches(src.execute("SELECT * FROM markets")):
                session.execute(ins(Market.__table__).on_conflict_do_nothing(), [{
                    "market_id": r["market_id"], "station": r["station"], "system_name": r["system_name"],
                    "station_type": r["station_type"], "is_carrier": bool(r["is_carrier"]),
                    "economies": _json(r["economies"], []), "prohibited": _json(r["prohibited"], []),
                    "updated_at": store.to_dt(r["updated_at"]), "source": r["source"] or "eddn"} for r in rows])
                n += len(rows)
            counts["markets"] = n
        for table, model, ts_col in (("commodity_prices", CommodityPrice, None), ("price_history", PriceHistory, "ts")):
            if table not in have:
                continue
            n = 0
            for rows in _batches(src.execute(f"SELECT * FROM {table}")):
                rows = _keep(rows, "market_id", market_ids, skipped, table)
                if not rows:
                    continue
                ids = store.commodity_ids(session, {r["commodity"] for r in rows})
                if table == "commodity_prices":
                    payload = [{"market_id": r["market_id"], "commodity_id": ids[r["commodity"]],
                                "buy_price": r["buy_price"], "sell_price": r["sell_price"],
                                "mean_price": r["mean_price"], "stock": r["stock"], "demand": r["demand"],
                                "stock_bracket": r["stock_bracket"], "demand_bracket": r["demand_bracket"]}
                               for r in rows]
                else:
                    payload = [{"market_id": r["market_id"], "commodity_id": ids[r["commodity"]],
                                "ts": store.to_dt(r["ts"]), "buy_price": r["buy_price"],
                                "sell_price": r["sell_price"], "stock": r["stock"], "demand": r["demand"]}
                               for r in rows]
                session.execute(ins(model.__table__).on_conflict_do_nothing(), payload)
                n += len(rows)
            counts[table] = n
        session.commit()
        store.link_markets_to_systems(session)
        if skipped:
            counts["skipped_orphans"] = skipped
    finally:
        src.close()
    return counts
