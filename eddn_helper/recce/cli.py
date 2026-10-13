"""elite-dash: create, edit and open Recce dashboards. python -m eddn_helper.recce <command>"""

import argparse
import json
import sys
import webbrowser

from . import config as rcfg
from . import server


def _err(msg):
    print(msg, file=sys.stderr)
    return 1


def _print_config_error(e):
    print("Invalid dashboard config:", file=sys.stderr)
    for line in e.errors:
        print("  " + line, file=sys.stderr)
    return 1


def _need(name):
    if not rcfg.exists(name):
        names = ", ".join(rcfg.list_names()) or "none yet"
        raise KeyError(f"no dashboard named {name!r} (existing: {names})")


def cmd_serve(args):
    return server.serve(port=args.port, quiet=args.quiet)


def cmd_open(args):
    rcfg.ensure_default()
    _need(args.name)
    info = server.ensure_server(port=args.port or 0)
    url = server.dashboard_url(info["base_url"], args.name)
    print(("Started the Recce server. " if info["started"] else "Recce server already running. ") + url)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def cmd_stop(args):
    print("Stopped the Recce server." if server.stop_server() else "No Recce server is running.")
    return 0


def cmd_list(args):
    rcfg.ensure_default()
    running = server.running_server()
    for n in rcfg.list_names():
        try:
            title = rcfg.load(n)["title"]
        except (rcfg.ConfigError, KeyError) as e:
            title = f"(invalid: {e})"
        url = server.dashboard_url(running["base_url"], n) if running else "(server not running)"
        print(f"{n:<20} {title:<28} {url}")
    print("server: " + (f"running on port {running['port']}" if running else "not running"))
    return 0


def cmd_show(args):
    _need(args.name)
    cfg = rcfg.load(args.name)
    print(json.dumps(cfg, indent=2))
    if args.data:
        from .core import Core
        core = Core(server.DbFactory())
        print(json.dumps(core.snapshot(cfg), indent=2))
    return 0


def cmd_delete(args):
    _need(args.name)
    rcfg.delete(args.name)
    print(f"Deleted dashboard {args.name!r}.")
    return 0


def cmd_new(args):
    if rcfg.exists(args.name) and not args.force:
        return _err(f"dashboard {args.name!r} already exists (use --force to replace it)")
    cfg = json.loads(json.dumps(rcfg.DEFAULT_CONFIG))
    cfg["name"] = args.name
    cfg["title"] = args.title or args.name.replace("-", " ").replace("_", " ").title()
    if args.origin is not None:
        cfg["origin"] = args.origin
    if args.radius is not None:
        cfg["radius_ly"] = args.radius
    if args.economy is not None:
        cfg["filters"]["economy"] = args.economy
    if args.min_pop is not None:
        cfg["filters"]["min_population"] = args.min_pop
    if args.state_in is not None:
        cfg["filters"]["state_in"] = args.state_in
    if args.refresh is not None:
        cfg["refresh_seconds"] = args.refresh
    saved, note = rcfg.save_new(cfg)
    print(f"Saved dashboard {saved['name']!r} in {rcfg.path_for(saved['name'])}")
    print(f"Anchored on {saved['origin']}." if saved["origin"].lower() != "current" else (note or ""))
    print(f"Open it with: elite-dash open {saved['name']}")
    return 0


def cmd_edit(args):
    _need(args.name)
    cfg = rcfg.load(args.name)
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key:
            return _err(f"--set needs key=value, got {item!r}")
        cfg = rcfg.set_path(cfg, key.strip(), rcfg.parse_value(value))
    saved = rcfg.save(cfg)
    print(f"Updated dashboard {saved['name']!r}; open browsers reload it by themselves.")
    return 0


def cmd_recentre(args):
    from .core import Core, CoreError
    _need(args.name)
    try:
        res = Core(None).recentre(args.name)
    except CoreError as e:
        return _err(str(e))
    if res["previous"] == res["origin"]:
        print(f"{args.name!r} is already anchored on {res['origin']}.")
    else:
        print(f"{args.name!r} recentred on {res['origin']} (was {res['previous']}); open browsers update by themselves.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="elite-dash", description="Recce: local live dashboards for elite-helper.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the server in the foreground")
    s.add_argument("--port", type=int, default=0, help="port to listen on (default: a free one)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("open", help="start the server if needed and open a dashboard in your browser")
    s.add_argument("name", nargs="?", default=rcfg.DEFAULT_NAME)
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true", help="only print the URL")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("stop", help="stop the background server")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("list", help="list dashboards")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="print a dashboard's config (--data also runs the query once)")
    s.add_argument("name")
    s.add_argument("--data", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("recentre", help="anchor a dashboard on the system you are in now")
    s.add_argument("name", nargs="?", default=rcfg.DEFAULT_NAME)
    s.set_defaults(func=cmd_recentre)

    s = sub.add_parser("delete", help="delete a dashboard")
    s.add_argument("name")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("new", help="create a dashboard from the default, with overrides")
    s.add_argument("name")
    s.add_argument("--title")
    s.add_argument("--origin", help='a system name, or "current" for your journal position')
    s.add_argument("--radius", type=float, help="radius in ly")
    s.add_argument("--economy", help="e.g. Agriculture")
    s.add_argument("--min-pop", type=int, dest="min_pop", help="minimum population")
    s.add_argument("--state-in", dest="state_in", choices=list(rcfg.STATE_IN))
    s.add_argument("--refresh", type=int, help="minimum seconds between refreshes (at least 5)")
    s.add_argument("--force", action="store_true", help="replace an existing dashboard")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("edit", help="change settings: --set filters.min_population=500000 --set radius_ly=60")
    s.add_argument("name")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", required=True)
    s.set_defaults(func=cmd_edit)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except rcfg.ConfigError as e:
        return _print_config_error(e)
    except KeyError as e:
        return _err(str(e.args[0]) if e.args else "not found")
    except RuntimeError as e:
        return _err(str(e))
    except KeyboardInterrupt:
        return 130
