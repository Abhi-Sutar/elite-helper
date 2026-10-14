"""Dashboard kinds. Every stored dashboard has a `kind` (absent = "recce"); the kind decides how its config is
validated, which query builds its snapshot, which page shows it and what makes it refresh.

A kind is a small object:
  name           the value of "kind" in the dashboard's JSON
  page           the HTML file the server serves for it
  stamp_key      which database stamp (Core.stamps) tells the poller the data moved
  validate(raw)  fill defaults and check a config; raises config.ConfigError
  snapshot(core, cfg)       the JSON the page shows (runs on a worker thread)
  labels(cfg)    column labels for the page ({} when it has none)
  follows_position(cfg)     True when the dashboard tracks the commander's journal position
  origin(cfg)    the system the dashboard is anchored on (a name, or "current")
  origin_patch(system)      the config patch that anchors it on `system`
"""

from pathlib import Path

from . import config as rcfg

DEFAULT_KIND = "recce"


class RecceKind:
    name = "recce"
    page = Path(__file__).with_name("page.html")
    stamp_key = "newest_faction_update"

    def validate(self, raw):
        return rcfg.validate_recce(raw)

    def snapshot(self, core, cfg):
        return core.snapshot(cfg)

    def labels(self, cfg):
        from .core import column_labels
        return column_labels(cfg)

    def follows_position(self, cfg):
        return cfg["origin"].strip().lower() == "current"

    def origin(self, cfg):
        return cfg["origin"]

    def origin_patch(self, system):
        return {"origin": system}


KINDS = {}


def register(kind):
    KINDS[kind.name] = kind
    return kind


register(RecceKind())

from . import cg  # noqa: E402  (cg needs config, not kinds; registered here so every importer sees both)

register(cg.KIND)


def get(name):
    """The kind called `name` (None means the default). Raises ConfigError for an unknown one."""
    name = DEFAULT_KIND if name is None else name
    try:
        return KINDS[name]
    except (KeyError, TypeError):
        raise rcfg.ConfigError(f"kind: must be one of {', '.join(KINDS)}; got {name!r}") from None


def for_cfg(cfg):
    """The kind of a validated config."""
    return get(cfg.get("kind") if isinstance(cfg, dict) else None)
