"""MCP (stdio) server exposing the same queries as the CLI. Run: python -m eddn_helper.mcp_server"""

from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import cg_target, database, live, migrate, queries
from .journal import context as journal_context
from .journal import files as jfiles
from .journal import income as journal_income
from .journal import ingest as journal_ingest
from .journal import queries as journal_queries
from .journal import views as journal_views
from .recce import tools as recce_tools

mcp = MCPServer(
    "elite-helper",
    instructions=(
        "Elite Dangerous market and faction data collected from EDDN into a local database. "
        "Data only covers what players uploaded while the listener ran; always check the age fields "
        "and use data_status to see whether the listener is running. Use live=True to fall back to EDSM "
        "for systems/stations the database lacks. Commodity names are matched case/space-insensitively "
        "(e.g. 'Liquid Oxygen'). 'buy_price' is what a commander pays the station; 'sell_price' is what "
        "the station pays the commander. "
        "Two tools read the commander's own local journal files instead of the database: where_am_i (current "
        "system/station/ship - the answer to 'where am I' or 'what is near me') and credit_rate (credits/hour "
        "and income by stream). Use where_am_i first when a question depends on the player's location. The "
        "rest of the journal tools (journal_search, travel_log, visited_*, ledger, cargo, materials, ships, missions, "
        "reputation, commander_stats, carrier_status, play_sessions) read the commander's history from the database, "
        "ingesting new journal lines on demand; every reply has journal_freshness - if db_behind_journal is true the "
        "answer may be missing the latest play (see journal_sync_status). "
        "The dashboard_* tools manage local live dashboards: kind \"recce\" (the default, systems with faction states) and "
        "kind \"cg\" (a community-goal supply board: large-pad stations selling a commodity near a delivery system); "
        "cg_board_target / cg_board_set_target read and move the search location of a cg dashboard."))


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = database.make_engine()
        migrate.ensure_current(_engine)
    return _engine


def _run(fn, *args, **kwargs):
    try:
        engine = _get_engine()
    except migrate.SchemaError as e:
        return {"error": str(e)}
    with database.session_scope(database.make_session_factory(engine)) as session:
        return fn(session, *args, **kwargs)


def _run_fetching(fetch, fn, *args, **kwargs):
    """
    Like _run, but when `fetch` is true the call runs on the writer connection (ELITE_HELPER_INGEST_DATABASE_URL) so
    data fetched from EDSM can be stored; the normal reader connection cannot write. Without that variable the call
    still runs, and a fetch reports that the database is read-only.
    """
    if not fetch:
        return _run(fn, *args, **kwargs)
    try:
        _get_engine()
    except migrate.SchemaError as e:
        return {"error": str(e)}
    session = live.writer_session()
    if session is None:
        return _run(fn, *args, **kwargs)
    with session:
        try:
            return fn(session, *args, **kwargs)
        finally:
            session.rollback()


@mcp.tool()
def data_status() -> dict:
    """Row counts, newest data timestamps, and whether the EDDN listener is currently running."""
    return _run(queries.data_status)


@mcp.tool()
def system_info(system: str, live_lookup: bool = False) -> dict:
    """System details plus every faction's influence %, state, pending/recovering states and conflicts."""
    return _run_fetching(live_lookup, live.system_info, system, live_lookup)


@mcp.tool()
def system_economy(system: str, live_lookup: bool = False) -> dict:
    """
    A system's economy and second economy (e.g. Extraction, Refinery, Industrial, HighTech, Agri) plus its stations
    with type and station economies. Current snapshot only; live_lookup refreshes a missing/stale system from EDSM and
    lists EDSM-only stations under stations_live.
    """
    return _run_fetching(live_lookup, live.system_economy, system, live_lookup)


