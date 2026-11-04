"""
What else fits: candidates for free (or given-up) slots that the power plant can run, and power-priority advice.

Priority tiers are a community heuristic, not game data: tier 1 is what the ship cannot be flown without, higher tiers can
go dark first. The game sheds modules from the highest priority group number downwards when the plant is overdrawn.
"""

from . import calc

RATING_ORDER = "ABCDEFGHI"
# module group code (coriolis) -> role shown to the user
ROLES = {
    "sg": "shield", "bsg": "shield", "psg": "shield", "scb": "shield cell", "hr": "hull armour", "mahr": "hull armour",
    "ghrp": "hull armour", "mrp": "module armour", "gmrp": "module armour", "gsrp": "shield reinforcement",
    "fs": "fuel scoop", "am": "repair (AFMU)", "cr": "cargo", "crl": "cargo", "pv": "SRV hangar", "fh": "fighter hangar",
    "rf": "refinery", "cc": "limpet: collector", "pc": "limpet: prospector", "fx": "limpet: fuel transfer",
    "rpl": "limpet: repair", "hb": "limpet: hatch breaker", "rcpl": "limpet: recon", "dtl": "limpet: decontamination",
    "mlc": "limpet: multi", "rsl": "limpet: research", "dc": "docking computer", "sua": "supercruise assist",
    "ss": "surface scanner", "gfsb": "FSD booster", "fi": "FSD interdictor", "ews": "weapon stabiliser",
    "pcm": "passenger cabin", "pci": "passenger cabin", "pce": "passenger cabin", "pcq": "passenger cabin",
}
# role -> priority tier (1 = never lose it ... 4 = fine to lose first)
TIERS = {"thrusters": 1, "fsd": 1, "pd": 1, "ls": 1, "sensors": 2, "shield": 2, "shield cell": 2, "FSD booster": 2,
         "fuel scoop": 3, "hardpoint": 3, "repair (AFMU)": 3, "surface scanner": 3, "FSD interdictor": 3,
         "weapon stabiliser": 3, "docking computer": 4, "supercruise assist": 4, "refinery": 4, "SRV hangar": 4,
         "fighter hangar": 4}
DEFAULT_TIER = 3
TIER_WHY = {1: "the ship stops being flyable without it", 2: "keeps you alive and informed", 3: "needed only when in use",
            4: "convenience, fine to lose first"}
EXCLUDED_GROUPS = {"pas", "mm", "bh"}


def role_of(f):
    if f.kind in ("thrusters", "fsd", "pd", "ls", "sensors"):
        return f.kind
    if f.kind == "hardpoint":
        return "hardpoint"
    return ROLES.get(f.group, f.group or "unknown")


def _rank(m):
    return (-m["class"], RATING_ORDER.index(m.get("rating", "I")) if m.get("rating", "I") in RATING_ORDER else 9, m["symbol"])


def _candidates(slot, cat, unique=()):
    """Catalogue modules a free internal slot can take (restricted slots only take their eligible groups)."""
    eligible = slot.get("eligible")
    size = slot["size"]
    out = []
    for m in cat.modules.values():
        grp = m.get("grp")
        if grp in unique:
            continue
        if grp not in ROLES or grp in EXCLUDED_GROUPS or not isinstance(m.get("class"), int):
            continue
        if eligible is not None:
            if grp not in eligible:
                continue
        elif m.get("restriction") or m["class"] > size:
            continue
        if m["class"] > size:
            continue
        out.append(m)
    return out


