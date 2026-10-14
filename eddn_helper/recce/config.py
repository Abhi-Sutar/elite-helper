"""Recce dashboard configs: schema, validation, and one JSON file per dashboard under the config directory."""

import copy
import json
import os
import re
from pathlib import Path

from .. import config as app_config

ENV_DIR = "ELITE_HELPER_RECCE_DIR"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

COLUMNS = ("system", "faction", "economy", "pending_for", "state", "last_update", "distance", "population", "influence",
           "expansion_chance")
SORT_KEYS = ("state", "pending_for", "system", "faction", "distance", "population", "influence", "last_update")
STATE_IN = ("any", "pending", "active", "recovering")
KINDS = ("pending", "active", "recovering", "other")
HIGHLIGHT_WHEN = ("active_for_under_hours", "pending_for_over_hours")
MAX_LIVE_REQUESTS = 10          # hard ceiling on EDSM requests per refresh cycle

DEFAULT_NAME = "recce"

DEFAULT_CONFIG = {
    "name": DEFAULT_NAME,
    "title": "Recce",
    "refresh_seconds": 20,
    "origin": "current",
    "radius_ly": 100,
    "filters": {
        "economy": "Agriculture",
        "min_population": 1000000,
        "controlling_only": True,
        "player_faction_only": True,
        "faction_state": "Expansion",
        "state_in": "any",
        "kinds": ["pending", "active"],
    },
    "columns": ["system", "faction", "economy", "pending_for", "state", "expansion_chance", "last_update"],
    "expansion_estimate": True,
    "sort": {"by": "state", "order": "asc"},
    "highlight_rules": [{"when": "active_for_under_hours", "hours": 48}],
    "live_fetch": {"enabled": True, "max_requests": MAX_LIVE_REQUESTS},
    "stale_after_minutes": 60,
}

TOP_KEYS = tuple(DEFAULT_CONFIG)
FILTER_KEYS = tuple(DEFAULT_CONFIG["filters"])


class ConfigError(ValueError):
    """Invalid dashboard config; `errors` lists every problem, each prefixed with its field path."""

    def __init__(self, errors):
        self.errors = list(errors) if isinstance(errors, (list, tuple)) else [str(errors)]
        super().__init__("; ".join(self.errors))


# ---- locations ---------------------------------------------------------------------------------------------------

def root_dir():
    """Folder holding dashboards/, server.json and server.log (ELITE_HELPER_RECCE_DIR overrides)."""
    env = os.environ.get(ENV_DIR)
    return Path(env) if env else app_config.data_dir() / "recce"


def settings_path():
    return root_dir() / "settings.json"