@mcp.tool()
def find_systems(economies: list[str] | None = None, economy_scope: str = "system", include_second: bool = True,
                 faction_state: str | None = None, state_in: str = "any", controlling_only: bool = False,
                 near: str | None = None, ly: float | None = None, min_population: int = 0,
                 demand_commodities: list[str] | None = None, include_carrier_markets: bool = False,
                 max_age_hours: float | None = None, limit: int = 20,
                 exclude_economies: list[str] | None = None, exclude_scope: str = "any",
                 exclude_states: list[str] | None = None, exclude_states_in: str = "any",
                 exclude_systems: list[str] | None = None, max_population: int | None = None,
                 allegiance: list[str] | None = None, government: list[str] | None = None,
                 security: list[str] | None = None, in_conflict: bool | None = None,
                 conflict_type: list[str] | None = None, faction_name: str | None = None,
                 min_influence: float | None = None, max_influence: float | None = None,
                 happiness: list[str] | None = None, state_trend: int | None = None, min_stations: int = 0,
                 station_types: list[str] | None = None, sort: str = "distance",
                 min_faction_stations: int = 0, player_faction: bool | None = None,
                 state_since_hours: float | None = None, refresh_missing: bool = False,
                 refresh_limit: int = 10, refresh_stations: bool | None = None) -> dict:
    """
    Find systems matching ALL given criteria, nearest to `near` first (else most populated first).
    economies: any of these, e.g. ["Extraction", "Refinery"]; economy_scope: "system" (system economy, plus second
    economy when include_second), "station" (any station's economy) or "any".
    faction_state: e.g. "Expansion"; state_in: "any" (main state or any active/pending/recovering list), "state",
    "active", "pending" or "recovering"; controlling_only: only the controlling faction. Results show where the state
    was found and the raw trend of pending/recovering states.
    demand_commodities: keep systems with a station currently demanding any of them (e.g. ["bertrandite", "coltan"]).
    Exclusions: exclude_economies rejects a system when its economy, second economy or (exclude_scope "any", the
    default, or "station") any station's economy is one of them; exclude_states rejects systems where ANY faction has
    one of those states (exclude_states_in as state_in); exclude_systems drops names.
    System filters: max_population, allegiance, government, security (any-of lists), in_conflict (true/false),
    conflict_type (War, CivilWar, Election), min_stations, station_types (e.g. ["coriolis", "outpost"]).
    Faction filters, which must all hold for the same faction: faction_state/state_in, state_trend (raw number),
    controlling_only, faction_name, min_influence/max_influence (percent), happiness (any-of list).
    player_faction: true keeps only player factions, false only NPC factions (applies to the same faction as the other
    faction filters; every faction in the results carries is_player). Player status comes from EDSM and is stored when
    `refresh-stations` / `find-systems --refresh-stations` (or a live lookup) fetches a system; a faction not yet checked
    has is_player null and is kept, not rejected - see faction_players_unknown in the reply.
    state_since_hours: with faction_state, keep only factions whose CURRENT run of that state began within the last N
    hours (worked out from the stored faction history; state_started_at and state_hours_ago are returned, and
    start_is_lower_bound true means history begins inside the run so it may be older).
    refresh_missing: fetch from EDSM only what this search needs and the result systems lack, store it, and search
    again. With player_faction, that is the player status of the matching factions that have none (one request each;
    a faction controlling several result systems is looked up once). Stations (two requests per system) are fetched
    only when the search uses station filters (min_stations, station_types, min_faction_stations) or
    refresh_stations is true; refresh_stations=false forces them off. At most refresh_limit systems per call
    (default 10, max 30; EDSM allows 1 request per second); the reply lists `refreshed` systems and
    `refresh_pending`, how many result systems still lack data - call again to continue.
    Needs the server's ELITE_HELPER_INGEST_DATABASE_URL (a writer connection); otherwise `refreshed` shows the
    read-only error.
    sort: "distance" (default), "population", "influence", "freshest" or "name".
    Stations come from EDSM data stored by the CLI command `refresh-stations` (incl. planetary outposts and surface
    settlements, each with its controlling faction and controlled_by_matching_faction) merged with market uploads;
    stations_known false means EDSM has not been asked for that system - station filters (min_stations, station_types,
    min_faction_stations) never reject such systems. min_faction_stations: the matching faction controls at least N.
    Example - agricultural or industrial systems with a faction pending Expansion and no extraction/refinery anywhere:
    economies=["Agri", "Industrial"], faction_state="Expansion", state_in="pending",
    exclude_economies=["Extraction", "Refinery"].
    Example - Industrial systems of at least 1,000,000 population whose controlling player faction started Expansion in
    the last 48 hours: economies=["Industrial"], min_population=1000000, controlling_only=true, player_faction=true,
    faction_state="Expansion", state_in="active", state_since_hours=48.
    Snapshot of systems the listener has seen; check factions_age_hours.
    """
    def search(session):
        return queries.find_systems(
            session, economies, economy_scope, include_second, faction_state, state_in, controlling_only, near, ly,
            min_population, demand_commodities, include_carrier_markets, max_age_hours, limit,
            exclude_economies=exclude_economies, exclude_scope=exclude_scope, exclude_states=exclude_states,
            exclude_states_in=exclude_states_in, exclude_systems=exclude_systems, max_population=max_population,
            allegiance=allegiance, government=government, security=security, in_conflict=in_conflict,
            conflict_type=conflict_type, faction_name=faction_name, min_influence=min_influence,
            max_influence=max_influence, happiness=happiness, state_trend=state_trend, min_stations=min_stations,
            station_types=station_types, sort=sort, min_faction_stations=min_faction_stations,
            player_faction=player_faction, state_since_hours=state_since_hours)

    if not refresh_missing:
        return _run(search)
    if refresh_stations is None:
        refresh_stations = bool(min_stations or station_types or min_faction_stations)
    return _run_fetching(True, lambda session: live.refresh_missing(
        session, lambda: search(session), max(0, min(refresh_limit, 30)), 168, player_faction is not None,
        need_stations=refresh_stations))


