"""MCP (stdio) server exposing the same queries as the CLI. Run: python -m eddn_helper.mcp_server"""

from mcp.server.mcpserver import MCPServer

from . import database, live, migrate, queries

mcp = MCPServer(
    "elite-helper",
    instructions=(
        "Elite Dangerous market and faction data collected from EDDN into a local database. "
        "Data only covers what players uploaded while the listener ran; always check the age fields "
        "and use data_status to see whether the listener is running. Use live=True to fall back to EDSM "
        "for systems/stations the database lacks. Commodity names are matched case/space-insensitively "
        "(e.g. 'Liquid Oxygen'). 'buy_price' is what a commander pays the station; 'sell_price' is what "
        "the station pays the commander."))


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


@mcp.tool()
def data_status() -> dict:
    """Row counts, newest data timestamps, and whether the EDDN listener is currently running."""
    return _run(queries.data_status)


@mcp.tool()
def system_info(system: str, live_lookup: bool = False) -> dict:
    """System details plus every faction's influence %, state, pending/recovering states and conflicts."""
    return _run(live.system_info, system, live_lookup)


@mcp.tool()
def system_economy(system: str, live_lookup: bool = False) -> dict:
    """
    A system's economy and second economy (e.g. Extraction, Refinery, Industrial, HighTech, Agri) plus its stations
    with type and station economies. Current snapshot only; live_lookup refreshes a missing/stale system from EDSM and
    lists EDSM-only stations under stations_live.
    """
    return _run(live.system_economy, system, live_lookup)


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
                 min_faction_stations: int = 0) -> dict:
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
    sort: "distance" (default), "population", "influence", "freshest" or "name".
    Stations come from EDSM data stored by the CLI command `refresh-stations` (incl. planetary outposts and surface
    settlements, each with its controlling faction and controlled_by_matching_faction) merged with market uploads;
    stations_known false means EDSM has not been asked for that system - station filters (min_stations, station_types,
    min_faction_stations) never reject such systems. min_faction_stations: the matching faction controls at least N.
    Example - agricultural or industrial systems with a faction pending Expansion and no extraction/refinery anywhere:
    economies=["Agri", "Industrial"], faction_state="Expansion", state_in="pending",
    exclude_economies=["Extraction", "Refinery"].
    Snapshot of systems the listener has seen; check factions_age_hours.
    """
    return _run(queries.find_systems, economies, economy_scope, include_second, faction_state, state_in,
                controlling_only, near, ly, min_population, demand_commodities, include_carrier_markets,
                max_age_hours, limit, exclude_economies=exclude_economies, exclude_scope=exclude_scope,
                exclude_states=exclude_states, exclude_states_in=exclude_states_in, exclude_systems=exclude_systems,
                max_population=max_population, allegiance=allegiance, government=government, security=security,
                in_conflict=in_conflict, conflict_type=conflict_type, faction_name=faction_name,
                min_influence=min_influence, max_influence=max_influence, happiness=happiness,
                state_trend=state_trend, min_stations=min_stations, station_types=station_types, sort=sort,
                min_faction_stations=min_faction_stations)


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
    mean_price (galactic average), stock (supply), demand and their brackets. Kept 30 days.
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