def settings():
    """Recce-only settings from settings.json ({} when missing or unreadable). Keys: ingest_database_url."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def database_url():
    """Reader URL: ELITE_HELPER_DATABASE_URL wins, else settings.json "database_url", else None (the app default)."""
    import os
    if os.environ.get(app_config.ENV_URL):
        return None                                  # make_engine reads the variable itself
    url = settings().get("database_url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def ingest_url():
    """Writer URL for the limited EDSM fetch: ELITE_HELPER_INGEST_DATABASE_URL wins, else settings.json."""
    url = app_config.ingest_database_url() or settings().get("ingest_database_url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def dashboards_dir():
    return root_dir() / "dashboards"


def path_for(name):
    return dashboards_dir() / f"{name}.json"


# ---- validation --------------------------------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_number(errors, path, v, lo=None, hi=None, integer=False):
    if not _is_num(v) or (integer and (isinstance(v, float) and not v.is_integer())):
        errors.append(f"{path}: must be {'a whole number' if integer else 'a number'}, got {v!r}")
        return
    if lo is not None and v < lo:
        errors.append(f"{path}: must be at least {lo}, got {v}")
    if hi is not None and v > hi:
        errors.append(f"{path}: must be at most {hi}, got {v}")


def _check_bool(errors, path, v):
    if not isinstance(v, bool):
        errors.append(f"{path}: must be true or false, got {v!r}")


def _check_choice(errors, path, v, choices):
    if v not in choices:
        errors.append(f"{path}: must be one of {', '.join(choices)}; got {v!r}")


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate(raw):
    """Fill defaults and check a config of any kind (see kinds.py). Returns the complete config; raises ConfigError
    listing every problem."""
    from . import kinds
    if not isinstance(raw, dict):
        raise ConfigError("config: must be a JSON object")
    return kinds.get(raw.get("kind")).validate(raw)


def validate_recce(raw):
    """The Recce kind's validation. An explicit "kind": "recce" is accepted and dropped (it is the default)."""
    if not isinstance(raw, dict):
        raise ConfigError("config: must be a JSON object")
    raw = {k: v for k, v in raw.items() if k != "kind"}
    errors = []
    for k in raw:
        if k not in TOP_KEYS:
            errors.append(f"{k}: unknown setting (allowed: {', '.join(TOP_KEYS)})")
    for key in ("filters", "live_fetch", "sort"):
        v = raw.get(key)
        if v is not None and not isinstance(v, dict):
            errors.append(f"{key}: must be an object")
    if errors:
        raise ConfigError(errors)
    cfg = _deep_merge(DEFAULT_CONFIG, raw)

    name = cfg["name"]
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"name: must be 1-40 lowercase letters, digits, '-' or '_' (starting with a letter or digit); "
                      f"got {name!r}")
    if not isinstance(cfg["title"], str) or not cfg["title"].strip() or len(cfg["title"]) > 120:
        errors.append("title: must be a non-empty string of at most 120 characters")
    _check_number(errors, "refresh_seconds", cfg["refresh_seconds"], lo=5, hi=86400)
    _check_number(errors, "stale_after_minutes", cfg["stale_after_minutes"], lo=5, hi=10080)
    if not isinstance(cfg["origin"], str) or not cfg["origin"].strip():
        errors.append("origin: must be a system name, or \"current\" to use your position from the journal")
    _check_number(errors, "radius_ly", cfg["radius_ly"], lo=1, hi=1000)
    _check_bool(errors, "expansion_estimate", cfg["expansion_estimate"])

    f = cfg["filters"]
    for k in f:
        if k not in FILTER_KEYS:
            errors.append(f"filters.{k}: unknown filter (allowed: {', '.join(FILTER_KEYS)})")
    eco = f.get("economy")
    if eco is not None and not (isinstance(eco, str) and eco.strip()) and not (
            isinstance(eco, list) and all(isinstance(e, str) and e.strip() for e in eco)):
        errors.append("filters.economy: must be a name, a list of names, or null for any economy")
    _check_number(errors, "filters.min_population", f.get("min_population"), lo=0, integer=True)
    _check_bool(errors, "filters.controlling_only", f.get("controlling_only"))
    _check_bool(errors, "filters.player_faction_only", f.get("player_faction_only"))
    fs = f.get("faction_state")
    if fs is not None and not (isinstance(fs, str) and fs.strip()):
        errors.append("filters.faction_state: must be a state name such as \"Expansion\", or null")
    _check_choice(errors, "filters.state_in", f.get("state_in"), STATE_IN)
    kinds = f.get("kinds")
    if kinds is not None and not (isinstance(kinds, list) and all(k in KINDS for k in kinds)):
        errors.append(f"filters.kinds: must be null or a list of {', '.join(KINDS)}")
    if f.get("state_in") != "any" and f.get("state_in") in STATE_IN and not fs:
        errors.append(f"filters.state_in: {f.get('state_in')!r} needs filters.faction_state")

    cols = cfg["columns"]
    if not isinstance(cols, list) or not cols:
        errors.append(f"columns: must be a non-empty list drawn from {', '.join(COLUMNS)}")
    else:
        for i, c in enumerate(cols):
            if c not in COLUMNS:
                errors.append(f"columns[{i}]: unknown column {c!r} (allowed: {', '.join(COLUMNS)})")
        if len(set(cols)) != len(cols):
            errors.append("columns: a column is listed more than once")

    s = cfg["sort"]
    for k in s:
        if k not in ("by", "order"):
            errors.append(f"sort.{k}: unknown setting (allowed: by, order)")
    _check_choice(errors, "sort.by", s.get("by"), SORT_KEYS)
    _check_choice(errors, "sort.order", s.get("order"), ("asc", "desc"))

    rules = cfg["highlight_rules"]
    if not isinstance(rules, list):
        errors.append("highlight_rules: must be a list (use [] for none)")
    else:
        for i, r in enumerate(rules):
            p = f"highlight_rules[{i}]"
            if not isinstance(r, dict):
                errors.append(f"{p}: must be an object like {{\"when\": \"active_for_under_hours\", \"hours\": 48}}")
                continue
            for k in r:
                if k not in ("when", "hours"):
                    errors.append(f"{p}.{k}: unknown setting (allowed: when, hours)")
            _check_choice(errors, f"{p}.when", r.get("when"), HIGHLIGHT_WHEN)
            _check_number(errors, f"{p}.hours", r.get("hours"), lo=0.1, hi=24 * 365)

    lf = cfg["live_fetch"]
    for k in lf:
        if k not in ("enabled", "max_requests"):
            errors.append(f"live_fetch.{k}: unknown setting (allowed: enabled, max_requests)")
    _check_bool(errors, "live_fetch.enabled", lf.get("enabled"))
    _check_number(errors, "live_fetch.max_requests", lf.get("max_requests"), lo=0, hi=MAX_LIVE_REQUESTS, integer=True)

    if errors:
        raise ConfigError(errors)
    return cfg