@mcp.tool()
def faction_stations(faction: str, system: str | None = None, near: str | None = None, ly: float | None = None,
                     station_types: list[str] | None = None, limit: int = 100) -> dict:
    """
    Stations (incl. planetary outposts and surface settlements) controlled by a faction, from the EDSM station data
    stored with `refresh-stations`; only systems that were fetched are covered. Filter by system, distance or
    station_types (e.g. ["settlement"], ["surface"], ["coriolis"]).
    """
    return _run(queries.faction_stations, faction, system, near, ly, station_types, limit)


@mcp.tool()
def faction_history(system: str, days: int = 30, faction: str | None = None, max_points: int = 500) -> dict:
    """
    Stored observations of each faction in a system: influence %, main state, happiness, and the active, pending
    and recovering states (pending/recovering with their trend). present=false marks the faction leaving.
    Long series are thinned per faction to about max_points but always keep every change.
    """
    return _run(queries.faction_history, system, days, faction, max_points)


@mcp.tool()
def faction_timeline(system: str, faction: str, days: int = 30, max_points: int = 500) -> dict:
    """
    Chart-ready history of one faction in one system: the influence series plus intervals (start, last_seen,
    ended_at) for its main state, happiness, and every active / pending / recovering state, and any 'left' events.
    """
    return _run(queries.faction_timeline, system, faction, days, max_points)


@mcp.tool()
def faction_presence(faction: str) -> dict:
    """
    Every system where a named faction is present, with influence, state, data age, and how many history rows
    (and since when) exist there - use it to choose systems for faction_history / faction_timeline.
    """
    return _run(queries.faction_presence, faction)


@mcp.tool()
def station_market(station: str, system: str | None = None, live_lookup: bool = False) -> dict:
    """Full commodity table (prices, stock, demand) for one station. system is needed for live_lookup."""
    return _run(live.station_market, station, system, live_lookup)


