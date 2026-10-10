"""Command-line interface: python -m eddn_helper <command> [--json]."""

import argparse
import csv
import json
import sys

from . import config, database, legacy, live, migrate, queries, store


def fmt_cr(v):
    return "-" if v is None else f"{v:,.0f} Cr"


def fmt_age(h):
    if h is None:
        return "?"
    return f"{h * 60:.0f}m" if h < 1 else (f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d")


def table(rows, cols):
    """cols: list of (header, key_or_callable). Prints a left-aligned text table."""
    cells = [[h for h, _ in cols]]
    for r in rows:
        cells.append([str(k(r) if callable(k) else r.get(k, "")) for _, k in cols])
    widths = [max(len(row[i]) for row in cells) for i in range(len(cols))]
    for n, row in enumerate(cells):
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
        if n == 0:
            print("  ".join("-" * w for w in widths))


def emit(args, data, human, csv_rows=None):
    """Print as JSON (--json), CSV (--csv, if the command supports it) or human-readable text."""
    if getattr(args, "csv", False):
        if csv_rows is None:
            print("--csv is not available for this command", file=sys.stderr)
            return
        columns, rows = csv_rows(data)
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([[("" if r.get(c) is None else r.get(c)) for c in columns] for r in rows])
    elif args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        human(data)


PRICE_RAW = ["ts", "commodity", "buy_price", "sell_price", "mean_price", "stock", "demand",
             "stock_bracket", "demand_bracket"]
PRICE_BUCKET = ["ts", "commodity", "updates", "buy_min", "buy_avg", "buy_max", "sell_min", "sell_avg", "sell_max",
                "stock_min", "stock_avg", "stock_max", "demand_min", "demand_avg", "demand_max", "mean_avg",
                "stock_bracket", "demand_bracket"]


def price_csv(d):
    """Flatten a (single or multi commodity) price_history reply. An inactive side (price 0) is left empty."""
    if not d.get("found") or d.get("ambiguous"):
        return PRICE_RAW, []
    blocks = d["series_by_commodity"] if "series_by_commodity" in d else {d["commodity"]: d}
    bucketed = any("bucket_minutes" in b for b in blocks.values())
    rows = []
    for sym, block in blocks.items():
        for pt in block["series"]:
            row = dict(pt, commodity=sym)
            for key in ("buy_price", "sell_price", "mean_price"):
                if row.get(key) == 0:
                    row[key] = None
            rows.append(row)
    return (PRICE_BUCKET if bucketed else PRICE_RAW), rows


def _states_text(items, with_trend=False):
    out = []
    for it in items or []:
        if isinstance(it, dict):
            out.append(f"{it['state']}:{it['trend']}" if with_trend and it.get("trend") is not None else it["state"])
        else:
            out.append(str(it))
    return "|".join(out)


FACTION_COLUMNS = ["ts", "faction", "present", "influence_pct", "state", "happiness", "active_states",
                   "pending_states", "recovering_states"]


def faction_csv(d):
    rows = []
    if d.get("history"):
        hist = d["history"]["history"] if isinstance(d["history"], dict) else d["history"]
        for h in hist:
            rows.append(dict(h, active_states=_states_text(h["active_states"]),
                             pending_states=_states_text(h["pending_states"], True),
                             recovering_states=_states_text(h["recovering_states"], True)))
    return FACTION_COLUMNS, rows


TIMELINE_COLUMNS = ["field", "value", "trend", "start", "last_seen", "ended_at"]


def not_found(what):
    print(f"{what}: not found in the local database. Is the listener running "
          f"(`python -m eddn_helper listen`)? Try --live for an EDSM lookup.", file=sys.stderr)


# ---- command handlers ------------------------------------------------------

def cmd_listen(args, _):
    from . import listener
    return listener.run(args.db, log_file=args.log_file)


def cmd_status(args, _):
    engine = database.make_engine(args.db)
    st = migrate.status(engine)
    if st["state"] != "ok":
        data = {"database": database.masked_url(args.db), "schema_state": st["state"],
                "schema_current": st["current"], "schema_head": st["head"],
                "hint": "run `python -m eddn_helper migrate upgrade` (or start the listener)"}
    else:
        with database.session_scope(database.make_session_factory(engine)) as session:
            data = queries.data_status(session)
    engine.dispose()

    def human(d):
        for k, v in d.items():
            print(f"{k}: {v}")
    emit(args, data, human)


def cmd_migrate(args, _):
    engine = database.make_engine(args.db)
    if args.mcmd == "status":
        data = migrate.status(engine)
        emit(args, data, lambda d: print(f"current={d['current']} head={d['head']} state={d['state']}"))
    elif args.mcmd == "upgrade":
        data = migrate.upgrade(engine, args.rev, backup=not args.no_backup)
        emit(args, data, lambda d: print(f"schema now at {d['current']} (head {d['head']})"))
    elif args.mcmd == "new":
        from alembic import command
        command.revision(migrate.alembic_config(engine), message=args.message, autogenerate=True)
    engine.dispose()


def cmd_backup(args, _):
    engine = database.make_engine(args.db)
    dest = migrate.backup_database(engine, args.path)
    engine.dispose()
    print(f"backup written to {dest}")


def cmd_import_legacy(args, _):
    engine = database.make_engine(args.db)
    migrate.upgrade(engine)
    with database.session_scope(database.make_session_factory(engine)) as session:
        counts = legacy.import_legacy(session, args.source or legacy.default_legacy_path())
    engine.dispose()
    emit(args, counts, lambda d: print("imported: " + ", ".join(f"{k}={v}" for k, v in d.items())))


def cmd_system(args, session):
    res = live.system_info(session, args.name, args.live, args.max_age_hours)

    def human(d):
        if not d["found"]:
            return not_found(f"System '{d['system']}'")
        print(f"{d['system']}  pop={d['population']:,}  {d['allegiance']} / {d['government']}  "
              f"security={d['security']}  economy={d['economy']}/{d['second_economy']}")
        print(f"controlling faction: {d['controlling_faction']}   "
              f"faction data age: {fmt_age(d['factions_age_hours'])} (source: {d['source']})")
        c = d["coords"]
        if c["x"] is not None:
            print(f"coords: {c['x']}, {c['y']}, {c['z']}")
        print_factions(d["factions"])
        for c in d["conflicts"]:
            print(f"CONFLICT ({c['type']}, {c['status']}): {c['faction1']} {c['won1']} - {c['won2']} {c['faction2']}")
        if d.get("live_error"):
            print(f"live lookup error: {d['live_error']}", file=sys.stderr)
    emit(args, res, human)


def print_factions(factions):
    def states(f):
        parts = []
        if f["state"] and f["state"] != "None":
            parts.append(f["state"])
        parts += [f"{s}(active)" for s in f["active_states"] if s != f["state"]]
        parts += [f"{s}(pending)" for s in f["pending_states"]]
        parts += [f"{s}(recovering)" for s in f["recovering_states"]]
        return ", ".join(parts) or "None"
    table(factions, [("Faction", lambda f: ("* " if f["is_controlling"] else "  ") + f["name"]),
                     ("Infl%", "influence_pct"), ("Gov", "government"), ("Allegiance", "allegiance"),
                     ("Mood", "happiness"), ("States", states)])


def cmd_factions(args, session):
    res = live.system_info(session, args.system, args.live, args.max_age_hours)
    if args.history:
        res["history"] = queries.faction_history(session, args.system, args.history, args.faction,
                                                 args.max_points)

    def human(d):
        if not d["found"]:
            return not_found(f"System '{d['system']}'")
        print(f"{d['system']}  (faction data age: {fmt_age(d['factions_age_hours'])}, source: {d['source']})")
        print_factions(d["factions"])
        if args.history:
            h = d["history"]
            print(f"\nObservations, last {args.history} days: {h['observations']} stored, {h['returned']} shown")
            table(h["history"], [
                ("Time", "ts"), ("Faction", "faction"),
                ("Infl%", lambda r: r["influence_pct"] if r["present"] else "LEFT"), ("State", "state"),
                ("Mood", "happiness"), ("Active", lambda r: _states_text(r["active_states"])),
                ("Pending", lambda r: _states_text(r["pending_states"], True)),
                ("Recovering", lambda r: _states_text(r["recovering_states"], True))])
    emit(args, res, human, faction_csv)


def cmd_faction(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Faction '{d['faction']}'")
        table(d["systems"], [("System", "system"), ("Faction", "faction"), ("Infl%", "influence_pct"),
                             ("State", "state"), ("Controls", lambda r: "yes" if r["is_controlling"] else ""),
                             ("History rows", "history_rows"), ("First seen", lambda r: r["first_seen"] or "-"),
                             ("Age", lambda r: fmt_age(r["age_hours"]))])
    emit(args, queries.faction_presence(session, args.name), human)


def cmd_timeline(args, session):
    def human(d):
        if not d["found"]:
            return print(d.get("note") or f"System '{d['system']}' / faction '{d['faction']}' not found",
                         file=sys.stderr)
        infl = d["influence"]
        print(f"{d['faction']} in {d['system']}: {d['observations']} observations "
              f"({d['first_observation']} -> {d['last_observation']})")
        if infl:
            print(f"  influence {infl[0]['influence_pct']}% -> {infl[-1]['influence_pct']}%  "
                  f"(min {min(p['influence_pct'] for p in infl)}%, max {max(p['influence_pct'] for p in infl)}%)")
        for t in d["left_events"]:
            print(f"  LEFT the system at {t}")
        table(d["intervals"], [("Field", "field"), ("Value", "value"),
                               ("Trend", lambda r: "" if r["trend"] is None else r["trend"]),
                               ("Start", "start"), ("Last seen", "last_seen"),
                               ("Ended", lambda r: r["ended_at"] or "(still present)")])
        print(f"  {d['note']}")
    emit(args, queries.faction_timeline(session, args.system, args.faction, args.days, args.max_points), human,
         lambda d: (TIMELINE_COLUMNS, d.get("intervals", [])))


def cmd_market(args, session):
    res = live.station_market(session, args.station, args.system, args.live, args.max_age_hours)

    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'")
        if d.get("ambiguous"):
            print("Multiple matches, pass --system:")
            for c in d["candidates"]:
                print(f"  {c['station']} ({c['system']})")
            return
        print(f"{d['station']} ({d['system']}) {d['station_type'] or ''}  economies={', '.join(d['economies'])}  "
              f"age={fmt_age(d['age_hours'])}  source={d['source']}")
        rows = [c for c in d["commodities"] if c["stock"] or c["demand"]]
        table(rows, [("Commodity", "commodity"), ("Buy", lambda r: fmt_cr(r["buy_price"]) if r["stock"] else "-"),
                     ("Stock", lambda r: r["stock"] or "-"),
                     ("Sell", lambda r: fmt_cr(r["sell_price"]) if r["demand"] else "-"),
                     ("Demand", lambda r: r["demand"] or "-"), ("Mean", "mean_price")])
        if d.get("live_error"):
            print(f"live lookup error: {d['live_error']}", file=sys.stderr)
    emit(args, res, human)


def cmd_price_history(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'")
        if d.get("ambiguous"):
            return print("Multiple matches, pass --system.")
        if "series_by_commodity" in d:
            series = d["series_by_commodity"]
            note = ""
            if d.get("truncated"):
                note = f" (showing {len(series)} of {d['commodities_available']}; --all-commodities lifts the cap)"
            print(f"{d['station']} ({d['system']}), last {d['days']} days, {len(series)} commodities{note}")
            table([dict(commodity=sym, **blk) for sym, blk in series.items()], [
                ("Commodity", "commodity"), ("Updates", "updates"),
                ("Buy avg", lambda r: fmt_cr(r["buy_price_stats"]["avg"]) if r["buy_price_stats"] else "-"),
                ("Sell avg", lambda r: fmt_cr(r["sell_price_stats"]["avg"]) if r["sell_price_stats"] else "-"),
                ("Stock avg", lambda r: r["stock_stats"]["avg"] if r["stock_stats"] else "-"),
                ("Demand avg", lambda r: r["demand_stats"]["avg"] if r["demand_stats"] else "-"),
                ("Last update", lambda r: r["last_update"] or "-")])
            print("Use one commodity for its series, or --csv / --json for every point.")
            return
        bucket = d.get("bucket_minutes")
        print(f"{d['commodity']} at {d['station']} ({d['system']}), last {d['days']} days, "
              f"{d['updates']} updates" + (f", shown as {d['points']} buckets of {bucket} min" if bucket else ""))
        for label, key in (("buy ", "buy_price_stats"), ("sell", "sell_price_stats")):
            if d[key]:
                print(f"  {label}: min {fmt_cr(d[key]['min'])}  avg {fmt_cr(d[key]['avg'])}  max {fmt_cr(d[key]['max'])}")
        for label, key in (("stock ", "stock_stats"), ("demand", "demand_stats")):
            if d[key]:
                print(f"  {label}: min {d[key]['min']:,}  avg {d[key]['avg']:,}  max {d[key]['max']:,}")
        money = lambda key: (lambda r: fmt_cr(r[key]) if r[key] else "-")
        if bucket:
            table(d["series"], [("Bucket start", "ts"), ("Updates", "updates"), ("Buy avg", money("buy_avg")),
                                ("Sell avg", money("sell_avg")), ("Mean", money("mean_avg")),
                                ("Stock avg", "stock_avg"), ("Stock min-max", lambda r: f"{r['stock_min']}-{r['stock_max']}"),
                                ("Demand avg", "demand_avg"),
                                ("Demand min-max", lambda r: f"{r['demand_min']}-{r['demand_max']}")])
        else:
            table(d["series"], [("Time", "ts"), ("Buy", money("buy_price")), ("Stock", "stock"),
                                ("Sell", money("sell_price")), ("Demand", "demand"), ("Mean", money("mean_price"))])
    emit(args, queries.price_history(session, args.station, args.commodity, args.system, args.days,
                                     args.bucket, args.max_points, args.all_commodities, args.max_commodities),
         human, price_csv)


def cmd_activity(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'")
        if d.get("ambiguous"):
            return print("Multiple matches, pass --system.")
        print(f"{d['station']} ({d['system']}): {d['updates']} updates in the last {d['days']} days "
              f"(history mode: {d['history_mode']})")
        print(f"  {d['note']}")
        print(f"  first {d['first_update']}  last {d['last_update']}  current snapshot {d['current_snapshot_at']}")
        table(d["per_day"], [("Date", "date"), ("Updates", "updates")])
    emit(args, queries.station_activity(session, args.station, args.system, args.days), human)


def cmd_copy_data(args, _):
    from . import copydata
    counts = copydata.copy_data(args.source, args.dest)
    emit(args, counts, lambda d: print("copied: " + ", ".join(f"{k}={v}" for k, v in d.items())))


def cmd_prune(args, session):
    removed = store.prune(session, args.days)
    emit(args, removed, lambda d: print(f"Removed: {d}"))


def cmd_commodity(args, session):
    mode = "sell" if args.sell else "buy"
    res = queries.find_commodity(session, args.name, mode, args.near, args.ly, args.min_amount,
                                 args.max_age_hours, not args.no_carriers, args.limit)

    def human(d):
        if "error" in d:
            print(d["error"], file=sys.stderr)
            return
        if not d["results"]:
            print(f"No fresh {mode} data for '{d['commodity']}' in the local database.")
            return
        label = "Sell price" if mode == "sell" else "Buy price"
        table(d["results"], [("Station", "station"), ("System", "system"), (label, lambda r: fmt_cr(r["price"])),
                             ("Demand" if mode == "sell" else "Stock", "amount"),
                             ("Ly", lambda r: r["distance_ly"] if r["distance_ly"] is not None else "-"),
                             ("Carrier", lambda r: "yes" if r["is_carrier"] else ""),
                             ("Age", lambda r: fmt_age(r["age_hours"]))])
    emit(args, res, human)


def cmd_trades(args, session):
    res = queries.best_trades(session, args.origin, args.ly, args.cargo, args.max_age_hours,
                              args.min_amount, args.carriers, args.limit)

    def human(d):
        if "error" in d:
            print(d["error"], file=sys.stderr)
            return
        print(f"{d['markets_considered']} markets within {d['radius_ly']} ly of {d['origin']}")
        table(d["trades"], [("Commodity", "commodity"), ("Profit/t", lambda r: fmt_cr(r["profit_per_ton"])),
                            ("Tons", "tons"), ("Total", lambda r: fmt_cr(r["total_profit"])),
                            ("Buy at", "buy_at"), ("Sell at", "sell_at"),
                            ("Leg ly", lambda r: r["leg_distance_ly"]),
                            ("Age b/s", lambda r: f"{fmt_age(r['buy_age_hours'])}/{fmt_age(r['sell_age_hours'])}")])
    emit(args, res, human)


# ---- argument parsing -------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="eddn_helper",
                                description="Query Elite Dangerous market and faction data collected from EDDN.")
    p.add_argument("--db", "--db-url", dest="db",
                   help="database URL or SQLite path (default: $ELITE_HELPER_DATABASE_URL or %%LOCALAPPDATA%%\\elite-helper\\elite.sqlite)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_, live_opt=False):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(func=fn)
        sp.add_argument("--json", action="store_true", help="machine-readable JSON output")
        if name in ("price-history", "factions", "timeline"):
            sp.add_argument("--csv", action="store_true", help="CSV on stdout (redirect to a file for charting)")
        if live_opt:
            sp.add_argument("--live", action="store_true", help="query EDSM if local data is missing or stale")
            sp.add_argument("--max-age-hours", type=float, default=48, help="staleness threshold for --live (default 48)")
        return sp

    sp = add("listen", cmd_listen, "Run the EDDN listener (leave running to collect data)")
    sp.set_defaults(raw=True)
    sp.add_argument("--log-file", help="write logs here (rotating) instead of only stderr")
    sp = add("status", cmd_status, "Database counts, schema revision and listener health")
    sp.set_defaults(raw=True)
    sp = add("migrate", cmd_migrate, "Database schema migrations (Alembic)")
    sp.set_defaults(raw=True)
    msub = sp.add_subparsers(dest="mcmd", required=True)
    msub.add_parser("status", help="show current vs head revision")
    up = msub.add_parser("upgrade", help="apply pending migrations (backs up SQLite first)")
    up.add_argument("--rev", default="head")
    up.add_argument("--no-backup", action="store_true")
    nw = msub.add_parser("new", help="autogenerate a new revision from the models (development)")
    nw.add_argument("message")
    sp = add("backup", cmd_backup, "Consistent copy of the SQLite database (safe while the listener runs)")
    sp.set_defaults(raw=True)
    sp.add_argument("path")
    sp = add("import-legacy", cmd_import_legacy, "Import rows from the old pre-SQLAlchemy SQLite file")
    sp.set_defaults(raw=True)
    sp.add_argument("--from", dest="source", help="old database file (default: <data dir>\\eddn.sqlite)")
    sp = add("system", cmd_system, "System info and faction states", True)
    sp.add_argument("name")
    sp = add("factions", cmd_factions, "Faction table for a system, optionally with influence history", True)
    sp.add_argument("system")
    sp.add_argument("--history", type=int, metavar="DAYS", help="also show influence changes over N days")
    sp.add_argument("--faction", help="limit history to one faction")
    sp.add_argument("--max-points", type=int, default=500,
                    help="thin long histories to about this many rows per faction, keeping every change (0 = never)")
    sp = add("timeline", cmd_timeline, "Chart-ready view of one faction in one system: influence and state intervals")
    sp.add_argument("system")
    sp.add_argument("faction")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--max-points", type=int, default=500, help="thin the influence series (0 = never)")
    sp = add("faction", cmd_faction, "Where a faction is present, with influence and state")
    sp.add_argument("name")
    sp = add("market", cmd_market, "Commodity table for a station", True)
    sp.add_argument("station")
    sp.add_argument("--system", help="system name (required for --live, and to disambiguate)")
    sp = add("price-history", cmd_price_history,
             "Price, stock and demand time series at a station: one commodity, a,b,c, or 'all'")
    sp.add_argument("station")
    sp.add_argument("commodity", help="commodity name, comma-separated list, or 'all'")
    sp.add_argument("--all-commodities", action="store_true", help="with 'all': no cap on the number of commodities")
    sp.add_argument("--max-commodities", type=int, default=25, help="with 'all': cap (default 25, busiest first)")
    sp.add_argument("--system")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--bucket", type=int, metavar="MINUTES", help="summarise into buckets of this many minutes")
    sp.add_argument("--max-points", type=int, default=500,
                    help="auto-bucket when there are more rows than this (default 500; 0 = never)")
    sp = add("activity", cmd_activity, "How many market updates are stored for a station, per day")
    sp.add_argument("station")
    sp.add_argument("--system")
    sp.add_argument("--days", type=int, default=30)
    sp = add("copy-data", cmd_copy_data, "Copy every table from one database to another (e.g. SQLite -> PostgreSQL)")
    sp.set_defaults(raw=True)
    sp.add_argument("--from", dest="source", required=True, help="source database URL or SQLite path")
    sp.add_argument("--to", dest="dest", required=True, help="destination database URL (must be migrated to head)")
    sp = add("prune", cmd_prune, "Delete commodity data older than N days (the listener also does this hourly)")
    sp.add_argument("--days", type=int, default=store.RETENTION_DAYS)
    sp = add("commodity", cmd_commodity, "Best places to sell (--sell) or buy (--buy) a commodity")
    sp.add_argument("name")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--sell", action="store_true", help="highest sell price (needs demand)")
    g.add_argument("--buy", action="store_true", help="lowest buy price (needs stock)")
    sp.add_argument("--near", metavar="SYSTEM", help="measure distance from this system")
    sp.add_argument("--ly", type=float, help="max distance from --near")
    sp.add_argument("--min-amount", type=int, default=0, help="minimum stock (buy) or demand (sell)")
    sp.add_argument("--max-age-hours", type=float, default=48, help="ignore data older than this (default 48)")
    sp.add_argument("--no-carriers", action="store_true", help="exclude fleet carriers")
    sp.add_argument("--limit", type=int, default=15)
    sp = add("trades", cmd_trades, "Best buy/sell pairs among markets near a system")
    sp.add_argument("origin", metavar="SYSTEM")
    sp.add_argument("--ly", type=float, default=30, help="search radius (default 30)")
    sp.add_argument("--cargo", type=int, help="cargo capacity in tons (ranks by total profit)")
    sp.add_argument("--min-amount", type=int, default=100, help="minimum stock/demand per side (default 100)")
    sp.add_argument("--max-age-hours", type=float, default=24, help="ignore data older than this (default 24)")
    sp.add_argument("--carriers", action="store_true", help="include fleet carriers")
    sp.add_argument("--limit", type=int, default=10)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "raw", False):
            rc = args.func(args, None)
            sys.exit(rc or 0)
        engine = database.make_engine(args.db)
        try:
            migrate.ensure_current(engine)
            session = database.make_session_factory(engine)()
            try:
                args.func(args, session)
            finally:
                session.close()
        finally:
            engine.dispose()
    except migrate.SchemaError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
