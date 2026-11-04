"""A small hand-made coriolis-data catalogue (real numbers for the Panther Clipper Mk II fit) and its journal Loadout."""
from eddn_helper.builds import data

SHIP = {
    "stem": "panther_clipper", "key": "panthermkii",
    "properties": {"name": "Panther Clipper Mk II", "manufacturer": "Zorgon Peterson", "class": 3, "hullMass": 1200},
    "slots": {"standard": [8, 8, 7, 5, 7, 5, 7], "hardpoints": [3, 2, 1, 0],
              "internal": [{"class": 8, "name": "Cargo", "eligible": {"cr": 1, "crl": 1, "ft": 1}}, 8,
                           {"class": 7, "name": "Cargo", "eligible": {"cr": 1, "crl": 1, "ft": 1}}, 7, 6, 6, 6, 5, 5, 4, 2, 1,
                           {"class": 1, "name": "PlanetaryApproachSuite", "eligible": {"pas": 1}}]},
    "bulkheads": [{"name": "Lightweight Alloy", "mass": 0}, {"name": "Reinforced Alloy", "mass": 75}],
}
SMALL = {   # a tiny ship with two general slots and one cargo-only slot, for the max-cargo port
    "stem": "tiny_hauler", "key": "tinyhauler",
    "properties": {"name": "Tiny Hauler", "manufacturer": "Test", "class": 2, "hullMass": 100},
    "slots": {"standard": [3, 3, 3, 2, 3, 2, 3], "hardpoints": [1],
              "internal": [4, 3, 2, 1, {"class": 5, "name": "Cargo", "eligible": {"cr": 1, "crl": 1}}]},
    "bulkheads": [{"name": "Lightweight Alloy", "mass": 0}],
}


def m(symbol, grp, cls, rating, **kw):
    return {"symbol": symbol, "grp": grp, "class": cls, "rating": rating, **kw}


def modules():
    out = [
        m("Int_Powerplant_Size7_Class5", "pp", 7, "A", mass=40, pgen=30),
        m("Int_Engine_Size8_Class5", "t", 8, "A", mass=160, power=10.8, optmass=3360, maxmass=5040),
        m("Int_Hyperdrive_Overcharge_Size7_Class5", "fsd", 7, "A", mass=80, power=0.9, optmass=3000, maxfuel=13.1, fuelmul=0.013, fuelpower=2.75),
        m("Int_Hyperdrive_Size7_Class2", "fsd", 7, "D", mass=32, power=0.54, optmass=1620, maxfuel=8.5, fuelmul=0.01, fuelpower=2.75),
        m("Int_Hyperdrive_Size7_Class5", "fsd", 7, "A", mass=80, power=0.9, optmass=2700, maxfuel=10.6, fuelmul=0.012, fuelpower=2.75),
        m("Int_Hyperdrive_Overcharge_Size5_Class5", "fsd", 5, "A", mass=20, power=0.6, optmass=1000, maxfuel=5.2, fuelmul=0.012, fuelpower=2.45),
        m("Int_Hyperdrive_Overcharge_Size3_Class5", "fsd", 3, "A", mass=5, power=0.3, optmass=300, maxfuel=2.6, fuelmul=0.012, fuelpower=2.45),
        m("Int_Hyperdrive_Overcharge_Size3_Class3", "fsd", 3, "C", mass=13, power=0.3, optmass=270, maxfuel=2.6, fuelmul=0.012, fuelpower=2.45),
        m("Int_Engine_Size3_Class3", "t", 3, "C", mass=10, power=1.0, optmass=300, maxmass=500),
        m("Int_Powerplant_Size3_Class3", "pp", 3, "C", mass=10, pgen=8),
        m("Int_LifeSupport_Size5_Class2", "ls", 5, "D", mass=8, power=0.64),
        m("Int_LifeSupport_Size2_Class3", "ls", 2, "C", mass=2, power=0.3),
        m("Int_PowerDistributor_Size7_Class2", "pd", 7, "D", mass=32, power=0.67),
        m("Int_PowerDistributor_Size3_Class3", "pd", 3, "C", mass=4, power=0.3),
        m("Int_Sensors_Size5_Class2", "s", 5, "D", mass=8, power=0.37),
        m("Int_Sensors_Size2_Class3", "s", 2, "C", mass=1, power=0.2),
        m("Int_FuelTank_Size7_Class3", "ft", 7, "C", fuel=128),
        m("Int_FuelTank_Size6_Class3", "ft", 6, "C", fuel=64),
        m("Int_FuelTank_Size5_Class3", "ft", 5, "C", fuel=32),
        m("Int_FuelTank_Size4_Class3", "ft", 4, "C", fuel=16),
        m("Int_FuelTank_Size3_Class3", "ft", 3, "C", fuel=8),
        m("Int_FuelTank_Size2_Class3", "ft", 2, "C", fuel=4),
        m("Int_LargeCargoRack_Size8_Class1", "crl", 8, "D", mass=0, cargo=384, restriction="Cargo"),
        m("Int_LargeCargoRack_Size7_Class1", "crl", 7, "D", mass=0, cargo=192, restriction="Cargo"),
        m("Int_ShieldGenerator_Size6_Class5", "sg", 6, "A", mass=40, power=4.34),
        m("Int_ShieldGenerator_Size4_Class3", "sg", 4, "C", mass=10, power=1.5),
        m("Int_GuardianFSDBooster_Size5", "gfsb", 5, "H", mass=1.3, power=2.14, jumpboost=10.5),
        m("Int_GuardianFSDBooster_Size4", "gfsb", 4, "H", mass=1.3, power=1.65, jumpboost=8),
        m("Int_FuelScoop_Size6_Class5", "fs", 6, "A", power=0.9),
        m("Int_FuelScoop_Size4_Class5", "fs", 4, "A", power=0.6),
        m("Int_DockingComputer_Advanced", "dc", 1, "E", power=0.45),
        m("Int_SupercruiseAssist", "sua", 1, "E", mass=0, power=0.3),
        m("Int_PlanetApproachSuite_Advanced", "pas", 1, "I", mass=0, power=0),
        m("Hpt_HeatSinkLauncher_Turret_Tiny", "hs", 0, "I", mass=1.3, power=0.2),
        m("Hpt_BeamLaser_Fixed_Large", "bl", 3, "A", mass=16, power=2.6),
    ]
    for c in range(1, 9):
        out.append(m(f"Int_CargoRack_Size{c}_Class1", "cr", c, "E", mass=0, cargo=2 ** c))
    return out