@mcp.tool()
def price_history(station: str, commodity: str, system: str | None = None, days: int = 30,
                  bucket_minutes: int | None = None, max_points: int = 500,
                  all_commodities: bool = False, max_commodities: int = 25,
                  resample_minutes: int | None = None) -> dict:
    """
    Price and amount time series at a station (fleet carriers by callsign). commodity is one name, a comma
    list, or 'all' (busiest first, capped at max_commodities unless all_commodities=True). Each point has
    buy_price (station sells; needs stock), sell_price (station buys; needs demand; 0 = side inactive),
    mean_price (galactic average), stock (supply), demand and their brackets. History kept 30 days (a station's last report is kept for ever).
    Stored rows are change points (the value at any time is the latest row at or before it; state_at_start gives the
    state when the window opens); `updates` is the true number of EDDN updates and `rows` the stored change rows.
    Series longer than max_points are summarised into time buckets with min/avg/max of prices, stock and demand
    (pass bucket_minutes to choose the size); resample_minutes gives evenly spaced samples carrying the last state
    forward, each with the number of updates in that step.
    """
    return _run(queries.price_history, station, commodity, system, days, bucket_minutes, max_points,
                all_commodities, max_commodities, resample_minutes)


@mcp.tool()
def station_traffic(station: str, system: str | None = None, days: int = 7, bucket_minutes: int = 60) -> dict:
    """How often a station (or fleet carrier callsign) is reported to EDDN: updates and distinct uploaders per time bucket, updates per hour, busiest period."""
    return _run(queries.station_traffic, station, system, days, bucket_minutes)


@mcp.tool()
def system_traffic(system: str, days: int = 7, bucket_minutes: int = 60) -> dict:
    """How often a system's factions are reported to EDDN (journal messages with faction data), per time bucket."""
    return _run(queries.system_traffic, system, days, bucket_minutes)


@mcp.tool()
def busiest_stations(days: int = 1, limit: int = 20, include_carriers: bool = True) -> dict:
    """Leaderboard of the most frequently reported stations: updates, updates per hour, distinct uploaders, price rows written, share of all updates."""
    return _run(queries.busiest_stations, days, limit, include_carriers)


@mcp.tool()
def busiest_systems(days: int = 1, limit: int = 20) -> dict:
    """Leaderboard of systems whose factions are reported most often to EDDN."""
    return _run(queries.busiest_systems, days, limit)


@mcp.tool()
def station_activity(station: str, system: str | None = None, days: int = 30) -> dict:
    """How many market updates are stored for a station (total, first/last, per day) - e.g. a fleet carrier by callsign."""
    return _run(queries.station_activity, station, system, days)


@mcp.tool()
def find_commodity(commodity: str, mode: str = "sell", near_system: str | None = None,
                   max_ly: float | None = None, min_amount: int = 0, max_age_hours: float = 48,
                   include_carriers: bool = True, limit: int = 15) -> dict:
    """Best stations to 'sell' a commodity (highest price, needs demand) or 'buy' it (lowest price, needs stock)."""
    if mode not in ("sell", "buy"):
        return {"error": "mode must be 'sell' or 'buy'"}
    return _run(queries.find_commodity, commodity, mode, near_system, max_ly, min_amount,
                max_age_hours, include_carriers, limit)


@mcp.tool()
def best_trades(origin_system: str, max_ly: float = 30, cargo_tons: int | None = None,
                max_age_hours: float = 24, min_amount: int = 100, include_carriers: bool = False,
                limit: int = 10) -> dict:
    """Top buy/sell commodity pairs among markets within max_ly of a system, by profit per ton (or total if cargo_tons set)."""
    return _run(queries.best_trades, origin_system, max_ly, cargo_tons, max_age_hours, min_amount,
                include_carriers, limit)


@mcp.tool()
def where_am_i(enrich: bool = False, live_lookup: bool = False) -> dict:
    """
    The commander's current system, station, ship and credits, read from the local Elite Dangerous journal (no
    database needed). Position is the last known one: check game_running and last_event_age_seconds - when the game is
    not running it is where you were when you quit. credits is the balance at login (credits_as_of); status.balance
    is live while the game runs. enrich=True adds the EDDN database's view of the system (eddn_system: factions,
    states) and station market age (eddn_market); live_lookup also refreshes a missing/stale system from EDSM.
    """
    data = journal_context.where_am_i()
    if enrich and "error" not in data:
        res = _run(journal_context.enrich, data, live_lookup)
        if "error" in res and "system" not in res:   # database unavailable: keep the journal answer
            data["enrich_error"] = res["error"]
            return data
        return res
    return data