def suggest_additions(b, cat, role=None, replace_slots=(), per_role=2):
    """Per free slot (plus slots in `replace_slots`, treated as given up): the best fitting modules per role, with power
    cost, what is left of the power budget and the jump-range effect. Only modules the plant can run are listed as fitting."""
    base = b.with_changes({s: None for s in replace_slots}, cat) if replace_slots else b
    free = base.free_slots()
    p0 = calc.power(base)
    base_mass = calc.ranges(base)["laden_mass"]
    tank = calc.fuel_capacity(base)
    range0 = calc.jump_range(base, base_mass, tank)
    unique = {"gfsb"} if any(f.group == "gfsb" for f in base.fitted) else set()    # only one booster fits
    slots = []
    for i, slot in enumerate(free["internal"]):
        name = f"{slot['name'] or 'Slot'}{90 + i}" + ("" if slot["name"] else f"_Size{slot['size']}")
        by_role = {}
        for m in _candidates(slot, cat, unique):
            r = ROLES[m["grp"]]
            if role and role.lower() not in r.lower():
                continue
            by_role.setdefault(r, []).append(m)
        options = []
        for r, mods in sorted(by_role.items()):
            for m in sorted(mods, key=_rank)[:per_role]:
                draw = m.get("power", 0) or 0
                trial = base.with_changes({name: m["symbol"]}, cat)
                new_range = calc.jump_range(trial, calc.ranges(trial)["laden_mass"], tank)
                options.append({"role": r, "item": m["symbol"], "class": m["class"], "rating": m.get("rating"), "power": draw,
                                "mass": m.get("mass", 0), "fits_power": draw <= p0["headroom_retracted"] + 1e-9,
                                "headroom_after": round(p0["headroom_retracted"] - draw, 3),
                                "range_change_ly": round((new_range or 0) - (range0 or 0), 2) if range0 else None,
                                "cargo": m.get("cargo"), "guardian": m.get("rating") == "H"})
        slots.append({"slot": name, "size": slot["size"], "restricted_to": sorted(slot["eligible"]) if slot["eligible"] else None,
                      "options": options})
    return {"ship": b.ship["properties"]["name"], "power_headroom_retracted": p0["headroom_retracted"],
            "power_headroom_deployed": p0["headroom_deployed"], "free_internal_slots": len(free["internal"]),
            "free_hardpoints": sorted(free["hardpoints"], reverse=True),
            "slots": slots,
            "note": "hardpoint weapons are not listed; ask for a specific weapon and use build-check --swap to test its draw"}


def priority_report(b):
    """Current power priorities against the tier heuristic: what is shed first, what is at risk, what to move."""
    p = calc.power(b)
    rows = []
    for g in p["groups"]:
        for m in g["modules"]:
            f = b.get(m["slot"])
            role = role_of(f)
            tier = TIERS.get(role, DEFAULT_TIER)
            rows.append({"slot": m["slot"], "item": m["item"], "role": role, "draw": m["draw"], "group": g["group"],
                         "tier": tier, "kind": f.kind})
    problems = []
    for r in rows:
        later = [o for o in rows if o["group"] < r["group"] and o["tier"] > r["tier"] and o["draw"] > 0]
        if r["tier"] <= 2 and later:
            problems.append(f"{r['role']} ({r['slot']}) is in group {r['group']} but {', '.join(sorted({o['role'] for o in later}))} "
                            f"stay powered in an earlier group; it would be shed first ({TIER_WHY[r['tier']]})")
    out = {"supply": p["supply"], "groups": p["groups"], "modules": rows, "problems": problems,
           "suggested_groups": {r["slot"]: min(r["tier"], 5) for r in rows}, "tier_meaning": TIER_WHY}
    for state, key in (("retracted", "retracted"), ("deployed", "deployed")):
        out[state] = _shed(rows, p, key)
    return out


def _shed(rows, p, key):
    """Which groups lose power in this state, and what that costs."""
    supply = p["supply"]
    draw = p[key]
    shed = []
    groups = sorted({r["group"] for r in rows}, reverse=True)
    for g in groups:
        if draw <= supply + 1e-9:
            break
        mods = [r for r in rows if r["group"] == g and (key == "deployed" or r["kind"] != "hardpoint")]
        draw -= sum(m["draw"] for m in mods)
        shed.extend(mods)
    return {"draw": p[key], "supply": supply, "ok": p[key] <= supply + 1e-9, "shed": shed,
            "tier_1_2_lost": sorted({m["role"] for m in shed if m["tier"] <= 2})}
