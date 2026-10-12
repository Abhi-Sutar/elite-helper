"""Command-line interface: python -m eddn_helper <command> [--json]."""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone

from . import config, database, legacy, live, migrate, queries, store


def fmt_cr(v):
    return "-" if v is None else f"{v:,.0f} Cr"


def fmt_age(h):
    if h is None:
        return "?"
    return f"{h * 60:.0f}m" if h < 1 else (f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d")


_UTC_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def show_time(value):
    """UTC ISO stamp ('2026-10-11T10:54:03Z') -> the display timezone (ELITE_HELPER_DISPLAY_TZ), other values unchanged."""
    if not isinstance(value, str) or not _UTC_STAMP.fullmatch(value):
        return value
    tz = config.display_tz()
    if tz is None:
        return value
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(None if tz == "local" else tz).isoformat(timespec="seconds")


def table(rows, cols):
    """cols: list of (header, key_or_callable). Prints a left-aligned text table."""
    cells = [[h for h, _ in cols]]
    for r in rows:
        cells.append([str(show_time(k(r) if callable(k) else r.get(k, ""))) for _, k in cols])
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
        writer.writerows([[("" if r.get(c) is None else show_time(r.get(c))) for c in columns] for r in rows])
    elif args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        human(data)


PRICE_RAW = ["ts", "commodity", "buy_price", "sell_price", "mean_price", "stock", "demand",
             "stock_bracket", "demand_bracket"]
PRICE_RESAMPLED = ["ts", "commodity", "buy_price", "sell_price", "mean_price", "stock", "demand", "stock_bracket",
                   "demand_bracket", "listed", "stale", "updates", "carried"]
PRICE_BUCKET = ["ts", "commodity", "updates", "changes", "buy_min", "buy_avg", "buy_max", "sell_min", "sell_avg", "sell_max",
                "stock_min", "stock_avg", "stock_max", "demand_min", "demand_avg", "demand_max", "mean_avg",
                "stock_bracket", "demand_bracket"]


def price_csv(d):
    """Flatten a (single or multi commodity) price_history reply. An inactive side (price 0) is left empty."""
    if not d.get("found") or d.get("ambiguous"):
        return PRICE_RAW, []
    blocks = d["series_by_commodity"] if "series_by_commodity" in d else {d["commodity"]: d}
    bucketed = any("bucket_minutes" in b for b in blocks.values())
    resampled = any("resample_minutes" in b for b in blocks.values())
    rows = []
    for sym, block in blocks.items():
        for pt in block["series"]:
            row = dict(pt, commodity=sym)
            for key in ("buy_price", "sell_price", "mean_price"):
                if row.get(key) == 0:
                    row[key] = None
            rows.append(row)
    return (PRICE_RESAMPLED if resampled else PRICE_BUCKET if bucketed else PRICE_RAW), rows


TRAFFIC_COLUMNS = ["ts", "updates", "distinct_uploaders", "changed"]


def traffic_csv(d):
    return TRAFFIC_COLUMNS, d.get("series", []) if d.get("found", True) else []


def busiest_csv(d):
    rows = d.get("stations") or d.get("systems") or []
    cols = list(rows[0]) if rows else ["station"]
    return cols, rows


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

def player_mark(f, unknown=False):
    """'[P] ' for a player faction; with `unknown`, '[?] ' when its status has not been fetched yet."""
    return "[P] " if f.get("is_player") else "[?] " if unknown and f.get("is_player") is None else ""


def hits_text(f):
    """'active, pending (trend 1)': where the searched state was found for one faction."""
    text = ", ".join(h["in"] + (f" (trend {h['trend']})" if h["trend"] is not None else "") for h in f["found_in"]) \
        or f"{f['influence_pct']}%"
    if f.get("state_started_at"):
        text += f", since {f['state_started_at']} ({f['state_hours_ago']}h ago" \
                + (", or earlier" if f.get("start_is_lower_bound") else "") + ")"
    return text


SYSTEMS_COLUMNS = ["system", "distance_ly", "population", "economy", "second_economy", "matched_economy", "factions",
                   "stations", "demand", "factions_age_hours"]


def systems_csv(d):
    rows = []
    for r in d.get("results", []) if "error" not in d else []:
        rows.append(dict(
            r, factions="; ".join(f"{player_mark(f, True)}{f['faction']} ({f['state'] or '-'}): {hits_text(f)}"
                                  for f in r["matching_factions"]),
            stations="; ".join(f"{s['station']} [{'/'.join(s['economies'])}]" for s in r["stations"]),
            demand="; ".join(f"{x['commodity']}@{x['station']} {x['demand']}" for x in r["demand"])))
    return SYSTEMS_COLUMNS, rows


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


def cmd_economy(args, session):
    res = live.system_economy(session, args.name, args.live, args.max_age_hours)

    def human(d):
        if not d["found"]:
            return not_found(f"System '{d['system']}'")
        print(f"{d['system']}  economy={d['economy'] or '-'} / {d['second_economy'] or '-'}  pop={d['population'] or 0:,}  "
              f"(data {fmt_age(d['info_age_hours'])} old, source: {d['source']})")
        if not d["stations_known"]:
            print("(stations: only those seen in market uploads - run `refresh-stations` for the full EDSM list)")
        table(d["stations"], [("Station", "station"), ("Type", lambda r: r["station_type"] or "-"),
                              ("Body", lambda r: r["body"] or "-"),
                              ("Economies", lambda r: "/".join(r["economies"]) or "-"),
                              ("Controlled by", lambda r: r["controlling_faction"] or "-"),
                              ("Prices", lambda r: fmt_age(r["market_age_hours"]) if r["has_prices"] else "-")])
        if d.get("stations_live"):
            print("\nAlso listed by EDSM (no market stored locally):")
            table(d["stations_live"], [("Station", "station"), ("Type", lambda r: r["station_type"] or "-"),
                                       ("Economies", lambda r: "/".join(r["economies"]) or "-")])
        if d.get("live_error"):
            print(f"live lookup error: {d['live_error']}", file=sys.stderr)
    emit(args, res, human)


def _find_systems(args, session):
    conflict = {"yes": True, "no": False}.get(args.conflict)
    return queries.find_systems(
        session, args.economy, args.economy_scope, not args.no_second, args.state, args.state_in,
        args.controlling_only, args.near, args.ly, args.min_pop, args.demand, args.carriers, args.max_age_hours,
        args.limit, exclude_economies=args.exclude_economy, exclude_scope=args.exclude_scope,
        exclude_states=args.exclude_state, exclude_states_in=args.exclude_state_in,
        exclude_systems=args.exclude_system, max_population=args.max_pop, allegiance=args.allegiance,
        government=args.government, security=args.security, in_conflict=conflict, conflict_type=args.conflict_type,
        faction_name=args.faction, min_influence=args.min_influence, max_influence=args.max_influence,
        happiness=args.happiness, state_trend=args.trend, min_stations=args.min_stations,
        station_types=args.station_type, sort=args.sort, min_faction_stations=args.min_faction_stations,
        player_faction={"yes": True, "no": False}.get(args.player_faction),
        state_since_hours=args.state_since_hours)


def refresh_system_stations(session, name, id64=None):
    """Fetch one system's stations from EDSM into the database. Returns (count_or_None, error_message_or_None)."""
    from sqlalchemy.exc import DBAPIError
    from . import external
    try:
        return external.refresh_stations(session, name, id64), None
    except external.LiveLookupError as e:
        return None, str(e)
    except DBAPIError:
        session.rollback()
        return None, live.READ_ONLY_MSG


def cmd_refresh_stations(args, session):
    out = []
    for name in args.systems:
        n, err = refresh_system_stations(session, name)
        out.append({"system": name, "stations": n, "error": err})
        msg = f"{n} stations stored" if n is not None else (err or "not found (is the system in the database?)")
        print(f"{name}: {msg}", file=sys.stderr)
        if err:
            break
    if args.json:
        print(json.dumps(out, indent=2))


def cmd_faction_stations(args, session):
    res = queries.faction_stations(session, args.faction, args.system, args.near, args.ly, args.station_type, args.limit)

    def human(d):
        if "error" in d:
            print(d["error"], file=sys.stderr)
            return
        print(f"{d['count']} stations in {d['systems']} systems controlled by {d['faction']}")
        table(d["stations"], [("System", "system"), ("Station", "station"), ("Type", lambda r: r["station_type"] or "-"),
                              ("Body", lambda r: r["body"] or "-"), ("Economy", lambda r: "/".join(r["economies"]) or "-"),
                              ("Ls", lambda r: r["distance_to_arrival_ls"] if r["distance_to_arrival_ls"] is not None else "-"),
                              ("Ly", lambda r: r["distance_ly"] if r["distance_ly"] is not None else "-")])
        print(d["note"])
    emit(args, res, human)


def cmd_find_systems(args, session):
    res = _find_systems(args, session)
    if args.refresh_stations and "error" not in res:
        budget, tried = args.refresh_limit, set()
        while budget > 0:
            stale = args.stations_max_age_hours
            todo = [r for r in res["results"] if r["system"] not in tried and (
                not r["stations_known"] or (r["stations_fetched_age_hours"] or 0) > stale
                or (args.player_faction and any(f["is_player"] is None for f in r["matching_factions"])))][:budget]
            if not todo:
                break
            for r in todo:
                tried.add(r["system"])
                budget -= 1
                n, err = refresh_system_stations(session, r["system"], r["id64"])
                print(f"refreshed stations of {r['system']}: " + (f"{n} stored" if err is None and n is not None
                                                                   else err or "not found at EDSM"), file=sys.stderr)
                if err:
                    budget = 0
                    break
            res = _find_systems(args, session)
    def human(d):
        if "error" in d:
            print(d["error"], file=sys.stderr)
            return
        print(f"{d['count']} matching systems" + (f" (showing {d['shown']})" if d["shown"] < d["count"] else ""))
        if not d["results"]:
            return

        def factions(r):
            return "; ".join(f"{player_mark(f, True)}{f['faction']} ({f['state'] or '-'}): {hits_text(f)}"
                             + (f", {f['stations_controlled']} stn" if r["stations_known"] else "")
                             for f in r["matching_factions"]) or "-"

        def economy(r):
            extra = f" ({r['matched_economy']})" if r["matched_economy"] not in (None, "economy") else ""
            return f"{r['economy'] or '-'}/{r['second_economy'] or '-'}{extra}"
        cols = [("System", "system"), ("Ly", lambda r: r["distance_ly"] if r["distance_ly"] is not None else "-"),
                ("Pop", lambda r: f"{r['population']:,}" if r["population"] else "-"), ("Economy", economy)]
        if any((args.state, args.faction, args.happiness, args.controlling_only, args.min_influence is not None,
                args.max_influence is not None, args.player_faction)):
            cols.append(("Factions", factions))
        cols += [("Stations", lambda r: f"{len(r['stations'])}" + ("" if r["stations_known"] else "?")),
                 ("Faction age", lambda r: fmt_age(r["factions_age_hours"]))]
        if args.demand:
            cols.append(("Demand", lambda r: ", ".join(f"{x['commodity']}@{x['station']}" for x in r["demand"][:3])))
        table(d["results"], cols)
        if d.get("stations_unknown"):
            print(f"\n{d['stations_unknown']} of {d['count']} systems have no EDSM station data ('?' above; station "
                  f"filters were not applied to them). Fetch it with --refresh-stations or: refresh-stations <system> ...")
    emit(args, res, human, systems_csv)


def print_factions(factions):
    def states(f):
        parts = []
        if f["state"] and f["state"] != "None":
            parts.append(f["state"])
        parts += [f"{s}(active)" for s in f["active_states"] if s != f["state"]]
        parts += [f"{s}(pending)" for s in f["pending_states"]]
        parts += [f"{s}(recovering)" for s in f["recovering_states"]]
        return ", ".join(parts) or "None"
    table(factions, [("Faction", lambda f: ("* " if f["is_controlling"] else "  ") + player_mark(f) + f["name"]),
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
        rows = [c for c in d["commodities"] if (c["stock"] or c["demand"]) and not c["stale"]]
        hidden = sum(1 for c in d["commodities"] if c["stale"])
        table(rows, [("Commodity", "commodity"), ("Buy", lambda r: fmt_cr(r["buy_price"]) if r["stock"] else "-"),
                     ("Stock", lambda r: r["stock"] or "-"),
                     ("Sell", lambda r: fmt_cr(r["sell_price"]) if r["demand"] else "-"),
                     ("Demand", lambda r: r["demand"] or "-"), ("Mean", "mean_price"),
                     ("Seen", lambda r: fmt_age(r["seen_age_hours"]))])
        if hidden:
            print(f"({hidden} commodities not reported for 48+ hours before the last update are hidden; "
                  f"see --json for all)")
        if d.get("live_error"):
            print(f"live lookup error: {d['live_error']}", file=sys.stderr)
    emit(args, res, human)


def cmd_price_history(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'")
        if d.get("ambiguous"):
            return print("Multiple matches, pass --system.")
        updates = f"{d['updates']} updates ({d['updates_source']})"
        if "series_by_commodity" in d:
            series = d["series_by_commodity"]
            note = ""
            if d.get("truncated"):
                note = f" (showing {len(series)} of {d['commodities_available']}; --all-commodities lifts the cap)"
            print(f"{d['station']} ({d['system']}), last {d['days']} days, {updates}, "
                  f"{len(series)} commodities{note}")
            table([dict(commodity=sym, **blk) for sym, blk in series.items()], [
                ("Commodity", "commodity"), ("Rows", "rows"),
                ("Buy avg", lambda r: fmt_cr(r["buy_price_stats"]["avg"]) if r["buy_price_stats"] else "-"),
                ("Sell avg", lambda r: fmt_cr(r["sell_price_stats"]["avg"]) if r["sell_price_stats"] else "-"),
                ("Stock avg", lambda r: r["stock_stats"]["avg"] if r["stock_stats"] else "-"),
                ("Demand avg", lambda r: r["demand_stats"]["avg"] if r["demand_stats"] else "-"),
                ("Last change", lambda r: r["last_update"] or "-")])
            print("Use one commodity for its series, or --csv / --json for every point.")
            return
        bucket, resample = d.get("bucket_minutes"), d.get("resample_minutes")
        shown = (f", shown as {d['points']} buckets of {bucket} min" if bucket else
                 f", resampled every {resample} min ({d['points']} samples)" if resample else "")
        print(f"{d['commodity']} at {d['station']} ({d['system']}), last {d['days']} days, {updates}, "
              f"{d['rows']} stored rows [{d['history_mode']} mode]{shown}")
        for label, key in (("buy ", "buy_price_stats"), ("sell", "sell_price_stats")):
            if d[key]:
                print(f"  {label}: min {fmt_cr(d[key]['min'])}  avg {fmt_cr(d[key]['avg'])}  max {fmt_cr(d[key]['max'])}")
        for label, key in (("stock ", "stock_stats"), ("demand", "demand_stats")):
            if d[key]:
                print(f"  {label}: min {d[key]['min']:,}  avg {d[key]['avg']:,}  max {d[key]['max']:,}")
        money = lambda key: (lambda r: fmt_cr(r[key]) if r[key] else "-")
        if bucket:
            table(d["series"], [("Bucket start", "ts"), ("Updates", "updates"), ("Changes", "changes"),
                                ("Buy avg", money("buy_avg")), ("Sell avg", money("sell_avg")),
                                ("Mean", money("mean_avg")), ("Stock avg", "stock_avg"),
                                ("Stock min-max", lambda r: f"{r['stock_min']}-{r['stock_max']}"),
                                ("Demand avg", "demand_avg"),
                                ("Demand min-max", lambda r: f"{r['demand_min']}-{r['demand_max']}")])
        elif resample:
            table(d["series"], [("Time", "ts"), ("Buy", money("buy_price")), ("Stock", "stock"),
                                ("Sell", money("sell_price")), ("Demand", "demand"), ("Updates", "updates"),
                                ("Carried", lambda r: "yes" if r["carried"] else "")])
        else:
            table(d["series"], [("Time", "ts"), ("Buy", money("buy_price")), ("Stock", "stock"),
                                ("Sell", money("sell_price")), ("Demand", "demand"), ("Mean", money("mean_price")),
                                ("Listed", lambda r: "" if r["listed"] else "NO")])
    emit(args, queries.price_history(session, args.station, args.commodity, args.system, args.days,
                                     args.bucket, args.max_points, args.all_commodities, args.max_commodities,
                                     args.resample), human, price_csv)


def cmd_traffic(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'" if "station" in d else f"System '{d['system']}'")
        if d.get("ambiguous"):
            return print("Multiple matches, pass --system.")
        name = d.get("station") or d["system"]
        print(f"{name}: {d['updates']} updates in the last {d['days']} days "
              f"({d['updates_per_hour']}/hour over {d['observed_hours']} observed hours), "
              f"{d['distinct_uploaders']} distinct uploaders, {d['changed']} history rows written")
        if d.get("busiest_bucket"):
            print(f"  busiest {d['bucket_minutes']}-minute bucket: {show_time(d['busiest_bucket']['ts'])} "
                  f"({d['busiest_bucket']['updates']} updates)")
        if d.get("software"):
            print("  software: " + ", ".join(f"{x['software']} ({x['updates']})" for x in d["software"]))
        if d["blank_uploader_updates"]:
            print(f"  {d['blank_uploader_updates']} updates had no uploader id")
        table(d["series"], [("Bucket start", "ts"), ("Updates", "updates"), ("Uploaders", "distinct_uploaders"),
                            ("Rows written", "changed")])
    data = (queries.system_traffic(session, args.name, args.days, args.bucket, args.max_points) if args.systems
            else queries.station_traffic(session, args.name, args.system, args.days, args.bucket, args.max_points))
    emit(args, data, human, traffic_csv)


def cmd_busiest(args, session):
    def human(d):
        rows = d.get("systems") if args.systems else d["stations"]
        print(f"{d['total_updates']} updates in the last {d['days']} day(s)")
        if args.systems:
            table(rows, [("System", "system"), ("Updates", "updates"), ("Per hour", "updates_per_hour"),
                         ("Uploaders", "distinct_uploaders"), ("Rows written", "faction_rows_written"),
                         ("% of all", "share_of_all_updates_pct")])
        else:
            table(rows, [("Station", "station"), ("System", "system"),
                         ("Carrier", lambda r: "yes" if r["is_carrier"] else ""), ("Updates", "updates"),
                         ("Per hour", "updates_per_hour"), ("Uploaders", "distinct_uploaders"),
                         ("Rows written", "price_rows_written"), ("% of all", "share_of_all_updates_pct")])
    data = (queries.busiest_systems(session, args.days, args.limit) if args.systems
            else queries.busiest_stations(session, args.days, args.limit, not args.no_carriers))
    emit(args, data, human, busiest_csv)


def cmd_activity(args, session):
    def human(d):
        if not d["found"]:
            return not_found(f"Station '{d['station']}'")
        if d.get("ambiguous"):
            return print("Multiple matches, pass --system.")
        print(f"{d['station']} ({d['system']}): {d['updates']} updates in the last {d['days']} days "
              f"[{d['updates_source']}], {d['price_rows']} price rows stored (history mode: {d['history_mode']})")
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


# ---- journal commands (read the local Saved Games journals, no database needed) ----

def cmd_where(args, _):
    from .journal import context
    data = context.where_am_i(args.journal_dir)
    if args.enrich and "error" not in data:
        engine = database.make_engine(args.db)
        try:
            migrate.ensure_current(engine)
            with database.session_scope(database.make_session_factory(engine)) as session:
                context.enrich(session, data, args.live)
        except migrate.SchemaError as e:   # keep the journal answer; just say why the database part is missing
            data["enrich_error"] = str(e)
        finally:
            engine.dispose()
    emit(args, data, lambda d: print("\n".join(context.format_where(d))))
    return 1 if "error" in data else 0


def cmd_credit_rate(args, _):
    from .journal import files as jfiles, income
    if args.from_db:
        from .journal import queries as jq
        engine = database.make_engine(args.db)
        try:
            migrate.ensure_current(engine)
            with database.session_scope(database.make_session_factory(engine)) as session:
                data = jq.credit_rate(session, directory=args.journal_dir, since=args.since, session_only=args.session,
                                      max_gap_minutes=args.max_gap_minutes, streams=args.streams, crew_wages=args.crew_wages)
        finally:
            engine.dispose()
        emit(args, data, lambda d: print("\n".join(income.format_report(d))))
        return 1 if "error" in data else 0
    try:
        if args.file:
            paths = args.file
        else:
            paths = jfiles.require_journal_files(jfiles.journal_dir(args.journal_dir))
            if args.session:
                paths = paths[-1:]
    except jfiles.JournalNotFound as e:
        data = {"error": str(e)}
    else:
        data = income.credit_rate(paths, args.streams, args.since, args.max_gap_minutes, args.crew_wages)
    emit(args, data, lambda d: print("\n".join(income.format_report(d))))
    return 1 if "error" in data else 0


def cmd_journal_sync(args, _):
    from .journal import ingest
    if args.watch:
        def log(msg):
            line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
            print(line, flush=True)
            if args.log_file:
                with open(args.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        try:
            if not ingest.watch(args.db, args.journal_dir, args.interval, log=log):
                print("another journal ingester is already running", file=sys.stderr)
                return 1
        except KeyboardInterrupt:
            pass
        return 0
    engine = database.make_engine(args.db)
    try:
        migrate.ensure_current(engine)
        with database.session_scope(database.make_session_factory(engine)) as session:
            from .journal import files as jfiles
            directory = jfiles.journal_dir(args.journal_dir)
            if not jfiles.find_journal_files(directory):
                data = {"error": f"No journal files found in: {directory}"}
            else:
                data = ingest.sync(session, directory, rebuild=args.rebuild)
                data["journal_dir"] = directory
    finally:
        engine.dispose()
    emit(args, data, lambda d: print(d["error"] if "error" in d else
                                     f"ingested {d['events']} events from {d['files']} file(s) in {d['seconds']}s"))
    return 1 if "error" in data else 0


def cmd_journal_status(args, session):
    from .journal import ingest

    def human(d):
        svc = d["ingester"]
        print(f"State: {d['state']}   (journals: {d['journal_dir']})")
        print(f"Files: {d['files_ingested']} ingested of {d['files_on_disk']} on disk"
              + (f", {len(d['files_to_ingest'])} to ingest ({d['bytes_behind']:,} bytes)" if d["files_to_ingest"] else ""))
        print(f"Events in database: {d['events']:,}   newest {d['newest_event_in_db'] or '-'}")
        nf = d["newest_journal_file"]
        if nf:
            print(f"Newest journal: {nf['name']} written {nf['modified']}")
        for c in d["commanders"]:
            print(f"Commander: {c['name'] or c['fid']}  {c['events']:,} events, last seen {c['last_seen']}")
        print(f"Database writable: {d['database_writable']}   ingest URL configured: {d['ingest_url_configured']}")
        print("Last ingest: " + (f"{d['last_ingest_minutes_ago']} min ago, {d['last_ingest_events']} events in "
                                  f"{d['last_ingest_seconds']}s" if d["last_ingest_minutes_ago"] is not None else "never"))
        if d["last_error"]:
            print(f"Last error: {d['last_error']}")
        print(f"Ingester service: {'running (pid %s)' % svc['pid'] if svc['running'] else 'not running'}"
              + (f", heartbeat {svc['heartbeat_seconds_ago']}s ago" if svc["heartbeat_seconds_ago"] is not None else "")
              + f", scheduled task: {svc['scheduled_task'] or 'not installed'}")
    emit(args, ingest.journal_status(session, args.journal_dir), human)


# ---- journal history and state views (database-backed; ingest on demand) -------

def _jq(args, fn, **kw):
    """Run a journal query: fresh unless --no-sync, from --journal-dir. Warn on stderr if the data may be behind."""
    res = fn(args.session_obj, commander=args.commander, directory=args.journal_dir, fresh=not args.no_sync, **kw)
    fr = res.get("journal_freshness") or {}
    if fr.get("db_behind_journal") or fr.get("ingest_error"):
        print(f"warning: journal data may be behind - {fr.get('ingest_error') or 'database not up to date'}", file=sys.stderr)
    return res


def _jerr(d):
    if d.get("error"):
        print(d["error"], file=sys.stderr)
        return True
    return False


def _jemit(args, session, fn, human, csv_rows=None, **kw):
    args.session_obj = session
    data = _jq(args, fn, **kw)
    emit(args, data, lambda d: None if _jerr(d) else human(d), csv_rows)


def cmd_history(args, session):
    from .journal import queries as jq

    def human(d):
        print(f"{d['matched']} matching events" + (f", showing {d['returned']}" if d["truncated"] else ""))
        table(d["events"], [("Time", "ts"), ("Event", "event"), ("System", "system"), ("Station", "station"),
                            ("What", lambda r: r.get("summary") or "")])
    _jemit(args, session, jq.journal_events, human, lambda d: (["ts", "event", "system", "station", "body", "summary"],
                                                              d.get("events", [])),
           event=args.event, system=args.system, station=args.station, text=args.text, days=args.days, since=args.since,
           until=args.until, limit=args.limit, include_data=args.data, oldest_first=args.oldest_first)


def cmd_travel(args, session):
    from .journal import queries as jq

    def human(d):
        print(f"{d['jumps']} jumps, {d['total_ly']} ly, {d['distinct_systems']} systems")
        table(d["log"], [("Time", "ts"), ("System", "system"), ("Jump ly", lambda r: r["jump_ly"] if r["jump_ly"] is not None else "-"),
                         ("Fuel used", lambda r: r["fuel_used"] if r["fuel_used"] is not None else "-"),
                         ("Stayed min", lambda r: r["stayed_minutes"] if r["stayed_minutes"] is not None else "-"),
                         ("Carrier", lambda r: "yes" if r["carrier"] else "")])
    _jemit(args, session, jq.travel_log, human, lambda d: (["ts", "system", "jump_ly", "fuel_used", "fuel_level", "carrier",
                                                            "stayed_minutes"], d.get("log", [])),
           days=args.days, since=args.since, until=args.until, limit=args.limit)


def cmd_visited(args, session):
    from .journal import queries as jq

    def human(d):
        if "stations" in d:
            print(f"{d['distinct_stations']} stations docked at")
            table(d["stations"], [("Station", "station"), ("System", "system"), ("Dockings", "dockings"),
                                  ("First", "first_docked"), ("Last", "last_docked")])
        else:
            print(f"{d['distinct_systems']} systems visited")
            table(d["systems"], [("System", "system"), ("Visits", "visits"), ("First", "first_visit"), ("Last", "last_visit")])
    fn = jq.visited_stations if args.stations else jq.visited_systems
    _jemit(args, session, fn, human, days=args.days, since=args.since, limit=args.limit, system=args.system)


def cmd_sessions(args, session):
    from .journal import queries as jq
    _jemit(args, session, jq.sessions,
           lambda d: table(d["sessions"], [("Start", "start"), ("End", "end"), ("Hours", "hours"), ("Events", "events"),
                                           ("Clean exit", lambda r: "yes" if r["ended_cleanly"] else "no"), ("File", "file")]),
           days=args.days, since=args.since, limit=args.limit)


def cmd_ledger(args, session):
    from .journal import queries as jq

    def human(d):
        print(f"net {fmt_cr(d['net'])}  (earned {fmt_cr(d['earned'])}, spent {fmt_cr(d['spent'])}) over {d['entries_total']} entries")
        table([dict(v, category=k) for k, v in d["by_category"].items()],
              [("Category", "category"), ("Count", "count"), ("Earned", lambda r: fmt_cr(r["earned"])),
               ("Spent", lambda r: fmt_cr(r["spent"])), ("Net", lambda r: fmt_cr(r["net"]))])
        print()
        table(d["entries"], [("Time", "ts"), ("Category", "category"), ("Amount", lambda r: fmt_cr(r["amount"])),
                             ("Running", lambda r: fmt_cr(r["running"])), ("Event", "event"), ("Detail", lambda r: r.get("detail") or "")])
    _jemit(args, session, jq.ledger, human, lambda d: (["ts", "category", "amount", "running", "event", "detail"],
                                                       d.get("entries", [])),
           days=args.days, since=args.since, until=args.until, category=args.category, limit=args.limit)


def cmd_cargo(args, session):
    from .journal import views

    def human(d):
        print(f"{d['total']} / {d['capacity']} t  (as of {show_time(d['as_of'])})")
        table(d["items"], [("Commodity", lambda r: r["localised"] or r["name"]), ("Tons", "count")])
    _jemit(args, session, views.cargo, human)


def cmd_materials(args, session):
    from .journal import views

    def human(d):
        print(f"as of {show_time(d['as_of'])}: " + ", ".join(f"{c} {n}" for c, n in d["totals"].items()))
        for cat, items in d["materials"].items():
            print(f"\n{cat}")
            table(items, [("Material", lambda r: r["localised"] or r["name"]), ("Count", "count")])
    _jemit(args, session, views.materials, human, category=args.category)


def cmd_ships(args, session):
    from .journal import views

    def human(d):
        cur = d.get("current")
        if cur:
            print(f"Current: {cur['type']} \"{cur['name']}\" {cur['ident']}  hull {fmt_cr(cur['hull_value'])}, modules "
                  f"{fmt_cr(cur['modules_value'])}, rebuy {fmt_cr(cur['rebuy'])}, cargo {cur['cargo_capacity']} t, jump {cur['max_jump_range']:.1f} ly")
            if cur.get("modules"):
                table(cur["modules"], [("Slot", "slot"), ("Item", "item"), ("On", lambda r: "" if r["on"] else "off"),
                                       ("Engineering", lambda r: f"{r['engineering']['blueprint']} G{r['engineering']['level']}"
                                        if r["engineering"] else "")])
        st = d.get("stored")
        if st:
            print(f"\nStored ships (as of {show_time(st['as_of'])}):")
            table(st["here"] + st["remote"], [("Ship", "type"), ("Name", lambda r: r["name"] or ""), ("System", "system"),
                                              ("Station", lambda r: r["station"] or ""), ("Value", lambda r: fmt_cr(r["value"]))])
    _jemit(args, session, views.ships, human, modules=args.modules)


def cmd_missions(args, session):
    from .journal import views

    def human(d):
        print(f"{d['active_count']} active missions")
        table(d["active"], [("Mission", lambda r: r.get("name") or ""), ("Faction", lambda r: r.get("faction") or ""),
                            ("Reward", lambda r: fmt_cr(r.get("reward"))), ("Expires", lambda r: r.get("expires") or ""),
                            ("To", lambda r: r.get("destination_system") or "")])
        if d["expired_unresolved"]:
            print(f"\n{len(d['expired_unresolved'])} past their expiry without a result in the journal")
        print(f"\nLast {d['recent_days']} days: {len(d['recent'])} finished, rewards {fmt_cr(d['recent_reward_total'])}")
        table(d["recent"], [("Time", "ts"), ("Result", "result"), ("Mission", lambda r: r.get("name") or ""),
                            ("Reward", lambda r: fmt_cr(r.get("reward")))])
    _jemit(args, session, views.missions, human, recent_days=args.days)


def cmd_ranks(args, session):
    from .journal import views

    def human(d):
        table([dict(v, area=k) for k, v in d["ranks"].items()],
              [("Area", "area"), ("Rank", "name"), ("Progress %", lambda r: r["progress_pct"] if r["progress_pct"] is not None else "-")])
        print("\nReputation: " + ", ".join(f"{k} {v:.1f}" for k, v in d["reputation"].items()))
    _jemit(args, session, views.reputation, human)


def cmd_stats(args, session):
    from .journal import views

    def human(d):
        for sec, vals in d["statistics"].items():
            print(f"\n{sec}")
            for k, v in vals.items():
                print(f"  {k}: {v:,}" if isinstance(v, (int, float)) else f"  {k}: {v}")
    _jemit(args, session, views.commander_stats, human, section=args.section)


def cmd_carrier(args, session):
    from .journal import views

    def human(d):
        c = d["carrier"]
        print(f"{c.get('Name')} ({c.get('Callsign')})  as of {show_time(d['as_of'])}")
        print(f"fuel {c.get('FuelLevel')} t, jump range {c.get('JumpRangeCurr')} ly, docking {c.get('DockingAccess')}")
        fin, space = c.get("Finance") or {}, c.get("SpaceUsage") or {}
        print(f"balance {fmt_cr(fin.get('CarrierBalance'))}, free space {space.get('FreeSpace')} of {space.get('TotalCapacity')}")
    _jemit(args, session, views.carrier, human)


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
        if name in ("price-history", "factions", "timeline", "traffic", "busiest", "find-systems", "faction-stations", "history", "travel", "ledger"):
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
    sp.add_argument("--resample", type=int, metavar="MINUTES",
                    help="evenly spaced samples, carrying the last known state forward")
    sp.add_argument("--system")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--bucket", type=int, metavar="MINUTES", help="summarise into buckets of this many minutes")
    sp.add_argument("--max-points", type=int, default=500,
                    help="auto-bucket when there are more rows than this (default 500; 0 = never)")
    sp = add("traffic", cmd_traffic, "How often a station (or, with --systems, a system) is reported to EDDN")
    sp.add_argument("name", help="station name / fleet carrier callsign (or a system name with --systems)")
    sp.add_argument("--system", help="system of the station, if the name is ambiguous")
    sp.add_argument("--systems", action="store_true", help="treat NAME as a system and show its faction-report traffic")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--bucket", type=int, default=60, metavar="MINUTES", help="bucket size (default 60)")
    sp.add_argument("--max-points", type=int, default=500, help="widen buckets above this many (0 = never)")
    sp = add("busiest", cmd_busiest, "Leaderboard of the most frequently reported stations (or --systems)")
    sp.add_argument("--systems", action="store_true")
    sp.add_argument("--days", type=int, default=1)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--no-carriers", action="store_true", help="exclude fleet carriers (stations only)")
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
    sp = add("economy", cmd_economy, "A system's economy and second economy plus its stations and station economies",
             live_opt=True)
    sp.add_argument("name", metavar="SYSTEM")
    sp = add("find-systems", cmd_find_systems,
             "Find systems by economy, faction state (e.g. Expansion), distance and commodity demand")
    sp.add_argument("--economy", help="comma separated, any of them matches (e.g. Extraction,Refinery,Industrial)")
    sp.add_argument("--economy-scope", choices=["system", "station", "any"], default="system",
                    help="match the system economy (default), station economies, or either")
    sp.add_argument("--no-second", action="store_true", help="ignore the system's second economy")
    sp.add_argument("--state", help="faction state to look for, e.g. Expansion")
    sp.add_argument("--state-in", choices=["any", "state", "active", "pending", "recovering"], default="any",
                    help="where the state must be: main state, active, pending or recovering list (default any)")
    sp.add_argument("--controlling-only", action="store_true", help="only the controlling faction counts")
    sp.add_argument("--near", metavar="SYSTEM", help="measure distance from this system")
    sp.add_argument("--ly", type=float, help="max distance from --near")
    sp.add_argument("--min-pop", type=int, default=0, help="minimum system population")
    sp.add_argument("--demand", help="comma separated commodities a station must currently demand (any of them)")
    sp.add_argument("--carriers", action="store_true", help="include fleet carrier markets")
    sp.add_argument("--max-age-hours", type=float, help="ignore systems whose data is older than this")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--exclude-economy", help="reject systems with any of these economies (system, second or station)")
    sp.add_argument("--exclude-scope", choices=["any", "system", "station"], default="any",
                    help="where --exclude-economy looks: system economies only, stations only, or all (default)")
    sp.add_argument("--exclude-state", help="reject systems where any faction has one of these states")
    sp.add_argument("--exclude-state-in", choices=["any", "state", "active", "pending", "recovering"], default="any")
    sp.add_argument("--exclude-system", help="comma separated system names to drop")
    sp.add_argument("--max-pop", type=int, help="maximum system population")
    sp.add_argument("--allegiance", help="any of (e.g. Federation,Independent)")
    sp.add_argument("--government", help="any of (e.g. Democracy,Corporate)")
    sp.add_argument("--security", help="any of (e.g. low,medium,high,anarchy)")
    sp.add_argument("--conflict", choices=["yes", "no"], help="only systems with / without an active conflict")
    sp.add_argument("--conflict-type", help="War, CivilWar or Election (any of)")
    sp.add_argument("--faction", help="a faction with exactly this name (case-insensitive)")
    sp.add_argument("--min-influence", type=float, help="matching faction influence at least N percent")
    sp.add_argument("--max-influence", type=float, help="matching faction influence at most N percent")
    sp.add_argument("--happiness", help="matching faction happiness, any of (e.g. Happy,Elated)")
    sp.add_argument("--trend", type=int, help="raw trend value of the --state entry (pending/recovering)")
    sp.add_argument("--min-stations", type=int, default=0, help="at least N stations with a stored market")
    sp.add_argument("--station-type", help="a station whose type contains one of these (e.g. coriolis,outpost)")
    sp.add_argument("--sort", choices=list(queries.SORTS), default="distance",
                    help="order: distance (default), population, influence, freshest, name")
    sp.add_argument("--min-faction-stations", type=int, default=0,
                    help="the matching faction controls at least N stations (needs EDSM station data)")
    sp.add_argument("--player-faction", choices=["yes", "no"],
                    help="matching faction is / is not a player faction (status from EDSM; unchecked factions are "
                         "kept and flagged)")
    sp.add_argument("--state-since-hours", type=float,
                    help="with --state: the faction's current run of that state began within N hours (from history)")
    sp.add_argument("--refresh-stations", action="store_true",
                    help="fetch stations (and player-faction status) from EDSM for result systems lacking them "
                         "(needs a writable database)")
    sp.add_argument("--refresh-limit", type=int, default=30, help="max systems to fetch per run (1 request/second)")
    sp.add_argument("--stations-max-age-hours", type=float, default=168,
                    help="also re-fetch stations older than this with --refresh-stations (default 168)")
    sp = add("refresh-stations", cmd_refresh_stations,
             "Fetch the stations (incl. settlements and controlling faction) of systems from EDSM into the database")
    sp.add_argument("systems", nargs="+", metavar="SYSTEM")
    sp = add("faction-stations", cmd_faction_stations, "Stations a faction controls (from the stored EDSM station data)")
    sp.add_argument("faction")
    sp.add_argument("--system", help="only this system")
    sp.add_argument("--near", metavar="SYSTEM")
    sp.add_argument("--ly", type=float)
    sp.add_argument("--station-type", help="e.g. settlement, outpost, coriolis, surface")
    sp.add_argument("--limit", type=int, default=100)
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
    sp = add("where", cmd_where, "Where you are now: system, station, ship and credits from the newest journal")
    sp.set_defaults(raw=True)
    sp.add_argument("--journal-dir", help="journal folder (default: $ELITE_JOURNAL_DIR or the Saved Games folder)")
    sp.add_argument("--enrich", action="store_true",
                    help="also show what the EDDN database knows about the current system and station")
    sp.add_argument("--live", action="store_true", help="with --enrich: query EDSM if the system is missing or stale")
    sp = add("credit-rate", cmd_credit_rate, "Credits/hour from journal files over stitched active playtime")
    sp.set_defaults(raw=True)
    sp.add_argument("--journal-dir", help="journal folder (default: $ELITE_JOURNAL_DIR or the Saved Games folder)")
    sp.add_argument("--file", action="append", help="parse specific journal file(s); can repeat")
    sp.add_argument("--since", help="only count events after this UTC time, YYYY-MM-DDTHH:MM:SS")
    sp.add_argument("--max-gap-minutes", type=float, help="treat in-session gaps longer than this as non-playtime (AFK)")
    sp.add_argument("--streams", help="comma separated subset of: trading,bounty,mission,exploration,carrier,"
                                      "carrier_market,maintenance,rebuy,crew_wages (default all)")
    sp.add_argument("--crew-wages", action="store_true", help="also show wages paid per NPC crew member")
    sp.add_argument("--session", action="store_true", help="only the most recent journal file")
    sp.add_argument("--from-db", action="store_true",
                    help="compute from the ingested events in the database (fast; ingests new lines first)")
    def jadd(name, fn, help_, days=None, limit=None, since=False):
        """A journal-database command: --commander, --journal-dir, --no-sync plus the usual window/limit options."""
        sp = add(name, fn, help_)
        sp.add_argument("--commander", help="commander name or FID (default: the most recently seen)")
        sp.add_argument("--journal-dir", help="journal folder to check for new lines (default: $ELITE_JOURNAL_DIR or Saved Games)")
        sp.add_argument("--no-sync", action="store_true", help="do not ingest new journal lines first; use what is stored")
        if days is not None:
            sp.add_argument("--days", type=int, default=days or None, help="only the last N days" + (f" (default {days})" if days else ""))
        if since:
            sp.add_argument("--since", help="start time, YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (UTC)")
        if limit:
            sp.add_argument("--limit", type=int, default=limit)
        return sp

    sp = jadd("history", cmd_history, "Search your journal history by event, system, station, text and time", 0, 50, True)
    sp.add_argument("--event", help="event name(s), comma separated, e.g. Docked,FSDJump")
    sp.add_argument("--system")
    sp.add_argument("--station")
    sp.add_argument("--text", help="case-insensitive text anywhere in the event")
    sp.add_argument("--until", help="end time (UTC)")
    sp.add_argument("--data", action="store_true", help="include the raw event JSON (with --json)")
    sp.add_argument("--oldest-first", action="store_true")
    sp = jadd("travel", cmd_travel, "Travel log: jumps with distance, fuel and time spent in each system", 30, 50, True)
    sp.add_argument("--until", help="end time (UTC)")
    sp = jadd("visited", cmd_visited, "Systems you have jumped to (or --stations docked at) with visit counts", 0, 30, True)
    sp.add_argument("--stations", action="store_true")
    sp.add_argument("--system", help="only this system")
    jadd("sessions", cmd_sessions, "Play sessions: one per journal file, with duration and event count", 0, 20, True)
    sp = jadd("ledger", cmd_ledger, "Every credit movement the journal records, by category, with running total", 7, 40, True)
    sp.add_argument("--until", help="end time (UTC)")
    sp.add_argument("--category", help="comma separated: trading,bounty,mission,exploration,maintenance,ship,module,...")
    jadd("cargo", cmd_cargo, "What your ship carries now (rebuilt from the journal)")
    sp = jadd("materials", cmd_materials, "Raw, manufactured and encoded materials (rebuilt from the journal)")
    sp.add_argument("--category", choices=["raw", "manufactured", "encoded"])
    sp = jadd("ships", cmd_ships, "Your current ship with modules, and stored ships")
    sp.add_argument("--modules", action="store_true", help="also list the current ship's modules")
    jadd("missions", cmd_missions, "Active missions and recently finished ones", 14)
    jadd("ranks", cmd_ranks, "Ranks with progress, and superpower reputation")
    sp = jadd("stats", cmd_stats, "The game's Statistics snapshot (bank, combat, trading, exploration, ...)")
    sp.add_argument("--section", help="one block, e.g. Bank_Account, Combat, Exploration, Trading")
    jadd("carrier", cmd_carrier, "Your fleet carrier as last reported")
    sp = add("journal-sync", cmd_journal_sync, "Ingest new journal lines into the database (once, or --watch as a service)")
    sp.set_defaults(raw=True)
    sp.add_argument("--journal-dir", help="journal folder (default: $ELITE_JOURNAL_DIR or the Saved Games folder)")
    sp.add_argument("--rebuild", action="store_true", help="re-read every journal file from scratch")
    sp.add_argument("--watch", action="store_true", help="keep running and ingest as the game writes")
    sp.add_argument("--interval", type=float, default=30, help="with --watch: seconds between passes (default 30)")
    sp.add_argument("--log-file", help="with --watch: also append log lines here")
    sp = add("journal-status", cmd_journal_status,
             "Is the database up to date with your journal files? Ingest state, writer access, ingester service")
    sp.add_argument("--journal-dir", help="journal folder (default: $ELITE_JOURNAL_DIR or the Saved Games folder)")
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
