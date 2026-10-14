"""Where a CG supply board (a recce dashboard of kind "cg") searches from: read and change its `target`.

A running server applies the change on its next refresh and restarts the dashboard's Change baselines."""

from . import queries
from .journal import context as journal_context
from .recce import config as rcfg

DEFAULT_NAME = "cg"


def _load(name):
    try:
        cfg = rcfg.load(name)
    except KeyError:
        return None, {"error": f"no dashboard named {name!r} (existing: {', '.join(rcfg.list_names()) or 'none yet'})"}
    except rcfg.ConfigError as e:
        return None, {"error": "invalid dashboard config: " + "; ".join(e.errors)}
    if cfg.get("kind") != "cg":
        return None, {"error": f"{name!r} is not a cg dashboard"}
    return cfg, None


def get_target(name=DEFAULT_NAME):
    cfg, err = _load(name)
    if err:
        return err
    t = cfg["target"]
    return {"dashboard": name, "system": t["system"], "station": t["name"], "refresh_seconds": cfg["refresh_seconds"],
            "tabs": [tab["key"] for tab in cfg["tabs"]]}


def set_target(session, system, station=None, name=DEFAULT_NAME, where=None):
    """
    Point the dashboard at `system` ("current" = follow the commander's journal position). The system must be in the
    database with coordinates, since distances are measured from it. `station` is only the name the page shows for
    the delivery point; without it the old name is kept when the system is unchanged, otherwise the system name is
    used. Only target.system and target.name are touched. `where` replaces the journal lookup (tests).
    """
    cfg, err = _load(name)
    if err:
        return err
    old = dict(cfg["target"])
    follow = str(system).strip().lower() == "current"
    if follow:
        row_name = "current"
        if station is None and old["system"].lower() == "current":
            station = old["name"]
        data = (where or journal_context.where_am_i)()
        if "error" in data or not data.get("system"):
            return {"error": "cannot use 'current': your position is unknown (%s)" % data.get("error", "no system")}
        check = data["system"]
    else:
        check = str(system).strip()
    row = queries.find_system(session, check)
    if row is None:
        return {"error": "system %r is not in the database, so the board could not measure distances from it" % check}
    if row.x is None or row.y is None or row.z is None:
        return {"error": "%s has no coordinates in the database yet, so distances cannot be measured from it" % row.name}
    if not follow:
        row_name = row.name
    if station:
        label = station
    elif old["system"].lower() == row_name.lower() and old["name"]:
        label = old["name"]
    else:
        label = row.name
    rcfg.update(name, {"target": {"name": label, "system": row_name}})
    return {"dashboard": name, "system": row_name, "resolved_system": row.name, "station": label,
            "previous": {"system": old["system"], "station": old["name"]},
            "changed": old["system"].lower() != row_name.lower(), "applied_within_seconds": cfg["refresh_seconds"],
            "note": "open pages update by themselves; the Change baselines restart when the target moves"}
