"""Where the CG supply board (cg_board/) searches from: read and change `target` in its config.json.

The board re-reads its config on every refresh, so a change made here shows up within refresh_seconds."""

import json
import os
from pathlib import Path

from . import queries
from .journal import context as journal_context


def config_path():
    return Path(__file__).resolve().parent.parent / "cg_board" / "config.json"


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError as e:
        raise ValueError("cannot read %s: %s" % (path, e))
    except ValueError as e:
        raise ValueError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(cfg, dict):
        raise ValueError("%s does not hold a JSON object" % path)
    return cfg


def _save(path, cfg):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)  # the board may be reading the file right now: swap it in whole


def get_target(path=None):
    path = Path(path) if path else config_path()
    try:
        cfg = _load(path)
    except ValueError as e:
        return {"error": str(e)}
    t = cfg.get("target") or {}
    return {"system": t.get("system"), "station": t.get("name"), "refresh_seconds": cfg.get("refresh_seconds", 30),
            "tabs": [tab.get("key") for tab in cfg.get("tabs", [])], "config_path": str(path)}


def set_target(session, system, station=None, path=None, where=None):
    """
    Point the board at `system` ("current" = the commander's journal position). The system must be in the database with
    coordinates, since distances are measured from it. `station` is only the name the page shows for the delivery
    point; without it the old name is kept when the system is unchanged, otherwise the system name is used.
    Only target.system and target.name are touched. `where` replaces the journal lookup (tests).
    """
    path = Path(path) if path else config_path()
    try:
        cfg = _load(path)
    except ValueError as e:
        return {"error": str(e)}
    if str(system).strip().lower() == "current":
        data = (where or journal_context.where_am_i)()
        if "error" in data or not data.get("system"):
            return {"error": "cannot use 'current': your position is unknown (%s)" % data.get("error", "no system")}
        system = data["system"]
    row = queries.find_system(session, str(system).strip())
    if row is None:
        return {"error": "system %r is not in the database, so the board could not measure distances from it" % system}
    if row.x is None or row.y is None or row.z is None:
        return {"error": "%s has no coordinates in the database yet, so distances cannot be measured from it" % row.name}

    old = dict(cfg.get("target") or {})
    if station:
        name = station
    elif old.get("system", "").lower() == row.name.lower() and old.get("name"):
        name = old["name"]
    else:
        name = row.name
    cfg["target"] = {**old, "name": name, "system": row.name}
    _save(path, cfg)
    return {"system": row.name, "station": name, "previous": {"system": old.get("system"), "station": old.get("name")},
            "changed": old.get("system", "").lower() != row.name.lower(),
            "applied_within_seconds": cfg.get("refresh_seconds", 30), "config_path": str(path),
            "note": "a running board picks this up on its next refresh; its Change baselines restart when the system changes"}