@mcp.tool()
def credit_rate(since_hours: float | None = None, session_only: bool = False, max_gap_minutes: float | None = None,
                streams: list[str] | None = None, include_crew: bool = False, source: str = "auto") -> dict:
    """
    Credits per hour from your journals, using active playtime stitched across sessions (time between sessions never
    counts; max_gap_minutes also drops AFK gaps inside a session). Streams: trading, bounty, mission, exploration,
    carrier, carrier_market, maintenance, rebuy, crew_wages - each with earned/spent/net, plus a per-commodity trading
    table, maintenance breakdown and per-session rows. since_hours limits to the last N hours; session_only uses just
    the newest journal. Caveats: carrier_market is an approximation from carrier balance snapshots (it also absorbs
    upkeep); mission commodity rewards are not priced.
    source: "auto" (default) answers from the ingested journal database (new journal lines are ingested first) and
    falls back to reading the files if the database is unavailable or cannot be brought up to date; "db" or "files"
    force one. The reply says which was used.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)) if since_hours else None
    if source not in ("auto", "db", "files"):
        return {"error": "source must be 'auto', 'db' or 'files'"}
    reason = None
    if source in ("auto", "db"):
        try:
            res = _run(journal_queries.credit_rate, since=since, session_only=session_only,
                       max_gap_minutes=max_gap_minutes, streams=streams, crew_wages=include_crew)
        except Exception as e:      # database unreachable: the files still work
            res = {"error": f"{type(e).__name__}: {e}"}
        fresh = res.get("journal_freshness") or {}
        if source == "db" or ("error" not in res and not fresh.get("db_behind_journal")):
            return res
        reason = res.get("error") or fresh.get("ingest_error") or "database is behind the journal files"
    try:
        paths = jfiles.require_journal_files(jfiles.journal_dir())
    except jfiles.JournalNotFound as e:
        return {"error": str(e)}
    if session_only:
        paths = paths[-1:]
    result = journal_income.credit_rate(paths, streams, since, max_gap_minutes, include_crew)
    result["source"] = "files"
    if reason:
        result["fallback_reason"] = reason
    return result


def _journal(fn, **kwargs):
    """Run a database-backed journal query; they ingest new journal lines first and report journal_freshness."""
    return _run(fn, **kwargs)


@mcp.tool()
def journal_search(event: str | None = None, system: str | None = None, station: str | None = None,
                   text: str | None = None, days: int | None = None, since: str | None = None, until: str | None = None,
                   commander: str | None = None, limit: int = 50, include_data: bool = False,
                   oldest_first: bool = False) -> dict:
    """
    Search your whole event history (everything the game ever wrote to the journal, ingested into the database).
    event: one name or comma list (Docked, FSDJump, MarketSell, Scan, ...); system / station: exact name; text:
    case-insensitive text anywhere in the event; days or since/until (UTC, YYYY-MM-DD[THH:MM:SS]). Newest first.
    include_data adds the raw event JSON (large). New journal lines are ingested before answering; check
    journal_freshness.db_behind_journal in the reply.
    """
    return _journal(journal_queries.journal_events, event=event, system=system, station=station, text=text, days=days,
                    since=since, until=until, commander=commander, limit=limit, include_data=include_data,
                    oldest_first=oldest_first)


@mcp.tool()
def travel_log(days: int = 30, since: str | None = None, until: str | None = None, commander: str | None = None,
               limit: int = 50) -> dict:
    """FSD and carrier jumps, newest first: system, jump distance, fuel, minutes stayed; plus total jumps / light years / distinct systems in the window."""
    return _journal(journal_queries.travel_log, days=days, since=since, until=until, commander=commander, limit=limit)


@mcp.tool()
def visited_systems(days: int | None = None, since: str | None = None, system: str | None = None,
                    commander: str | None = None, limit: int = 30) -> dict:
    """Systems you have jumped to, most recent first, with visit counts and first/last visit (give system to check one)."""
    return _journal(journal_queries.visited_systems, days=days, since=since, system=system, commander=commander, limit=limit)


@mcp.tool()
def visited_stations(days: int | None = None, since: str | None = None, system: str | None = None,
                     commander: str | None = None, limit: int = 30) -> dict:
    """Stations, outposts and carriers you have docked at, most recent first, with docking counts."""
    return _journal(journal_queries.visited_stations, days=days, since=since, system=system, commander=commander,
                    limit=limit)


@mcp.tool()
def play_sessions(days: int | None = None, since: str | None = None, commander: str | None = None, limit: int = 20) -> dict:
    """Play sessions (one per journal file), newest first: start, end, hours, event count, whether the game closed cleanly."""
    return _journal(journal_queries.sessions, days=days, since=since, commander=commander, limit=limit)


@mcp.tool()
def ledger(days: int = 7, since: str | None = None, until: str | None = None, category: str | None = None,
           commander: str | None = None, limit: int = 50) -> dict:
    """
    Every credit movement the journal records (trades, bounties, missions, exploration, repairs, crew wages, ships,
    modules, fines, community goals...), newest first, with per-category earned/spent/net and a running total.
    category: comma list to filter. The total is the sum over the window, not your balance - carrier market sales
    are not journal events.
    """
    return _journal(journal_queries.ledger, days=days, since=since, until=until, category=category, commander=commander,
                    limit=limit)


@mcp.tool()
def cargo(commander: str | None = None) -> dict:
    """What the ship is carrying now: the last Cargo snapshot moved forward through later trades, ejections, mining and transfers."""
    return _journal(journal_views.cargo, commander=commander)


@mcp.tool()
def materials(category: str | None = None, commander: str | None = None) -> dict:
    """Raw / manufactured / encoded materials with counts (last Materials snapshot plus collection, discards, trades, crafting). category: raw, manufactured or encoded."""
    return _journal(journal_views.materials, commander=commander, category=category)


@mcp.tool()
def ships(modules: bool = True, commander: str | None = None) -> dict:
    """The ship you are in (last Loadout: value, rebuy, cargo, jump range, modules with engineering) and the stored fleet with locations."""
    return _journal(journal_views.ships, commander=commander, modules=modules)


@mcp.tool()
def missions(recent_days: int = 14, commander: str | None = None) -> dict:
    """Active missions (faction, reward, expiry, destination), ones past expiry without a result, and those finished in the last recent_days."""
    return _journal(journal_views.missions, commander=commander, recent_days=recent_days)


@mcp.tool()
def reputation(commander: str | None = None) -> dict:
    """Ranks (Combat, Trade, Explore, CQC, Soldier, Exobiologist, Federation, Empire) with progress to the next, and superpower reputation."""
    return _journal(journal_views.reputation, commander=commander)


@mcp.tool()
def commander_stats(section: str | None = None, commander: str | None = None) -> dict:
    """The game's Statistics snapshot (Bank_Account, Combat, Trading, Exploration, Mining, Crafting, ...). section selects one block; omit for all."""
    return _journal(journal_views.commander_stats, commander=commander, section=section)


