"""The dashboard_* operations behind the MCP tools: plain functions returning plain dicts (errors as {"error": ...})."""

import webbrowser

from . import config as rcfg
from . import server


def _fail(e):
    if isinstance(e, rcfg.ConfigError):
        return {"error": "invalid dashboard config: " + "; ".join(e.errors), "errors": e.errors}
    if isinstance(e, KeyError):
        names = ", ".join(rcfg.list_names()) or "none yet"
        return {"error": f"no dashboard named {e.args[0]!r} (existing: {names})"}
    return {"error": str(e)}


def _with_server(name, cfg=None, **extra):
    info = server.ensure_server()
    out = {"name": name, "url": server.dashboard_url(info["base_url"], name), "server_running": True,
           "server_started": info["started"]}
    if cfg is not None:
        out["config"] = cfg
    out.update(extra)
    return out


def create(config):
    try:
        cfg = rcfg.save(config)
        return _with_server(cfg["name"], cfg)
    except (rcfg.ConfigError, KeyError, RuntimeError) as e:
        return _fail(e)


def update(name, patch):
    try:
        cfg = rcfg.update(name, patch)
        return _with_server(name, cfg, note="open browsers reload this dashboard by themselves")
    except (rcfg.ConfigError, KeyError, RuntimeError) as e:
        return _fail(e)


def list_dashboards():
    """Read-only: never starts the server."""
    rcfg.ensure_default()
    running = server.running_server()
    base = running["base_url"] if running else None
    items = []
    for n in rcfg.list_names():
        try:
            title = rcfg.load(n)["title"]
            err = None
        except (rcfg.ConfigError, KeyError) as e:
            title, err = None, _fail(e)["error"]
        items.append({"name": n, "title": title, "url": server.dashboard_url(base, n) if base else None,
                      **({"error": err} if err else {})})
    return {"server_running": bool(running), "server_url": base + "/" if base else None, "dashboards": items,
            "note": None if running else "the server is not running; dashboard_open starts it and returns the URL"}


def open_dashboard(name, browser=True):
    try:
        rcfg.ensure_default()
        if not rcfg.exists(name):
            raise KeyError(name)
        out = _with_server(name)
        if browser:
            out["browser_opened"] = bool(webbrowser.open(out["url"]))
        return out
    except (rcfg.ConfigError, KeyError, RuntimeError) as e:
        return _fail(e)


def delete(name):
    try:
        rcfg.delete(name)
    except (rcfg.ConfigError, KeyError) as e:
        return _fail(e)
    running = server.running_server()
    return {"name": name, "deleted": True, "server_running": bool(running),
            "url": running["base_url"] + "/" if running else None}
