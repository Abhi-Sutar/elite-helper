"""
Searches over a build: best jump range (FSD, Guardian booster, fuel tank) and the max-cargo fit per ship.

The jump search only touches the drive, a Guardian booster in a free slot and the main fuel tank; everything else stays as
fitted. New drives are assumed engineered (FSD Increased Range grade 5 + Mass Manager, best roll) unless told otherwise.
"""

from . import calc
from .model import Build, Fitted, engineered_mods

PAD_NAMES = {1: "small", 2: "medium", 3: "large"}
RATING_ORDER = "ABCDEFGHI"


def _fsd_options(b, cat, assume_engineering):
    """(label, Fitted) for the current drive as fitted, plus every drive of the slot's class."""
    cur = b.first("fsd")
    slot_class = b.ship["slots"]["standard"][2]
    out = []
    if cur is not None and cur.known:
        out.append(("current", cur))
    seen = set()
    for e in sorted(cat.group("fsd"), key=lambda m: (RATING_ORDER.index(m.get("rating", "I")), m["symbol"])):
        if e.get("class") != slot_class or e["symbol"] in seen:
            continue
        seen.add(e["symbol"])
        if cur is not None and cur.item.lower() == e["symbol"].lower() and cur.mods:
            continue                           # the real, engineered drive is already "current"
        slot = cur.slot if cur else "FrameShiftDrive"
        prio = cur.priority if cur else 0
        if cur is None or cur.item.lower() != e["symbol"].lower():
            out.append(("stock", Fitted(slot, e["symbol"], "fsd", None, e, True, prio)))
        lr = cat.blueprints.get("FSD_LongRange", {}).get("5")
        if assume_engineering and lr:
            mods = engineered_mods(e, lr, "special_fsd_heavy")
            out.append(("engineered G5 + Mass Manager", Fitted(slot, e["symbol"], "fsd", None, e, True, prio, mods,
                                                                {"blueprint": "FSD_LongRange", "level": 5, "quality": 1.0,
                                                                 "experimental": "Mass Manager"})))
    return out


def _booster_options(b, cat, keep):
    """(slot name, Fitted or None): no booster, the current one, or the best Guardian booster that fits a free slot."""
    cur = next((f for f in b.fitted if f.group == "gfsb"), None)
    opts = [("none" if cur is None else "current", None if cur is None else cur)]
    if "booster" in keep:
        return opts[:1] if cur is None else [("current", cur)]
    boosters = cat.group("gfsb")
    for i, slot in enumerate(b.free_slots()["internal"]):
        if slot["name"] or not isinstance(slot["size"], int):
            continue
        fits = [m for m in boosters if m["class"] <= slot["size"]]
        if fits:
            best = max(fits, key=lambda m: m.get("jumpboost", 0))
            opts.append((f"new {best['symbol']} in a free size {slot['size']} slot",
                         Fitted(f"Slot{90 + i}_Size{slot['size']}", best["symbol"], "internal", slot["size"], best, True, 0)))
    return opts


def _tank_options(b, cat, keep, min_fuel):
    cur = b.first("fuel_tank")
    if cur is None or "fuel_tank" in keep:
        return [cur]
    slot_class = b.ship["slots"]["standard"][6]
    tanks = [t for t in cat.group("ft") if t["class"] <= slot_class and t["fuel"] >= min_fuel]
    return [cur] + [Fitted(cur.slot, t["symbol"], "fuel_tank", None, t, True, cur.priority) for t in
                    sorted(tanks, key=lambda t: t["class"])[:1]]


def _swap(b, fsd, booster, tank):
    fitted = [f for f in b.fitted if f.kind not in ("fsd", "fuel_tank") and f.group != "gfsb"]
    fitted.append(fsd)
    if tank is not None:
        fitted.append(tank)
    if booster is not None:
        fitted.append(booster)
    return Build(b.ship, fitted, b.unknown)