@mcp.tool()
def carrier_status(commander: str | None = None) -> dict:
    """Your fleet carrier as last reported by the game: fuel, jump range, balance, space usage, crew/services (open carrier management in game to refresh)."""
    return _journal(journal_views.carrier, commander=commander)


@mcp.tool()
def journal_sync_status() -> dict:
    """
    Is the database up to date with the local journal files? state: current | behind (can be ingested now) |
    no_writer (behind and the database connection cannot write - set ELITE_HELPER_INGEST_DATABASE_URL or run
    `journal-sync`) | no_journals. Also files/events counts, the newest event stored vs the newest journal write, last
    ingest time and error, and whether the optional ingester service / scheduled task is running. Journal tools ingest
    on demand, so this is only for diagnosing.
    """
    return _run(journal_ingest.journal_status, None)


# ---- Recce dashboards: a local web page that refreshes by itself (see README, "Recce dashboard") ----------------------

@mcp.tool()
def dashboard_create(config: dict) -> dict:
    """
    Create or replace a dashboard and return its name and URL (the local server is started if needed and reused if
    already running). config.kind picks the kind: "recce" (default, described below) or "cg", a community-goal supply
    board with keys: name, title, refresh_seconds (min 5, default 30), target {name: delivery station, system: system
    name or "current"}, ship {name, laden_ly, unladen_ly, cargo}, station_types (default the large pads), min_stock,
    max_age_hours, lookup_ls (EDSM distance-to-star and pad-type lookups), stale_after_minutes and tabs [{key,
    commodity, label, cg_price, max_jumps or max_ly, min_stock, station_types, max_age_hours}] (at least one tab).
    The rest of this text describes the recce kind. config keys, all optional except name: name (lowercase slug), title, refresh_seconds
    (min 5, default 20), origin (a system name or "current" for the journal position), radius_ly, filters {economy
    (name, list, or null), min_population, controlling_only, player_faction_only, faction_state (e.g. "Expansion"),
    state_in (any|pending|active|recovering), kinds (with state_in "any": which of pending, active, recovering, other
    to show)}, columns (ordered from: system, faction, economy, pending_for, state, last_update, distance, population,
    influence, expansion_chance), sort {by: state|pending_for|system|faction|distance|population|influence|last_update, order:
    asc|desc}, highlight_rules [{when: active_for_under_hours|pending_for_over_hours, hours}], live_fetch {enabled,
    max_requests (at most 10 EDSM requests per refresh)}, expansion_estimate (true/false: estimate how likely each pending
    Expansion is to go ahead, shown in the expansion_chance column), stale_after_minutes. Missing keys take the defaults, which
    are the Agriculture / Expansion pending-or-active dashboard named "recce". Invalid configs come back as an error
    listing every problem.
    """
    return recce_tools.create(config)


