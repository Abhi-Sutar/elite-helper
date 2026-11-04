"""CLI side of the build tools: `register(add, jadd)` adds the build-* commands to the parser built in eddn_helper.cli."""

import json
import sys

from . import api


def _base():
    from .. import cli       # imported late: eddn_helper.cli imports this module while it builds its parser
    return cli


def _swaps(items):
    out = {}
    for s in items or []:
        slot, _, item = s.partition("=")
        if not slot:
            raise SystemExit(f"--swap needs SLOT=ITEM (or SLOT= to empty the slot), got '{s}'")
        out[slot.strip()] = item.strip()
    return out


def _fail(d):
    if d.get("error"):
        print(d["error"], file=sys.stderr)
        return True
    return False


def _ly(v):
    return "-" if v is None else f"{v:.2f} ly"


def _print_build(b):
    c = _base()
    p = b["power"]
    print(f"{b['ship']}: unladen {b['unladen_mass']} t, fuel {b['fuel_capacity']} t, cargo {b['cargo_capacity']} t")
    print(f"Jump range: {_ly(b['jump_range_unladen'])} unladen, {_ly(b['jump_range_laden'])} laden "
          f"(full tank + full hold, {b['laden_mass']} t)" + (f"; Guardian booster +{b['guardian_booster_ly']} ly"
                                                              if b["guardian_booster_ly"] else ""))
    if not b["thrusters_ok_laden"]:
        print("WARNING: thrusters cannot lift the laden mass")
    print("Range by cargo (full tank): " + ", ".join(f"{r['cargo']} t {_ly(r['range'])}" for r in b["cargo_table"]))
    print(f"Power: {p['supply']} MW supply, {p['retracted']} retracted ({p['headroom_retracted']:+.2f} spare), "
          f"{p['deployed']} deployed ({p['headroom_deployed']:+.2f} spare)")
    c.table([{"group": g["group"], "draw": g["retracted"], "dep": g["deployed"], "cum": g["cumulative_deployed"],
              "mods": ", ".join(m["item"] for m in g["modules"])} for g in p["groups"]],
            [("Prio", "group"), ("MW", "draw"), ("MW deployed", "dep"), ("Cum. deployed", "cum"), ("Modules", "mods")])
    if b["free_internal_slots"] or b["free_hardpoints"]:
        print("Free: " + (", ".join(f"size {s['size']}" + (f" ({'/'.join(s['restricted_to'])} only)" if s["restricted_to"] else "")
                                    for s in b["free_internal_slots"]) or "no internal slots")
              + f"; hardpoints {b['free_hardpoints'] or 'none'}")
    if b["unknown_modules"]:
        print("Not in the catalogue (mass and power unknown): " + ", ".join(b["unknown_modules"]))


def cmd_data(args, session):
    d = api.data_status(args.refresh)
    c = _base()
    c.emit(args, d, lambda d: print(d["error"] if not d["ok"] else
                                    f"coriolis-data: {d['ships']} ships, {d['modules']} modules, fetched {d['fetched_at']} "
                                    f"({d['age_days']} days old)\n{d['path']}"))
    return 0 if d["ok"] else 1


def cmd_show(args, session):
    c = _base()
    c._jemit(args, session, api.show, lambda d: (_print_build(d["build"]),
                                                 d.get("check") and print(f"vs journal: {d['check']}")),
             ship_id=args.ship_id, swaps=_swaps(args.swap), cargo=args.cargo)


def cmd_priorities(args, session):
    c = _base()

    def human(d):
        print(f"Supply {d['supply']} MW. Group 1 = highest priority (shed last); the game sheds the highest number first.")
        c.table(d["modules"], [("Prio", "group"), ("Slot", "slot"), ("Role", "role"), ("MW", "draw"),
                               ("Suggested", lambda r: d["suggested_groups"][r["slot"]])])
        for state in ("retracted", "deployed"):
            s = d[state]
            print(f"{state}: {s['draw']} / {s['supply']} MW " + ("ok" if s["ok"] else
                  f"OVERDRAWN, shed: {', '.join(m['role'] for m in s['shed'])}"
                  + (f"  (loses {', '.join(s['tier_1_2_lost'])})" if s["tier_1_2_lost"] else "")))
        for pr in d["problems"]:
            print("- " + pr)
    c._jemit(args, session, api.priorities, human, ship_id=args.ship_id, swaps=_swaps(args.swap))


def cmd_suggest(args, session):
    c = _base()

    def human(d):
        print(f"{d['ship']}: {d['power_headroom_retracted']} MW spare retracted, {d['power_headroom_deployed']} deployed; "
              f"{d['free_internal_slots']} free internal slot(s); free hardpoints {d['free_hardpoints'] or 'none'}")
        for s in d["slots"]:
            print(f"\nSlot size {s['size']}" + (f" ({'/'.join(s['restricted_to'])} only)" if s["restricted_to"] else "")
                  + f"  [{s['slot']}]")
            c.table(s["options"], [("Role", "role"), ("Module", "item"), ("MW", "power"), ("Mass", "mass"),
                                   ("Power", lambda r: "fits" if r["fits_power"] else "NO"),
                                   ("Range", lambda r: f"{r['range_change_ly']:+.2f}" if r["range_change_ly"] is not None else "-"),
                                   ("Note", lambda r: "Guardian unlock" if r["guardian"] else "")])
        print("\n" + d["note"])
    c._jemit(args, session, api.suggest_for, human, ship_id=args.ship_id, role=args.role, replace_slots=args.replace,
             swaps=_swaps(args.swap))