def catalogue(extra_ships=()):
    ships = {s["stem"]: s for s in (SHIP, SMALL, *extra_ships)}
    return data.Catalogue({"fetched_at": "2026-11-04T00:00:00+00:00", "source": "test", "ships": ships,
                           "modules": {e["symbol"].lower(): e for e in modules()}, "blueprints": {
        "FSD_LongRange": {"5": {"mass": [0.3, 0.3], "optmass": [0.45, 0.55], "power": [0.15, 0.15]}}}})


def mod(slot, item, priority=0, **eng):
    d = {"Slot": slot, "Item": item, "On": True, "Priority": priority}
    if eng:
        d["Engineering"] = {"BlueprintName": eng.get("blueprint", "X"), "Level": 5, "Quality": 1.0,
                            "Modifiers": [{"Label": k, "Value": v} for k, v in eng["mods"].items()]}
    return d


def panther_modules():
    return [
        mod("TinyHardpoint1", "hpt_heatsinklauncher_turret_tiny", 0),
        mod("Armour", "panthermkii_armour_grade1", 1),
        mod("PowerPlant", "int_powerplant_size7_class5", 1, mods={"PowerCapacity": 33.6}),
        mod("MainEngines", "int_engine_size8_class5", 0, mods={"PowerDraw": 11.664, "EngineOptimalMass": 3108}),
        mod("FrameShiftDrive", "int_hyperdrive_overcharge_size7_class5", 0,
            mods={"Mass": 104, "PowerDraw": 1.035, "FSDOptimalMass": 4836}),
        mod("LifeSupport", "int_lifesupport_size5_class2", 0), mod("PowerDistributor", "int_powerdistributor_size7_class2", 0),
        mod("Radar", "int_sensors_size5_class2", 0, mods={"Mass": 6.4}), mod("FuelTank", "int_fueltank_size7_class3", 1),
        mod("Cargo01", "int_largecargorack_size8_class1", 1), mod("Slot01_Size8", "int_cargorack_size8_class1", 1),
        mod("Cargo02", "int_largecargorack_size7_class1", 1), mod("Slot02_Size7", "int_cargorack_size7_class1", 1),
        mod("Slot03_Size6", "int_shieldgenerator_size6_class5", 0), mod("Slot04_Size6", "int_cargorack_size6_class1", 1),
        mod("Slot05_Size6", "int_cargorack_size6_class1", 1),
        mod("Slot06_Size5", "int_cargorack_size5_class1", 1, mods={"CargoCapacity": 43}),
        mod("Slot07_Size5", "int_guardianfsdbooster_size5", 0), mod("Slot08_Size4", "int_cargorack_size4_class1", 1),
        mod("Slot09_Size2", "int_dockingcomputer_advanced", 2), mod("Slot10_Size1", "int_supercruiseassist", 2),
        mod("PlanetaryApproachSuite", "int_planetapproachsuite_advanced", 1), mod("VesselVoice", "voicepack_verity", 1),
    ]


def panther_loadout(**top):
    return {"event": "Loadout", "Ship": "panthermkii", "ShipID": 10, "ShipName": "Le Big Plipper", "ShipIdent": "KI-22P",
            "UnladenMass": 1593.0, "MaxJumpRange": 47.724854, "CargoCapacity": 1147, "FuelCapacity": {"Main": 128.0, "Reserve": 1.11},
            "Modules": panther_modules(), **top}
