"""
A ship build: the hull plus the fitted modules, read from a journal Loadout event (yours, or Loadout JSON exported by
Coriolis / EDSY). Slots the game does not list in a Loadout are empty; `free_slots` works them out from the hull data.
"""

import json
import re
from dataclasses import dataclass, field, replace

HARDPOINT_SIZES = {"tiny": 0, "small": 1, "medium": 2, "large": 3, "huge": 4}
STANDARD_KINDS = {"PowerPlant": "pp", "MainEngines": "thrusters", "FrameShiftDrive": "fsd", "LifeSupport": "ls",
                  "PowerDistributor": "pd", "Radar": "sensors", "FuelTank": "fuel_tank"}
# Journal engineering modifier label -> the catalogue field it replaces.
LABEL_STATS = {"Mass": "mass", "PowerDraw": "power", "PowerCapacity": "pgen", "FSDOptimalMass": "optmass",
               "MaxFuelPerJump": "maxfuel", "CargoCapacity": "cargo", "EngineOptimalMass": "optmass"}
# Blueprint feature -> catalogue field (features are fractional changes, e.g. mass +0.3).
FEATURE_STATS = {"mass": "mass", "power": "power", "pgen": "pgen", "optmass": "optmass", "cargo": "cargo",
                 "maxfuel": "maxfuel"}
# Experimental effects that change range maths and are not in the blueprint features (fractional change).
EXPERIMENTALS = {"special_fsd_heavy": {"optmass": 0.04}}


class BuildError(Exception):
    pass


def slot_kind(slot):
    """(kind, size_or_name) for a journal slot name. kind: a standard key, bulkhead, hardpoint, internal or other."""
    if slot in STANDARD_KINDS:
        return STANDARD_KINDS[slot], None
    if slot == "Armour":
        return "bulkhead", None
    m = re.match(r"(Tiny|Small|Medium|Large|Huge)Hardpoint\d*$", slot)
    if m:
        return "hardpoint", HARDPOINT_SIZES[m.group(1).lower()]
    m = re.match(r"Slot\d+_Size(\d+)$", slot)
    if m:
        return "internal", int(m.group(1))
    m = re.match(r"([A-Za-z]+?)\d*$", slot)
    if m and m.group(1) in ("Cargo", "Military", "PlanetaryApproachSuite"):
        return "internal", m.group(1)
    return "other", None


@dataclass
class Fitted:
    slot: str
    item: str
    kind: str
    size: object = None            # slot size (int) or restricted-slot name
    base: dict = field(default_factory=dict)   # catalogue entry ({} if unknown)
    on: bool = True
    priority: int = 0              # journal value: 0 = in-game priority group 1
    mods: dict = field(default_factory=dict)   # engineered values by catalogue field
    engineering: dict = None       # {blueprint, level, quality, experimental}

    def stat(self, name, default=0):
        if name in self.mods:
            return self.mods[name]
        return self.base.get(name, default)

    @property
    def group(self):
        return self.base.get("grp")

    @property
    def known(self):
        return bool(self.base)