def cmd_jump(args, session):
    c = _base()

    def human(d):
        cur = d["current"]
        print(f"Current: {cur['fsd']} {cur['fsd_rating']}, tank {cur['fuel_capacity']} t, unladen {_ly(cur['range_unladen'])}, "
              f"laden {_ly(cur['range_laden'])}")
        c.table(d["candidates"], [("Drive", "fsd"), ("Rtg", "fsd_rating"), ("Variant", "fsd_variant"), ("Booster", "booster"),
                                  ("Tank t", "fuel_capacity"), ("Unladen", "range_unladen"), ("Laden", "range_laden"),
                                  ("vs now", lambda r: f"{r['delta_vs_current']:+.2f}")])
        print("\nMass you could shed (ly gained at laden mass):")
        c.table(d["removal_gains"], [("Slot", "slot"), ("Module", "item"), ("Mass t", "mass"), ("ly", "range_gain_ly")])
        for a in d["assumptions"]:
            print("- " + a)
    c._jemit(args, session, api.jump, human, ship_id=args.ship_id,
             keep=[k for k in (args.keep or "").split(",") if k], objective=args.objective,
             assume_engineering=not args.no_engineering, top=args.top)


def cmd_check(args, session):
    c = _base()
    if args.file == "-":
        text = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8-sig") as fh:
            text = fh.read()
    d = api.check(text, _swaps(args.swap), args.cargo)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
    elif not _fail(d):
        _print_build(d["build"])
        for pr in d["priorities"]["problems"]:
            print("- " + pr)
        for state in ("retracted", "deployed"):
            if not d["priorities"][state]["ok"]:
                print(f"{state}: overdrawn by {d['priorities'][state]['draw'] - d['priorities'][state]['supply']:.2f} MW")
    return 0 if d.get("found") else 1


def cmd_max_cargo(args, session):
    c = _base()
    d = api.max_cargo(args.ship, args.pad, args.reserve)

    def human(d):
        c.table(d["ships"], [("Ship", "name"), ("Maker", "manufacturer"), ("Pad", "pad"), ("Cargo t", "cargo_t"),
                             ("Range ly", lambda r: r["best_fsd"]["range_ly"] if r["best_fsd"] else "n/a"),
                             ("Best FSD", lambda r: f"{r['best_fsd']['fsd']} ({r['best_fsd']['fsd_rating']})" if r["best_fsd"] else "-")])
        print()
        for n in d["notes"]:
            print("- " + n)
    if d.get("error"):
        print(d["error"], file=sys.stderr)
        return 1
    c.emit(args, d, human)
    return 0


def register(add, jadd):
    sp = add("build-data", cmd_data, "Ship and module data (EDCD coriolis-data): download/refresh and show its age")
    sp.set_defaults(raw=True)
    sp.add_argument("--refresh", action="store_true", help="download again now")

    def common(sp):
        sp.add_argument("--ship-id", type=int, help="a ship you have flown (ShipID from `ships`); default: the current ship")
        sp.add_argument("--swap", action="append", metavar="SLOT=ITEM",
                        help="what-if: put ITEM (journal name, e.g. int_shieldgenerator_size6_class5) in SLOT, or SLOT= to empty it; repeatable")

    sp = jadd("build-show", cmd_show, "Mass, jump range, power by priority group and free slots of your ship's current fit")
    common(sp)
    sp.add_argument("--cargo", type=float, help="cargo tons for the laden range (default: full hold)")
    sp = jadd("build-priorities", cmd_priorities, "Power priority groups vs what the ship needs; what is shed first when overdrawn")
    common(sp)
    sp = jadd("build-suggest", cmd_suggest, "What still fits: modules for free slots that the power plant can run")
    common(sp)
    sp.add_argument("--role", help="only roles containing this text, e.g. shield, scoop, limpet, cargo")
    sp.add_argument("--replace", action="append", metavar="SLOT", help="treat this fitted slot as given up; repeatable")
    sp = jadd("build-jump", cmd_jump, "Best frame shift drive, Guardian booster and fuel tank for jump range")
    sp.add_argument("--ship-id", type=int)
    sp.add_argument("--keep", help="comma separated of fsd,booster,fuel_tank to leave as they are")
    sp.add_argument("--objective", choices=["laden", "unladen"], default="laden")
    sp.add_argument("--no-engineering", action="store_true", help="do not assume engineering on candidate drives")
    sp.add_argument("--top", type=int, default=5)

    sp = add("build-check", cmd_check, "Check a Loadout JSON file (Coriolis / EDSY export, journal event) or - for stdin")
    sp.set_defaults(raw=True)
    sp.add_argument("file")
    sp.add_argument("--swap", action="append", metavar="SLOT=ITEM")
    sp.add_argument("--cargo", type=float)

    sp = add("build-max-cargo", cmd_max_cargo, "Max cargo fit and best laden jump range for one ship or every ship")
    sp.set_defaults(raw=True)
    sp.add_argument("--ship", help="coriolis name (type_8_transport), journal key or ship name")
    sp.add_argument("--pad", choices=["small", "medium", "large"])
    sp.add_argument("--reserve", type=int, default=2, help="slots kept for docking computer + supercruise assist (default 2)")