def _describe(b, label_fsd, label_boost):
    r = calc.ranges(b)
    p = calc.power(b)
    f = b.first("fsd")
    return {"fsd": f.item, "fsd_rating": f.base.get("rating"), "fsd_mass": f.stat("mass"), "fsd_optmass": round(f.stat("optmass"), 1),
            "fsd_variant": label_fsd, "booster": label_boost, "booster_ly": calc.booster_bonus(b),
            "fuel_capacity": calc.fuel_capacity(b), "range_unladen": calc._r(r["unladen"]), "range_laden": calc._r(r["laden"]),
            "laden_mass": calc._r(r["laden_mass"]), "power_headroom_retracted": p["headroom_retracted"],
            "thrusters_ok": calc.thruster_ok(b, r["laden_mass"])}


def removal_gains(b):
    """What each module with mass costs in range: ly gained at laden mass if it were removed."""
    base_mass = calc.ranges(b)["laden_mass"]
    base = calc.jump_range(b, base_mass, calc.fuel_capacity(b))
    out = []
    for f in b.fitted:
        m = f.stat("mass")
        if m > 0 and f.kind not in ("fsd", "pp", "thrusters", "bulkhead"):
            gain = calc.jump_range(b, base_mass - m, calc.fuel_capacity(b)) - base
            out.append({"slot": f.slot, "item": f.item, "mass": round(m, 2), "range_gain_ly": round(gain, 2)})
    return sorted(out, key=lambda r: -r["range_gain_ly"])


def optimize_jump(b, cat, keep=(), objective="laden", assume_engineering=True, min_jumps_of_fuel=2, top=5):
    """Rank drive / booster / fuel-tank combinations by jump range; power and thruster limits are respected.

    keep: any of 'fsd', 'booster', 'fuel_tank' to leave alone. objective: 'laden' (full tank and hold) or 'unladen'.
    """
    keep = set(keep)
    if b.first("fsd") is None or not b.first("fsd").known:
        return {"error": "this build has no recognised frame shift drive"}
    key = "range_laden" if objective == "laden" else "range_unladen"
    fsds = [("current", b.first("fsd"))] if "fsd" in keep else _fsd_options(b, cat, assume_engineering)
    results = []
    for fl, fsd in fsds:
        min_fuel = fsd.stat("maxfuel") * min_jumps_of_fuel
        for bl, booster in _booster_options(b, cat, keep):
            for tank in _tank_options(b, cat, keep, min_fuel):
                cand = _swap(b, fsd, booster, tank)
                d = _describe(cand, fl, bl)
                d["tank"] = tank.item if tank is not None else None
                d["valid"] = d["power_headroom_retracted"] >= 0 and d["thrusters_ok"]
                results.append(d)
    current = _describe(b, "current", "current")
    valid = sorted((r for r in results if r["valid"]), key=lambda r: -(r[key] or 0))
    seen, ranked = set(), []
    for r in valid:                                  # drop duplicates that differ only in label
        sig = (r["fsd"], r["fsd_variant"], r["booster"], r["tank"])
        if sig not in seen:
            seen.add(sig)
            ranked.append(dict(r, delta_vs_current=round((r[key] or 0) - (current[key] or 0), 2)))
    assumptions = ["base-game maths: no heat, no SCO/overcharge behaviour; ranges in ly",
                   f"fuel tank sized for at least {min_jumps_of_fuel} jumps of the chosen drive" if "fuel_tank" not in keep else "fuel tank kept",
                   "every module you did not name stays exactly as fitted (including cargo racks)"]
    if assume_engineering:
        assumptions.append("candidate drives assumed FSD Increased Range grade 5 + Mass Manager at the best roll; your current drive uses its real values")
    return {"objective": objective, "current": current, "best": ranked[0] if ranked else None, "candidates": ranked[:top],
            "rejected_for_power_or_thrust": sum(1 for r in results if not r["valid"]),
            "removal_gains": removal_gains(b)[:8], "assumptions": assumptions}


# ---- max cargo (port of the Downloads templates) ---------------------------------------------------------------------

def _pick_default(entries, cls):
    cands = [e for e in entries if e.get("class") == cls]
    for rating in "CDEBA":
        for e in cands:
            if e.get("rating") == rating:
                return e
    return cands[0] if cands else None


