"""
Plain-dict entry points for the CLI and the MCP tools. Journal-backed ones take a database session first (like
journal/views.py) and carry `journal_freshness`; the pure ones (pasted Loadout JSON, max cargo, data status) do not.
"""

import functools

from ..journal import files as jfiles
from ..journal.queries import fresh_query, no_commander, resolve_commander
from ..journal.views import _latest
from . import calc, data, model, optimize, suggest


def data_status(refresh=False):
    try:
        return {"ok": True, **data.load(refresh_now=refresh).status()}
    except data.CatalogueError as e:
        return {"ok": False, "error": str(e), "cache": str(data.cache_path())}


def _cat():
    return data.load()


def _safe(fn):
    """Turn a missing/unreachable catalogue into an error reply instead of an exception."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except data.CatalogueError as e:
            return {"found": False, "error": str(e)}
    return wrapper


def _swaps(b, swaps, cat):
    """Apply {slot: item or empty} what-ifs; slot names are the journal's (see the module list in build-show)."""
    return b.with_changes({s: (i or None) for s, i in swaps.items()}, cat) if swaps else b


def describe(b, cat, cargo=None):
    out = calc.summary(b, cargo)
    out["modules"] = [{"slot": f.slot, "item": f.item, "kind": f.kind, "on": f.on, "priority_group": f.priority + 1,
                       "power": round(f.stat("power"), 3) if f.stat("power") else 0, "mass": round(f.stat("mass"), 2),
                       "engineering": f.engineering} for f in b.fitted if f.kind != "bulkhead"]
    free = b.free_slots()
    out["free_internal_slots"] = [{"size": s["size"], "restricted_to": sorted(s["eligible"]) if s["eligible"] else None}
                                  for s in free["internal"]]
    out["free_hardpoints"] = sorted(free["hardpoints"], reverse=True)
    out["catalogue_age_days"] = round(cat.age_days, 1) if cat.age_days is not None else None
    return out


def _journal_build(session, commander, ship_id, cat):
    fid, name = resolve_commander(session, commander)
    if fid is None:
        return None, no_commander(commander), None
    rows = _latest(session, fid, ["Loadout"], where=(lambda d: d.get("ShipID") == ship_id) if ship_id is not None else None)
    if not rows:
        what = f"ship {ship_id}" if ship_id is not None else "any ship"
        return None, {"found": False, "commander": name,
                      "error": f"no Loadout in the journals for {what} (the game writes one when you board a ship)"}, None
    d = rows[0].data
    try:
        return model.from_loadout(d, cat), {"commander": name, "ship_id": d.get("ShipID"), "ship_name": d.get("ShipName"),
                                              "as_of": jfiles.iso(rows[0].ts)}, d
    except model.BuildError as e:
        return None, {"found": False, "commander": name, "error": str(e)}, None


@fresh_query
@_safe
def show(session, commander=None, ship_id=None, swaps=None, cargo=None):
    """Mass, jump range, power by priority group and free slots of your current (or a given) ship, as last fitted."""
    cat = _cat()
    b, meta, raw = _journal_build(session, commander, ship_id, cat)
    if b is None:
        return meta
    out = {"found": True, **meta, "build": describe(_swaps(b, swaps, cat), cat, cargo)}
    out["journal_reported"] = {"unladen_mass": raw.get("UnladenMass"), "max_jump_range": raw.get("MaxJumpRange"),
                               "cargo_capacity": raw.get("CargoCapacity")}
    if not swaps:
        r = out["build"]
        out["check"] = {"unladen_mass_diff": _diff(r["unladen_mass"], raw.get("UnladenMass")),
                        "jump_range_diff": _diff(r["jump_range_unladen"], raw.get("MaxJumpRange")),
                        "cargo_diff": _diff(r["cargo_capacity"], raw.get("CargoCapacity"))}
    return out


def _diff(mine, theirs):
    return None if mine is None or theirs is None else round(mine - theirs, 2)


@fresh_query
@_safe
def suggest_for(session, commander=None, ship_id=None, role=None, replace_slots=None, swaps=None):
    """What can still be added to the ship: candidates per free slot that the power plant can run."""
    cat = _cat()
    b, meta, _ = _journal_build(session, commander, ship_id, cat)
    if b is None:
        return meta
    b = _swaps(b, swaps, cat)
    return {"found": True, **meta, **suggest.suggest_additions(b, cat, role, replace_slots or ())}


@fresh_query
@_safe
def priorities(session, commander=None, ship_id=None, swaps=None):
    """Power priority groups against what the ship needs to stay usable, and what gets shed first when overdrawn."""
    cat = _cat()
    b, meta, _ = _journal_build(session, commander, ship_id, cat)
    if b is None:
        return meta
    return {"found": True, **meta, **suggest.priority_report(_swaps(b, swaps, cat))}


@fresh_query
@_safe
def jump(session, commander=None, ship_id=None, keep=None, objective="laden", assume_engineering=True, top=5):
    """Best frame shift drive / Guardian booster / fuel tank for jump range, keeping everything else as fitted."""
    cat = _cat()
    b, meta, _ = _journal_build(session, commander, ship_id, cat)
    if b is None:
        return meta
    return {"found": True, **meta, **optimize.optimize_jump(b, cat, keep or (), objective, assume_engineering, top=top)}


def check(loadout, swaps=None, cargo=None, with_priorities=True):
    """A pasted Loadout (journal event, Coriolis or EDSY export, as text or parsed JSON): range, mass and power budget."""
    try:
        cat = _cat()
        b = _swaps(model.from_loadout(loadout, cat), swaps, cat)
    except (model.BuildError, data.CatalogueError) as e:
        return {"found": False, "error": str(e)}
    out = {"found": True, "build": describe(b, cat, cargo)}
    if with_priorities:
        rep = suggest.priority_report(b)
        out["priorities"] = {k: rep[k] for k in ("problems", "retracted", "deployed")}
    return out


def max_cargo(ship=None, pad=None, reserve=2):
    try:
        return optimize.max_cargo(_cat(), ship, pad, reserve)
    except data.CatalogueError as e:
        return {"error": str(e)}
