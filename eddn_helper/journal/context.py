"""Where the commander is right now: journal state + Status.json, optionally joined with the EDDN database."""

from datetime import datetime, timezone

from . import files as jfiles
from . import state as jstate
from . import status as jstatus

# A journal that stopped growing this long ago without a Shutdown event probably means the game crashed or was killed.
STALE_AFTER_SECONDS = 15 * 60


def where_am_i(directory=None):
    """
    Last known position and ship from the newest journal, no database needed. `game_running` is true only if that
    journal has no Shutdown event and was written to recently; last_event_age_seconds says how fresh the data is.
    """
    try:
        directory = jfiles.journal_dir(directory)
        state, path = jstate.current_state(directory)
    except jfiles.JournalNotFound as e:
        return {"error": str(e)}
    now = datetime.now(timezone.utc)
    out = state.to_dict()
    out["journal_dir"] = directory
    out["journal_file"] = path.replace("\\", "/").rsplit("/", 1)[-1]
    last = jfiles.parse_timestamp(state.last_event_time) if state.last_event_time else None
    age = round((now - last).total_seconds()) if last else None
    out["last_event_age_seconds"] = age
    if out["game_running"] and age is not None and age > STALE_AFTER_SECONDS:
        out["game_running"] = False
        out["note"] = "journal has no Shutdown event but has been silent for a while - the game likely crashed"
    elif not out["game_running"]:
        out["note"] = "game is not running: this is the last known position"
    live_status = jstatus.read_status(directory, now)
    if live_status and out["game_running"]:
        out["status"] = live_status
    return out


def enrich(session, data, live_lookup=False):
    """Add what the EDDN/EDSM database knows about the current system and station to a where_am_i() result."""
    from .. import live, queries
    if "error" in data or not data.get("system"):
        return data
    sysinfo = live.system_info(session, data["system"], live_lookup)
    data["eddn_system"] = sysinfo
    station = (data.get("station") or {}).get("name")
    if station:
        m = queries.station_market(session, station, data["system"])
        data["eddn_market"] = ({"found": True, "updated_at": m.get("updated_at"), "age_hours": m.get("age_hours"),
                                "commodities": len(m.get("commodities", []))}
                               if m.get("found") and not m.get("ambiguous") else {"found": False})
    return data


def format_where(d):
    """Human-readable lines for a where_am_i() result."""
    if "error" in d:
        return [d["error"]]
    ship = d.get("ship") or {}
    ship_txt = " ".join(x for x in (ship.get("type") or ship.get("key"), f"\"{ship['name']}\"" if ship.get("name") else None,
                                    ship.get("ident")) if x)
    st = d.get("station")
    if d["docked"] and st:
        place = f"docked at {st['name']} ({st.get('type') or '?'})"
    elif d.get("landed"):
        place = "landed" + (f" on {d['body']}" if d.get("body") else "")
    elif d.get("supercruise"):
        place = "in supercruise"
    else:
        place = "in normal space" + (f" near {d['body']}" if d.get("body") else "")
    lines = [f"Commander: {d.get('commander') or '?'}   ({'in game' if d['game_running'] else 'game not running'})",
             f"Ship:      {ship_txt or '?'}",
             f"System:    {d.get('system') or '?'}   {place}"]
    info = d.get("system_info") or {}
    if info:
        lines.append("           " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in info.items()))
    if d.get("star_pos"):
        lines.append("Position:  x={:.2f} y={:.2f} z={:.2f}".format(*d["star_pos"]))
    if st:
        lines.append(f"Station:   {st['name']}  {st.get('economy') or ''}  {st.get('faction') or ''}".rstrip())
    if d.get("credits") is not None:
        bal = (d.get("status") or {}).get("balance")
        lines.append(f"Credits:   {(bal if bal is not None else d['credits']):,} Cr"
                     + ("" if bal is not None else f"  (balance at {d.get('credits_as_of')}, not updated since)"))
    lines.append(f"Last event: {d.get('last_event')} at {d.get('last_event_time')}  "
                 f"({d.get('last_event_age_seconds')}s ago)   [{d.get('journal_file')}]")
    if d.get("note"):
        lines.append(f"Note: {d['note']}")
    if d.get("enrich_error"):
        lines.append(f"EDDN: unavailable ({d['enrich_error']})")
    sysinfo = d.get("eddn_system")
    if sysinfo is not None:
        if sysinfo.get("found"):
            lines.append(f"EDDN: {sysinfo['system']} factions updated {sysinfo.get('factions_age_hours')}h ago, "
                         f"{len(sysinfo.get('factions', []))} factions")
        else:
            lines.append("EDDN: system not in the local database")
    mk = d.get("eddn_market")
    if mk is not None:
        lines.append(f"EDDN market: {mk['commodities']} commodities, {mk['age_hours']}h old" if mk["found"]
                     else "EDDN market: none stored")
    return lines
