"""Query wrappers that fall back to EDSM when local EDDN data is missing or stale."""

from sqlalchemy.exc import DBAPIError

from . import config, database, external, queries

READ_ONLY_MSG = "database connection is read-only (or write failed); live lookup cannot store results"

_writer_engines = {}


def writer_session():
    """
    A session on the writer database named by ELITE_HELPER_INGEST_DATABASE_URL, for EDSM fetches made from the MCP
    server (whose normal connection is the read-only reader role); None when that variable is not set.
    The caller closes it.
    """
    url = config.ingest_database_url()
    if not url:
        return None
    if url not in _writer_engines:
        _writer_engines[url] = database.make_engine(url)
    return database.make_session_factory(_writer_engines[url])()


def refresh_system_stations(session, name, id64=None):
    """Fetch one system's stations (and its factions' player status) from EDSM into the database.
    Returns (count_or_None, error_message_or_None)."""
    try:
        return external.refresh_stations(session, name, id64), None
    except external.LiveLookupError as e:
        return None, str(e)
    except DBAPIError:
        session.rollback()
        return None, READ_ONLY_MSG


def refresh_missing(session, search, limit, stations_max_age_hours=168, need_players=False, log=None):
    """
    Run `search()` (a find_systems call returning its result dict), fetch from EDSM what the result systems lack
    (stations never fetched or older than `stations_max_age_hours`; with `need_players`, systems with a matching
    faction whose player status is unknown), at most `limit` systems, and search again. `log(line)` reports each fetch.
    Returns the final result with `refreshed`: [{system, stations, error}] and `refresh_pending`: result systems still
    lacking data because the limit was reached.
    """
    res, tried, done = search(), set(), []

    def lacking(r):
        return (not r["stations_known"] or (r["stations_fetched_age_hours"] or 0) > stations_max_age_hours
                or (need_players and any(f.get("is_player") is None for f in r["matching_factions"])))
    budget = limit
    while "error" not in res and budget > 0:
        todo = [r for r in res["results"] if r["system"] not in tried and lacking(r)][:budget]
        if not todo:
            break
        for r in todo:
            tried.add(r["system"])
            budget -= 1
            n, err = refresh_system_stations(session, r["system"], r["id64"])
            done.append({"system": r["system"], "stations": n, "error": err or (None if n is not None
                                                                              else "not found at EDSM")})
            if log:
                log(f"refreshed stations of {r['system']}: "
                    + (f"{n} stored" if err is None and n is not None else err or "not found at EDSM"))
            if err:
                budget = 0
                break
        res = search()
    if "error" not in res:
        res["refreshed"] = done
        res["refresh_pending"] = sum(1 for r in res["results"] if r["system"] not in tried and lacking(r))
    return res


def system_info(session, name, live=False, max_age_hours=48):
    """Return queries.system_info(), fetching from EDSM first if `live` and data is missing/stale."""
    res = queries.system_info(session, name)
    stale = not res["found"] or res.get("factions_age_hours") is None \
        or res["factions_age_hours"] > max_age_hours
    if live and stale:
        try:
            if external.fetch_system(session, name):
                res = queries.system_info(session, name)
        except external.LiveLookupError as e:
            res["live_error"] = str(e)
        except DBAPIError:
            session.rollback()
            res["live_error"] = READ_ONLY_MSG
    return res


def system_economy(session, name, live=False, max_age_hours=48):
    """queries.system_economy(); with `live`, refresh a missing/stale system from EDSM and list EDSM-only stations."""
    res = queries.system_economy(session, name)
    stale = not res["found"] or res.get("info_age_hours") is None or res["info_age_hours"] > max_age_hours
    if not live or not stale:
        return res
    try:
        if external.fetch_system(session, name):
            res = queries.system_economy(session, name)
        if res["found"]:
            known = {s["station"].lower() for s in res["stations"]}
            res["stations_live"] = [s for s in external.fetch_stations(res["system"])
                                    if s["station"].lower() not in known]
    except external.LiveLookupError as e:
        res["live_error"] = str(e)
    except DBAPIError:
        session.rollback()
        res["live_error"] = READ_ONLY_MSG
    return res


def station_market(session, station, system=None, live=False, max_age_hours=48):
    res = queries.station_market(session, station, system)
    stale = not res["found"] or (not res.get("ambiguous") and res.get("age_hours", 0) > max_age_hours)
    if live and stale and system:
        try:
            if external.fetch_market(session, station, system):
                res = queries.station_market(session, station, system)
        except external.LiveLookupError as e:
            res["live_error"] = str(e)
        except DBAPIError:
            session.rollback()
            res["live_error"] = READ_ONLY_MSG
    elif live and stale and not system:
        res["live_error"] = "live market lookup needs the system name"
    return res
