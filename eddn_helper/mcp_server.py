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