def apply_patch(cfg, patch):
    """Merge `patch` into `cfg`: objects merge key by key, everything else (lists included) is replaced, null clears."""
    if not isinstance(patch, dict):
        raise ConfigError("patch: must be a JSON object")
    return _deep_merge(cfg, patch)


def parse_value(text):
    """CLI value: JSON when it parses (numbers, true, null, lists), otherwise the plain string."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def set_path(cfg, dotted, value):
    """Set cfg['a']['b'] from 'a.b'; returns a new dict."""
    keys = dotted.split(".")
    patch = cur = {}
    for k in keys[:-1]:
        cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value
    return apply_patch(cfg, patch)


# ---- files -------------------------------------------------------------------------------------------------------

def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError(f"name: must be 1-40 lowercase letters, digits, '-' or '_'; got {name!r}")
    return name


def exists(name):
    return bool(isinstance(name, str) and NAME_RE.match(name)) and path_for(name).is_file()


def list_names():
    d = dashboards_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if NAME_RE.match(p.stem))


def load(name):
    """The validated config of a stored dashboard. Raises KeyError when missing, ConfigError when invalid."""
    _check_name(name)
    p = path_for(name)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise KeyError(name) from None
    except ValueError as e:
        raise ConfigError(f"{p.name}: not valid JSON ({e})") from None
    if isinstance(raw, dict):
        raw.setdefault("name", name)
    cfg = validate(raw)
    if cfg["name"] != name:
        raise ConfigError(f"name: file {p.name} holds a dashboard named {cfg['name']!r}")
    return cfg


def kind_of(name):
    """The kind named in a stored dashboard file, read without validating it (default "recce"; unreadable -> default)."""
    try:
        raw = json.loads(path_for(_check_name(name)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "recce"
    return raw.get("kind", "recce") if isinstance(raw, dict) else "recce"


def stamp(name):
    """(mtime_ns, size) of a dashboard file, or None when it does not exist; cheap change detection."""
    try:
        st = path_for(name).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def save(cfg):
    """Validate and write atomically; returns the stored config."""
    cfg = validate(cfg)
    p = path_for(cfg["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return cfg


def delete(name):
    _check_name(name)
    try:
        path_for(name).unlink()
    except FileNotFoundError:
        raise KeyError(name) from None


def update(name, patch):
    """Merge `patch` into the stored dashboard (its name cannot change) and save it."""
    cfg = load(name)
    if isinstance(patch, dict) and patch.get("name", name) != name:
        raise ConfigError("name: cannot be changed with an update; create a new dashboard instead")
    if isinstance(patch, dict) and patch.get("kind", cfg.get("kind", "recce")) != cfg.get("kind", "recce"):
        raise ConfigError("kind: cannot be changed with an update; create a new dashboard instead")
    return save(apply_patch(cfg, patch))


def current_system(where=None):
    """The commander's system from the journal, or (None, reason)."""
    if where is None:
        from ..journal import context
        where = context.where_am_i
    try:
        data = where()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if isinstance(data, dict) and "error" not in data and data.get("system"):
        return data["system"], None
    return None, (data.get("error") if isinstance(data, dict) else None) or "the journal names no system"


def pin_origin(cfg, where=None):
    """A new dashboard whose origin is "current" is anchored to the system the commander is in right now.
    Returns (config, note); the note says why it stayed "current" when the journal could not be read."""
    if not isinstance(cfg, dict) or cfg.get("kind", "recce") != "recce"             or str(cfg.get("origin", DEFAULT_CONFIG["origin"])).strip().lower() != "current":
        return cfg, None
    system, reason = current_system(where)
    if system is None:
        return cfg, f"origin stays \"current\" (follows your position) because the journal could not be read: {reason}"
    return {**cfg, "origin": system}, None


def save_new(cfg, where=None):
    """Create or replace a dashboard, anchoring an origin of "current". Returns (stored config, note)."""
    cfg, note = pin_origin(cfg, where)
    return save(cfg), note


def ensure_default():
    """Create the default `recce` dashboard when no dashboards exist yet."""
    if not list_names():
        save_new(DEFAULT_CONFIG)


def default_name():
    names = list_names()
    return DEFAULT_NAME if DEFAULT_NAME in names else (names[0] if names else None)
