"""
Pure build maths: mass, jump range (same formula as Coriolis / the game), cargo and fuel capacity, power budget.

Jump range: ((fuel used / fuelmul) ** (1 / fuelpower)) * optmass / mass, plus a Guardian FSD booster's flat bonus.
Fuel used is limited by the drive's maxfuel and by the fuel on board. The journal's MaxJumpRange is this with one jump's
fuel on board and no cargo ("unladen"); "laden" is a full tank and a full hold.
"""

CRUISE_ESSENTIALS = ("thrusters", "fsd", "pd", "ls", "sensors")


def hull_mass(b):
    return b.ship["properties"]["hullMass"]


def module_mass(b):
    return sum(f.stat("mass") for f in b.fitted)


def unladen_mass(b):
    """Hull plus every module, no fuel and no cargo (the journal's UnladenMass)."""
    return hull_mass(b) + module_mass(b)


def fuel_capacity(b):
    return sum(f.stat("fuel") for f in b.fitted if f.group == "ft")


def cargo_capacity(b):
    return sum(f.stat("cargo") for f in b.fitted if f.group in ("cr", "crl"))


def booster_bonus(b):
    return max((f.stat("jumpboost") for f in b.fitted if f.group == "gfsb" and f.on), default=0)


def jump_range(b, mass, fuel=None):
    """Range in ly at a total ship mass; `fuel` is the fuel available for the jump (default: the drive's maxfuel)."""
    fsd = b.first("fsd")
    if fsd is None or not fsd.known:
        return None
    used = fsd.stat("maxfuel") if fuel is None else min(fuel, fsd.stat("maxfuel"))
    if used <= 0 or mass <= 0:
        return 0.0
    return (used / fsd.stat("fuelmul")) ** (1 / fsd.stat("fuelpower")) * fsd.stat("optmass") / mass + booster_bonus(b)


def ranges(b, cargo=None):
    """Unladen (journal-style), at a given cargo load with one jump of fuel, and laden (full tank + `cargo` or a full hold)."""
    fsd = b.first("fsd")
    if fsd is None or not fsd.known:
        return {"unladen": None, "laden": None}
    base = unladen_mass(b)
    cap = cargo_capacity(b)
    load = cap if cargo is None else min(cargo, cap)
    tank = fuel_capacity(b)
    jump_fuel = min(fsd.stat("maxfuel"), tank) if tank else fsd.stat("maxfuel")
    return {"unladen": jump_range(b, base + jump_fuel, jump_fuel),
            "laden": jump_range(b, base + tank + load, tank),
            "unladen_mass": base, "jump_fuel": jump_fuel,
            "laden_mass": base + tank + load}


def cargo_table(b, steps=(0, 0.25, 0.5, 0.75, 1.0)):
    """Jump range with a full tank at several cargo loads."""
    cap, tank, base = cargo_capacity(b), fuel_capacity(b), unladen_mass(b)
    return [{"cargo": round(cap * s), "range": jump_range(b, base + tank + cap * s, tank)} for s in steps]


def thruster_ok(b, mass):
    t = b.first("thrusters")
    return t is None or not t.known or mass <= t.stat("maxmass", float("inf"))


def power(b):
    """Power budget in MW the way Coriolis shows it: retracted (hardpoints stowed) and deployed, plus draw by priority group.

    Priority group n (1-based, as in the game) is the journal's Priority + 1. When the plant cannot cover the draw the
    game switches modules off starting with the highest group number, so `shed_order` lists groups in that order.
    """
    pp = b.first("pp")
    supply = pp.stat("pgen") if pp else 0
    groups = {}
    retracted = deployed = 0.0
    for f in b.fitted:
        if f.kind in ("pp", "bulkhead", "other"):
            continue
        draw = f.stat("power") if f.on else 0
        if not draw:
            continue
        if f.kind == "hardpoint":
            deployed += draw
        else:
            retracted += draw
            deployed += draw
        g = groups.setdefault(f.priority + 1, {"group": f.priority + 1, "retracted": 0.0, "deployed": 0.0, "modules": []})
        g["modules"].append({"slot": f.slot, "item": f.item, "draw": round(draw, 3), "kind": f.kind})
        g["deployed"] += draw
        if f.kind != "hardpoint":
            g["retracted"] += draw
    ordered = [groups[k] for k in sorted(groups)]
    run_r = run_d = 0.0
    for g in ordered:
        run_r += g["retracted"]
        run_d += g["deployed"]
        g["retracted"], g["deployed"] = round(g["retracted"], 3), round(g["deployed"], 3)
        g["cumulative_retracted"], g["cumulative_deployed"] = round(run_r, 3), round(run_d, 3)
    return {"supply": round(supply, 3), "retracted": round(retracted, 3), "deployed": round(deployed, 3),
            "headroom_retracted": round(supply - retracted, 3), "headroom_deployed": round(supply - deployed, 3),
            "groups": ordered, "shed_order": [g["group"] for g in reversed(ordered)]}


def summary(b, cargo=None):
    r = ranges(b, cargo)
    tank = fuel_capacity(b)
    return {"ship": b.ship["properties"]["name"], "hull_mass": hull_mass(b), "unladen_mass": round(r.get("unladen_mass", unladen_mass(b)), 2),
            "fuel_capacity": tank, "cargo_capacity": cargo_capacity(b),
            "jump_range_unladen": _r(r["unladen"]), "jump_range_laden": _r(r["laden"]),
            "laden_mass": _r(r.get("laden_mass")), "thrusters_ok_laden": thruster_ok(b, r.get("laden_mass", 0)),
            "guardian_booster_ly": booster_bonus(b), "cargo_table": [{"cargo": c["cargo"], "range": _r(c["range"])}
                                                                      for c in cargo_table(b)],
            "power": power(b), "unknown_modules": list(b.unknown)}


def _r(v):
    return None if v is None else round(v, 2)