@mcp.tool()
def dashboard_update(name: str, patch: dict) -> dict:
    """
    Merge changes into an existing dashboard (objects merge key by key, lists are replaced, null clears an optional
    filter) and return its URL. Browsers that have the dashboard open reload it live.
    """
    return recce_tools.update(name, patch)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def dashboard_list() -> dict:
    """List dashboards with their URLs and whether the local Recce server is running. Does not start anything."""
    return recce_tools.list_dashboards()


@mcp.tool()
def dashboard_open(name: str) -> dict:
    """Start the local Recce server if it is not running (reuse it if it is), open the dashboard in the browser, and return its URL."""
    return recce_tools.open_dashboard(name)


@mcp.tool()
def dashboard_recentre(name: str) -> dict:
    """
    Anchor a dashboard on the commander's current system (from the journal) and return its URL, the new origin and the
    previous one. Dashboards stay anchored to the system they were created around; use this to move them. Open
    browsers update by themselves.
    """
    return recce_tools.recentre(name)


@mcp.tool()
def dashboard_delete(name: str) -> dict:
    """Delete a dashboard's saved config. Open browsers show that it no longer exists."""
    return recce_tools.delete(name)


# ---- cg dashboards: where they search from -----------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def cg_board_target(dashboard: str = "cg") -> dict:
    """A cg dashboard's current search location (system and delivery station name), refresh interval and tabs."""
    return cg_target.get_target(dashboard)


@mcp.tool()
def cg_board_set_target(system: str, station: str | None = None, dashboard: str = "cg") -> dict:
    """
    Move a cg dashboard's search location (same as dashboard_update with a target patch, plus a check that the system
    is in the database). Stations are listed by distance from `system` (laden jumps). system: an exact system name, or
    "current" to follow the commander's journal position. station: optional name of the delivery station, shown in the
    page subtitle only. Open pages update by themselves and the Change baselines restart when the system changes.
    Unknown systems, or ones without coordinates, are rejected and nothing is changed.
    """
    return _run(cg_target.set_target, system, station, dashboard)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