def _rack(cat, cls, large=False):
    group = [m for m in cat.group("crl" if large else "cr") if m["class"] == cls]
    return max(group, key=lambda m: m.get("cargo", 0), default=None)


def cargo_slots(ship, cat):
    """Cargo capacity of each internal slot that takes a rack: [(class, capacity, general)]; general slots take any module."""
    out = []
    for entry in ship["slots"]["internal"]:
        if isinstance(entry, int):
            r = _rack(cat, entry)
            out.append((entry, r["cargo"] if r else 2 ** entry, True))
        elif isinstance(entry, dict) and any(k in (entry.get("eligible") or {}) for k in ("cr", "crl")):
            el = entry["eligible"]
            r = (_rack(cat, entry["class"], True) if "crl" in el else None) or _rack(cat, entry["class"])
            if r:
                out.append((entry["class"], r["cargo"], False))
    return out


def max_cargo_fit(ship, cat, reserve=2):
    """Racks in every free internal slot, the `reserve` smallest general slots kept for a docking computer and supercruise
    assist, and the drive that gives the best laden range. None if no drive lets the ship lift off laden."""
    slots = cargo_slots(ship, cat)
    general = sorted(s for s in slots if s[2])
    reserved = general[:reserve]
    rest = list(slots)
    for r in reserved:
        rest.remove(r)
    cargo_t = sum(s[1] for s in rest)
    std = ship["slots"]["standard"]
    stock = {"pp": ("pp", std[0]), "thrusters": ("t", std[1]), "ls": ("ls", std[3]), "pd": ("pd", std[4]),
             "sensors": ("s", std[5]), "fuel_tank": ("ft", std[6])}
    base = []
    for kind, (grp, cls) in stock.items():
        e = _pick_default(cat.group(grp), cls)
        if e:
            base.append(Fitted(kind, e["symbol"], kind, None, e))
    best = None
    for e in cat.group("fsd"):
        if e.get("class") != std[2]:
            continue
        b = Build(ship, base + [Fitted("fsd", e["symbol"], "fsd", None, e)])
        mass = calc.hull_mass(b) + calc.module_mass(b) + calc.fuel_capacity(b) + cargo_t
        if not calc.thruster_ok(b, mass):
            continue
        rng = calc.jump_range(b, mass, calc.fuel_capacity(b))
        if best is None or rng > best["range_ly"]:
            best = {"fsd": e["symbol"], "fsd_rating": e["rating"], "fsd_mass": e["mass"], "range_ly": round(rng, 2),
                    "laden_mass": round(mass, 1)}
    props = ship["properties"]
    return {"stem": ship["stem"], "name": props["name"], "manufacturer": props.get("manufacturer", ""),
            "pad": PAD_NAMES.get(props["class"], "unknown"), "cargo_t": cargo_t, "rack_slots": len(rest),
            "reserved_slots": len(reserved), "best_fsd": best}


def max_cargo(cat, ship=None, pad=None, reserve=2):
    """One ship (coriolis stem, journal key or name) or every ship, optionally only one landing pad size."""
    if ship:
        s = cat.ship(ship)
        if s is None:
            return {"error": f"unknown ship '{ship}' (see build-max-cargo --list)"}
        return {"ships": [max_cargo_fit(s, cat, reserve)], "notes": _CARGO_NOTES}
    rows = [max_cargo_fit(s, cat, reserve) for s in cat.ships.values()]
    if pad:
        rows = [r for r in rows if r["pad"] == pad]
    rows.sort(key=lambda r: (r["best_fsd"] is not None, r["best_fsd"]["range_ly"] if r["best_fsd"] else 0), reverse=True)
    return {"ships": rows, "notes": _CARGO_NOTES}


_CARGO_NOTES = ["racks in every free internal slot, the two smallest general slots kept for docking computer + supercruise assist",
                "range is the worst case: full hold and full tank, best FSD of any rating for the ship's drive class",
                "other standard modules are stock C-rated (or the nearest rating); no engineering is modelled"]
