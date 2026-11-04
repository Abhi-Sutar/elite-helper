"""Ship build maths, searches, data cache, CLI and MCP tools. No network: a hand-made catalogue stands in for coriolis-data."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest import mock

from builds_helpers import SHIP, catalogue, mod, panther_loadout, panther_modules
from eddn_helper import cli, mcp_server
from eddn_helper.builds import api, calc, data, model, optimize, suggest
from test_journal import ev
from test_journal_db import DbTest

CAT = catalogue()


def panther(changes=None):
    b = model.from_loadout(panther_loadout(), CAT)
    return b.with_changes(changes, CAT) if changes else b


class CalcTests(unittest.TestCase):
    def test_matches_what_the_game_reports(self):
        s = calc.summary(panther())
        self.assertEqual(s["unladen_mass"], 1593.0)            # journal UnladenMass
        self.assertEqual(s["jump_range_unladen"], 47.72)       # journal MaxJumpRange 47.724854 (one jump of fuel, booster included)
        self.assertEqual(s["cargo_capacity"], 1147)            # incl. the engineered 43 t rack and the Mk II racks
        self.assertEqual(s["fuel_capacity"], 128)
        self.assertEqual(s["guardian_booster_ly"], 10.5)
        self.assertEqual(s["unknown_modules"], [])

    def test_laden_range_is_lower_and_falls_with_cargo(self):
        s = calc.summary(panther())
        self.assertLess(s["jump_range_laden"], s["jump_range_unladen"])
        rs = [r["range"] for r in s["cargo_table"]]
        self.assertEqual(rs, sorted(rs, reverse=True))
        self.assertEqual(calc.summary(panther(), cargo=0)["jump_range_laden"], s["cargo_table"][0]["range"])

    def test_power_groups_and_shed_order(self):
        p = calc.power(panther())
        self.assertEqual(p["supply"], 33.6)
        self.assertAlmostEqual(p["retracted"], 21.609, 3)
        self.assertAlmostEqual(p["deployed"] - p["retracted"], 0.2, 3)       # the heat sink launcher only draws deployed
        self.assertEqual([g["group"] for g in p["groups"]], [1, 3])          # journal Priority 0 and 2 = groups 1 and 3
        self.assertEqual(p["shed_order"], [3, 1])
        self.assertEqual(p["groups"][-1]["cumulative_deployed"], p["deployed"])

    def test_modules_that_are_off_do_not_draw(self):
        loadout = panther_loadout()
        for m in loadout["Modules"]:
            if m["Slot"] == "Slot03_Size6":
                m["On"] = False
        b = model.from_loadout(loadout, CAT)
        self.assertAlmostEqual(calc.power(b)["retracted"], 21.609 - 4.34, 3)

    def test_what_if_swap_changes_mass_and_power(self):
        b = panther({"Slot04_Size6": "int_fuelscoop_size6_class5", "Slot03_Size6": None})
        self.assertEqual(calc.cargo_capacity(b), 1147 - 64)
        self.assertAlmostEqual(calc.power(b)["retracted"], 21.609 - 4.34 + 0.9, 3)
        self.assertGreater(calc.summary(b)["jump_range_unladen"], 47.72 - 0.01)

    def test_thrusters_must_lift_the_laden_mass(self):
        self.assertTrue(calc.thruster_ok(panther(), 3000))
        self.assertFalse(calc.thruster_ok(panther(), 6000))


class ModelTests(unittest.TestCase):
    def test_free_slots_are_the_hull_slots_the_loadout_does_not_list(self):
        self.assertEqual(panther().free_slots(), {"internal": [], "hardpoints": [3, 2, 1]})
        free = panther({"Slot04_Size6": None}).free_slots()
        self.assertEqual([s["size"] for s in free["internal"]], [6])
        self.assertEqual(free["hardpoints"], [3, 2, 1])       # tiny one is fitted
        cargo = panther({"Cargo01": None}).free_slots()["internal"]
        self.assertEqual((cargo[0]["name"], cargo[0]["size"]), ("Cargo", 8))

    def test_ship_lookup_aliases(self):
        self.assertEqual(CAT.ship("panthermkii")["stem"], "panther_clipper")
        self.assertEqual(CAT.ship("Panther Clipper Mk II")["stem"], "panther_clipper")
        self.assertIsNone(CAT.ship("nonsense"))

    def test_bad_input_gives_clear_errors(self):
        with self.assertRaisesRegex(model.BuildError, "not valid JSON"):
            model.from_loadout("{nope", CAT)
        with self.assertRaisesRegex(model.BuildError, "Loadout object"):
            model.from_loadout({"foo": 1}, CAT)
        with self.assertRaisesRegex(model.BuildError, "not in the coriolis-data"):
            model.from_loadout({"Ship": "zzz", "Modules": []}, CAT)
        with self.assertRaisesRegex(model.BuildError, "unknown module"):
            panther().with_changes({"Slot04_Size6": "int_madeup"}, CAT)

    def test_unknown_items_are_reported_not_fatal(self):
        loadout = panther_loadout()
        loadout["Modules"].append(mod("Slot11_Size1", "int_newmodule_size1_class1"))
        self.assertEqual(model.from_loadout(loadout, CAT).unknown, ["int_newmodule_size1_class1"])

    def test_edsy_style_list_and_json_text(self):
        b = model.from_loadout(json.dumps([panther_loadout()]), CAT)
        self.assertEqual(b.ship["stem"], "panther_clipper")


class OptimizeTests(unittest.TestCase):
    def test_jump_search_never_loses_to_the_current_fit(self):
        r = optimize.optimize_jump(panther(), CAT)
        self.assertGreaterEqual(r["best"]["delta_vs_current"], 0)
        self.assertEqual(r["current"]["range_unladen"], 47.72)
        self.assertTrue(all(c["valid"] for c in r["candidates"]))
        # a smaller tank than the 128 t one saves laden mass
        self.assertLess(r["best"]["fuel_capacity"], 128)

    def test_keep_leaves_parts_alone(self):
        r = optimize.optimize_jump(panther(), CAT, keep=("fsd", "booster", "fuel_tank"))
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(r["best"]["delta_vs_current"], 0)

    def test_candidates_that_overdraw_power_are_rejected(self):
        b = panther({"Slot07_Size5": None})           # no booster fitted, a size 5 slot free
        b.first("pp").mods["pgen"] = 20.0             # retracted draw is now 19.47 MW; a Guardian booster wants 2.14 more
        r = optimize.optimize_jump(b, CAT)
        self.assertGreater(r["rejected_for_power_or_thrust"], 0)
        self.assertTrue(all(c["power_headroom_retracted"] >= 0 for c in r["candidates"]))

    def test_new_drives_assumed_engineered_only_when_asked(self):
        with_eng = optimize.optimize_jump(panther(), CAT, keep=("booster", "fuel_tank"), top=50)
        without = optimize.optimize_jump(panther(), CAT, keep=("booster", "fuel_tank"), assume_engineering=False, top=50)
        self.assertTrue(any("engineered" in c["fsd_variant"] for c in with_eng["candidates"]))
        self.assertFalse(any("engineered" in c["fsd_variant"] for c in without["candidates"]))

    def test_removal_gains_rank_heavy_modules_first(self):
        gains = optimize.removal_gains(panther())
        self.assertEqual(gains[0]["slot"], "Slot03_Size6")      # the 40 t shield
        self.assertGreater(gains[0]["range_gain_ly"], 0)

    def test_max_cargo_port(self):
        r = optimize.max_cargo(CAT, "tiny_hauler")["ships"][0]
        # general slots 1,2,3,4: the two smallest (1,2) are kept back; class 3 + 4 racks = 8 + 16; the cargo slot (class 5) = 32
        self.assertEqual((r["cargo_t"], r["rack_slots"], r["reserved_slots"]), (8 + 16 + 32, 3, 2))
        self.assertIsNotNone(r["best_fsd"])
        self.assertEqual(r["pad"], "medium")
        self.assertEqual([x["stem"] for x in optimize.max_cargo(CAT, pad="medium")["ships"]], ["tiny_hauler"])
        self.assertIn("error", optimize.max_cargo(CAT, "nope"))

    def test_mk2_racks_count_in_restricted_cargo_slots(self):
        r = optimize.max_cargo(CAT, "panther_clipper")["ships"][0]
        # restricted slots take the Mk II racks (384 + 192); general slots 8,7,6,6,6,5,5,4 minus the two smallest (1,2 are not
        # in this fixture's hull list order) -> just check the Mk II racks are included
        self.assertGreaterEqual(r["cargo_t"], 384 + 192)


class SuggestTests(unittest.TestCase):
    def test_free_slot_options_respect_size_and_power(self):
        b = panther({"Slot04_Size6": None})
        r = suggest.suggest_additions(b, CAT)
        self.assertEqual(r["free_internal_slots"], 1)
        roles = {o["role"] for o in r["slots"][0]["options"]}
        self.assertIn("fuel scoop", roles)
        self.assertNotIn("FSD booster", roles)                 # one booster is already fitted
        self.assertTrue(all(o["class"] <= 6 for o in r["slots"][0]["options"]))
        scoop = next(o for o in r["slots"][0]["options"] if o["role"] == "fuel scoop")
        self.assertTrue(scoop["fits_power"])
        self.assertAlmostEqual(scoop["headroom_after"], r["power_headroom_retracted"] - scoop["power"], 3)

    def test_power_that_is_not_there_is_flagged(self):
        b = panther({"Slot04_Size6": None})
        b.first("pp").mods["pgen"] = 21.7
        opts = suggest.suggest_additions(b, CAT, role="shield")["slots"][0]["options"]
        self.assertTrue(opts)
        self.assertFalse(any(o["fits_power"] for o in opts))   # 0.09 MW spare cannot run any shield generator

    def test_replace_slots_are_treated_as_given_up(self):
        r = suggest.suggest_additions(panther(), CAT, replace_slots=["Slot04_Size6", "Slot05_Size6"])
        self.assertEqual(r["free_internal_slots"], 2)

    def test_restricted_cargo_slot_only_offers_cargo(self):
        r = suggest.suggest_additions(panther({"Cargo01": None}), CAT)
        slot = r["slots"][0]
        self.assertEqual(slot["restricted_to"], ["cr", "crl", "ft"])
        self.assertEqual({o["role"] for o in slot["options"]}, {"cargo"})

    def test_priority_problem_when_essentials_rank_below_conveniences(self):
        loadout = panther_loadout()
        for m in loadout["Modules"]:
            if m["Slot"] == "FrameShiftDrive":
                m["Priority"] = 4
            if m["Slot"] == "Slot09_Size2":
                m["Priority"] = 0
        rep = suggest.priority_report(model.from_loadout(loadout, CAT))
        self.assertTrue(any("fsd" in p and "docking computer" in p for p in rep["problems"]))

    def test_good_priorities_report_no_problems_and_shedding_names_the_casualties(self):
        rep = suggest.priority_report(panther())
        self.assertEqual(rep["problems"], [])
        self.assertTrue(rep["retracted"]["ok"])
        b = panther()
        b.first("pp").mods["pgen"] = 21.0                       # retracted draw is 21.609
        over = suggest.priority_report(b)["retracted"]
        self.assertFalse(over["ok"])
        self.assertEqual({m["role"] for m in over["shed"]}, {"docking computer", "supercruise assist"})   # group 3 goes first
        self.assertEqual(over["tier_1_2_lost"], [])


class DataTests(unittest.TestCase):
    def zip_bytes(self):
        buf = io.BytesIO()
        ship = {"panthermkii": {"properties": SHIP["properties"], "slots": SHIP["slots"], "bulkheads": SHIP["bulkheads"]}}
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("coriolis-data-master/ships/panther_clipper.json", json.dumps(ship))
            z.writestr("coriolis-data-master/modules/standard/power_plant.json", json.dumps(
                {"pp": [{"symbol": "Int_Powerplant_Size7_Class5", "grp": "pp", "class": 7, "rating": "A", "mass": 40, "pgen": 30,
                         "ukName": "Power Plant", "ukDiscript": "x"},
                        {"symbol": "Int_Missing_PP", "grp": "pp", "class": 1, "rating": "Z"}]}))
            z.writestr("coriolis-data-master/modifications/blueprints.json", json.dumps(
                {"FSD_LongRange": {"grades": {"5": {"features": {"mass": [0.3, 0.3]}}}}}))
        return buf.getvalue()

    def test_build_catalogue_keeps_what_is_needed(self):
        cat = data.Catalogue(data.build_catalogue(self.zip_bytes()))
        self.assertEqual(cat.ship("panthermkii")["key"], "panthermkii")
        pp = cat.module("INT_POWERPLANT_SIZE7_CLASS5")
        self.assertEqual((pp["pgen"], pp["name"]), (30, "Power Plant"))
        self.assertNotIn("ukDiscript", pp)
        self.assertIsNone(cat.module("int_missing_pp"))
        self.assertEqual(cat.blueprints["FSD_LongRange"]["5"]["mass"], [0.3, 0.3])
        with self.assertRaises(data.CatalogueError):
            data.build_catalogue(io.BytesIO().getvalue() or self._empty_zip())

    def _empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x/readme.md", "hi")
        return buf.getvalue()

    def test_cache_download_reuse_and_stale_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            data._cache["cat"] = None
            calls = []

            def fetch():
                calls.append(1)
                return self.zip_bytes()
            cat = data.load(fetch=fetch)
            self.assertEqual((len(calls), cat.status()["ships"]), (1, 1))
            self.assertTrue(data.cache_path().exists())
            data._cache["cat"] = None
            data.load(fetch=fetch)                                   # read from disk, no second download
            self.assertEqual(len(calls), 1)
            # stale cache + failing network: the old copy is used
            raw = json.loads(data.cache_path().read_text())
            raw["fetched_at"] = "2020-01-01T00:00:00+00:00"
            data.cache_path().write_text(json.dumps(raw))
            data._cache["cat"] = None

            def broken():
                raise data.CatalogueError("offline")
            self.assertEqual(data.load(fetch=broken).status()["ships"], 1)
            # nothing cached and no network: an error, not a crash
            data.cache_path().unlink()
            data._cache["cat"] = None
            with self.assertRaises(data.CatalogueError):
                data.load(fetch=broken)
            data._cache["cat"] = None


class JournalBackedTests(DbTest):
    def setUp(self):
        super().setUp()
        self.journal("2026-10-11T100000.01", [
            ev(0, "Fileheader"), ev(0, "Commander", Name="CMDR Test", FID="F1"),
            ev(1, "Loadout", **{k: v for k, v in panther_loadout().items() if k != "event"})])
        self.patch = mock.patch.object(api, "_cat", return_value=CAT)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.q = dict(directory=str(self.jdir))

    def test_show_agrees_with_the_journal(self):
        r = api.show(self.session, **self.q)
        self.assertTrue(r["found"])
        self.assertEqual(r["check"], {"unladen_mass_diff": 0.0, "jump_range_diff": 0.0, "cargo_diff": 0.0})
        self.assertEqual(r["ship_name"], "Le Big Plipper")
        self.assertEqual(r["build"]["power"]["groups"][0]["group"], 1)
        self.assertEqual({m["slot"] for m in r["build"]["modules"]} >= {"FrameShiftDrive", "Slot03_Size6"}, True)

    def test_swaps_and_ship_id(self):
        r = api.show(self.session, swaps={"Slot03_Size6": ""}, **self.q)
        self.assertGreater(r["build"]["jump_range_unladen"], 47.72)
        self.assertNotIn("check", r)
        self.assertFalse(api.show(self.session, ship_id=99, **self.q)["found"])

    def test_suggest_priorities_jump(self):
        s = api.suggest_for(self.session, replace_slots=["Slot04_Size6"], **self.q)
        self.assertEqual(s["free_internal_slots"], 1)
        self.assertEqual(api.priorities(self.session, **self.q)["problems"], [])
        j = api.jump(self.session, **self.q)
        self.assertGreaterEqual(j["best"]["delta_vs_current"], 0)
        self.assertIn("journal_freshness", j)

    def test_missing_catalogue_is_an_error_reply(self):
        with mock.patch.object(api, "_cat", side_effect=data.CatalogueError("offline")):
            r = api.show(self.session, **self.q)
        self.assertEqual((r["found"], r["error"]), (False, "offline"))

    def test_cli_and_mcp(self):
        api.show(self.session, **self.q)                      # ingest the journal; the CLI below runs with --no-sync
        args = cli.build_parser().parse_args(["build-show", "--no-sync", "--json"])
        args.journal_dir = str(self.jdir)
        out = io.StringIO()
        with redirect_stdout(out):
            args.func(args, self.session)
        self.assertEqual(json.loads(out.getvalue())["build"]["jump_range_unladen"], 47.72)
        with mock.patch.object(mcp_server, "_run", side_effect=lambda fn, *a, **k: fn(self.session, *a, directory=str(self.jdir), **k)):
            self.assertEqual(mcp_server.build_current()["build"]["cargo_capacity"], 1147)
            self.assertEqual(mcp_server.build_priorities()["problems"], [])


class PureToolTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(api, "_cat", return_value=CAT)
        p.start()
        self.addCleanup(p.stop)

    def test_check_pasted_loadout(self):
        r = api.check(json.dumps(panther_loadout()))
        self.assertTrue(r["found"])
        self.assertEqual(r["build"]["jump_range_unladen"], 47.72)
        self.assertEqual(r["priorities"]["problems"], [])
        r = api.check(panther_loadout(), swaps={"PowerPlant": "int_powerplant_size3_class3"})
        self.assertFalse(r["priorities"]["retracted"]["ok"])      # an 8 MW plant cannot run this ship
        self.assertFalse(api.check("[]")["found"])

    def test_cli_check_and_max_cargo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(panther_loadout(), f)
        self.addCleanup(os.unlink, f.name)
        args = cli.build_parser().parse_args(["build-check", f.name, "--json"])
        self.assertTrue(args.raw)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(args.func(args, None), 0)
        self.assertEqual(json.loads(out.getvalue())["build"]["cargo_capacity"], 1147)
        args = cli.build_parser().parse_args(["build-max-cargo", "--ship", "tiny_hauler"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(args.func(args, None), 0)
        self.assertIn("Tiny Hauler", out.getvalue())

    def test_mcp_tools(self):
        self.assertTrue(mcp_server.build_check(panther_loadout())["found"])
        self.assertEqual(mcp_server.build_max_cargo(pad="medium")["ships"][0]["name"], "Tiny Hauler")
        with mock.patch.object(data, "load", return_value=CAT):
            self.assertEqual(mcp_server.build_data_status()["ships"], 2)


if __name__ == "__main__":
    unittest.main()
