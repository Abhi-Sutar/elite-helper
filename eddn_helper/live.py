"""Query wrappers that fall back to EDSM when local EDDN data is missing or stale."""

from sqlalchemy.exc import DBAPIError

from . import external, queries

READ_ONLY_MSG = "database connection is read-only (or write failed); live lookup cannot store results"


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