@dataclass
class Build:
    ship: dict                     # catalogue ship
    fitted: list
    unknown: list = field(default_factory=list)   # items the catalogue does not know (mass/power missing)

    def get(self, slot):
        return next((f for f in self.fitted if f.slot == slot), None)

    def of_kind(self, kind):
        return [f for f in self.fitted if f.kind == kind]

    def first(self, kind):
        return next(iter(self.of_kind(kind)), None)

    def with_changes(self, changes, cat):
        """Copy with {slot: item symbol or None (remove)}. New items are unengineered and keep the slot's priority."""
        fitted = list(self.fitted)
        for slot, item in changes.items():
            old = next((f for f in fitted if f.slot == slot), None)
            if old is not None:
                fitted.remove(old)
            if item:
                kind, size = slot_kind(slot)
                base = cat.module(item)
                if base is None:
                    raise BuildError(f"unknown module '{item}' (not in the coriolis-data catalogue)")
                fitted.append(Fitted(slot, item, kind, size, base, True, old.priority if old else 0))
        return replace(self, fitted=fitted)

    def free_slots(self):
        """Empty internal and hardpoint slots: hull slots minus the ones a Loadout lists."""
        ship_slots = self.ship["slots"]
        # What the Loadout occupies. A sized slot (Slot03_Size6) is matched on its size. A named slot such as Cargo has no
        # size in its journal name, so it is matched on the fitted module's class first, then in hull order.
        sized, named = {}, {}
        for f in self.fitted:
            if f.kind != "internal":
                continue
            if isinstance(f.size, int):
                sized[f.size] = sized.get(f.size, 0) + 1
            else:
                named.setdefault(f.size, []).append(f.base.get("class"))
        internal, hull_named = [], {}
        for entry in ship_slots["internal"]:
            if isinstance(entry, int):
                if sized.get(entry, 0) > 0:
                    sized[entry] -= 1
                else:
                    internal.append({"size": entry, "name": None, "eligible": None})
            else:
                hull_named.setdefault(entry.get("name"), []).append(entry)
        for name, entries in hull_named.items():
            classes = list(named.get(name, []))
            left = []
            for e in entries:                              # exact class matches first
                if e.get("class") in classes:
                    classes.remove(e.get("class"))
                else:
                    left.append(e)
            left = left[len(classes):]                      # remaining occupants take the next hull slots in order
            internal.extend({"size": e.get("class"), "name": name, "eligible": e.get("eligible")} for e in left)
        used_h = {}
        for f in self.fitted:
            if f.kind == "hardpoint":
                used_h[f.size] = used_h.get(f.size, 0) + 1
        hardpoints = []
        for size in ship_slots["hardpoints"]:
            if used_h.get(size, 0) > 0:
                used_h[size] -= 1
            else:
                hardpoints.append(size)
        return {"internal": internal, "hardpoints": hardpoints}


def _mods_from_journal(eng):
    out = {}
    for m in eng.get("Modifiers") or []:
        stat = LABEL_STATS.get(m.get("Label"))
        if stat and m.get("Value") is not None:
            out[stat] = float(m["Value"])
    return out


def engineered_mods(base, features, experimental=None):
    """Catalogue-field values for a blueprint grade at the best roll (second end of each feature range)."""
    out = {}
    for feat, rng in (features or {}).items():
        stat = FEATURE_STATS.get(feat)
        if stat and stat in base:
            frac = rng[-1] if isinstance(rng, (list, tuple)) else rng
            out[stat] = base[stat] * (1 + frac)
    for stat, frac in EXPERIMENTALS.get(experimental or "", {}).items():
        if stat in base or stat in out:
            out[stat] = out.get(stat, base[stat]) * (1 + frac)
    return out


def _bulkhead(ship, item):
    m = re.search(r"grade(\d)", item or "")
    idx = int(m.group(1)) - 1 if m else 0
    bh = ship.get("bulkheads") or []
    entry = bh[idx] if 0 <= idx < len(bh) else {}
    return {"mass": entry.get("mass", 0), "name": entry.get("name", item), "grp": "bh"}


def from_loadout(data, cat):
    """Build from a Loadout event dict (journal, Coriolis or EDSY export)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise BuildError(f"that is not valid JSON: {e}") from e
    if isinstance(data, list):
        loads = [d for d in data if isinstance(d, dict) and d.get("Modules")]
        if not loads:
            raise BuildError("no Loadout with a Modules list found in that JSON")
        data = loads[0]
    if not isinstance(data, dict) or "Modules" not in data:
        raise BuildError("expected a Loadout object with Ship and Modules (journal event, Coriolis or EDSY export)")
    ship = cat.ship(data.get("Ship"))
    if ship is None:
        raise BuildError(f"ship '{data.get('Ship')}' is not in the coriolis-data catalogue")
    fitted, unknown = [], []
    for m in data["Modules"]:
        slot, item = m.get("Slot", ""), m.get("Item", "")
        kind, size = slot_kind(slot)
        if kind == "other":
            continue
        base = _bulkhead(ship, item) if kind == "bulkhead" else (cat.module(item) or {})
        eng = m.get("Engineering")
        f = Fitted(slot, item, kind, size, base, m.get("On", True), int(m.get("Priority", 0) or 0))
        if eng:
            f.mods = _mods_from_journal(eng)
            f.engineering = {"blueprint": eng.get("BlueprintName"), "level": eng.get("Level"),
                             "quality": eng.get("Quality"), "experimental": eng.get("ExperimentalEffect_Localised")}
        if not base:
            unknown.append(item)
        fitted.append(f)
    return Build(ship, fitted, unknown)
