"""Recce config: schema validation with clear messages, defaults, merge patches, CLI --set parsing, file storage."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from eddn_helper.recce import cli
from eddn_helper.recce import config as rcfg


class ValidateTests(unittest.TestCase):
    def errors(self, raw):
        with self.assertRaises(rcfg.ConfigError) as cm:
            rcfg.validate(raw)
        return cm.exception.errors

    def test_default_config_is_valid_and_matches_the_brief(self):
        cfg = rcfg.validate(rcfg.DEFAULT_CONFIG)
        f = cfg["filters"]
        self.assertEqual((f["economy"], f["min_population"], f["controlling_only"], f["player_faction_only"],
                          f["faction_state"], f["kinds"]), ("Agriculture", 1000000, True, True, "Expansion",
                                                            ["pending", "active"]))
        self.assertEqual((cfg["origin"], cfg["radius_ly"], cfg["refresh_seconds"]), ("current", 100, 20))
        self.assertEqual(cfg["columns"], ["system", "faction", "economy", "pending_for", "state", "expansion_chance",
                                       "last_update"])
        self.assertIs(cfg["expansion_estimate"], True)

    def test_expansion_settings_are_validated_and_old_configs_still_load(self):
        old = rcfg.validate({"name": "old", "columns": ["system", "faction", "last_update"]})
        self.assertEqual(old["columns"], ["system", "faction", "last_update"])
        self.assertIs(old["expansion_estimate"], True)
        self.assertEqual(rcfg.validate({"columns": ["system", "expansion_chance"]})["columns"], ["system", "expansion_chance"])
        self.assertTrue(any(e.startswith("expansion_estimate") for e in self.errors({"expansion_estimate": "yes"})))

    def test_minimal_config_gets_defaults(self):
        cfg = rcfg.validate({"name": "mine", "radius_ly": 40})
        self.assertEqual((cfg["name"], cfg["radius_ly"], cfg["filters"]["faction_state"]), ("mine", 40, "Expansion"))

    def test_refresh_below_five_seconds_is_refused_with_a_clear_message(self):
        self.assertEqual(self.errors({"refresh_seconds": 4}), ["refresh_seconds: must be at least 5, got 4"])

    def test_every_problem_is_listed_with_its_path(self):
        errs = self.errors({"name": "Bad Name", "radius_ly": -3, "filters": {"state_in": "soon", "min_population": -1},
                            "columns": ["system", "nope"], "sort": {"by": "colour"}, "bogus": 1})
        errs = errs + self.errors({"name": "Bad Name", "radius_ly": -3, "filters": {"state_in": "soon",
                                                                                     "min_population": -1},
                                   "columns": ["system", "nope"], "sort": {"by": "colour"}})
        text = "\n".join(errs)
        for needle in ("bogus: unknown setting", "name: must be", "radius_ly: must be at least 1",
                       "filters.state_in: must be one of any, pending, active, recovering; got 'soon'",
                       "filters.min_population: must be at least 0", "columns[1]: unknown column 'nope'",
                       "sort.by: must be one of"):
            self.assertIn(needle, text)

    def test_type_mistakes(self):
        self.assertIn("filters.controlling_only: must be true or false",
                      self.errors({"filters": {"controlling_only": "yes"}})[0])
        self.assertIn("radius_ly: must be a number", self.errors({"radius_ly": "far"})[0])
        self.assertIn("radius_ly: must be a number", self.errors({"radius_ly": True})[0])    # bool is not a number
        self.assertIn("filters: must be an object", self.errors({"filters": [1]})[0])
        self.assertIn("config: must be a JSON object", self.errors([])[0])

    def test_unknown_filter_column_duplicate_and_empty_columns(self):
        self.assertIn("filters.colour: unknown filter", self.errors({"filters": {"colour": 1}})[0])
        self.assertIn("listed more than once", self.errors({"columns": ["system", "system"]})[0])
        self.assertIn("columns: must be a non-empty list", self.errors({"columns": []})[0])

    def test_highlight_rules_and_live_fetch_cap(self):
        self.assertIn("highlight_rules[0].when", self.errors({"highlight_rules": [{"when": "soon", "hours": 1}]})[0])
        self.assertIn("highlight_rules[0].hours",
                      "\n".join(self.errors({"highlight_rules": [{"when": "active_for_under_hours"}]})))
        self.assertEqual(self.errors({"live_fetch": {"max_requests": 11}}),
                         ["live_fetch.max_requests: must be at most 10, got 11"])
        self.assertEqual(rcfg.validate({"live_fetch": {"max_requests": 10}})["live_fetch"]["max_requests"], 10)

    def test_state_in_other_than_any_needs_a_state(self):
        errs = self.errors({"filters": {"faction_state": None, "state_in": "pending"}})
        self.assertIn("needs filters.faction_state", errs[0])

    def test_economy_forms(self):
        for ok in ("Agriculture", ["Agriculture", "Industrial"], None):
            rcfg.validate({"filters": {"economy": ok}})
        self.assertIn("filters.economy", self.errors({"filters": {"economy": 5}})[0])


class PatchTests(unittest.TestCase):
    def test_merge_objects_replace_lists_null_clears(self):
        base = rcfg.validate(rcfg.DEFAULT_CONFIG)
        out = rcfg.apply_patch(base, {"filters": {"min_population": 5}, "columns": ["system"], "title": "T"})
        self.assertEqual(out["filters"]["min_population"], 5)
        self.assertTrue(out["filters"]["controlling_only"])         # untouched sibling kept
        self.assertEqual(out["columns"], ["system"])
        cleared = rcfg.validate(rcfg.apply_patch(base, {"filters": {"economy": None}}))
        self.assertIsNone(cleared["filters"]["economy"])

    def test_set_path_and_value_parsing(self):
        base = rcfg.validate(rcfg.DEFAULT_CONFIG)
        out = rcfg.set_path(base, "filters.min_population", rcfg.parse_value("500000"))
        self.assertEqual(out["filters"]["min_population"], 500000)
        self.assertIs(rcfg.parse_value("true"), True)
        self.assertEqual(rcfg.parse_value("Agriculture"), "Agriculture")
        self.assertEqual(rcfg.parse_value('["system","faction"]'), ["system", "faction"])
        self.assertIsNone(rcfg.parse_value("null"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {rcfg.ENV_DIR: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        journal = mock.patch("eddn_helper.journal.context.where_am_i", return_value={"error": "no journal"})
        journal.start()                                                  # never read the real journal in tests
        self.addCleanup(journal.stop)

    def test_save_load_list_delete(self):
        self.assertEqual(rcfg.list_names(), [])
        rcfg.ensure_default()
        self.assertEqual(rcfg.list_names(), ["recce"])
        rcfg.save({"name": "close", "radius_ly": 25})
        self.assertEqual(rcfg.list_names(), ["close", "recce"])
        self.assertEqual(rcfg.load("close")["radius_ly"], 25)
        rcfg.delete("close")
        with self.assertRaises(KeyError):
            rcfg.load("close")
        with self.assertRaises(KeyError):
            rcfg.delete("close")

    def test_update_merges_and_validates_and_keeps_name(self):
        rcfg.ensure_default()
        self.assertEqual(rcfg.update("recce", {"radius_ly": 60})["radius_ly"], 60)
        with self.assertRaises(rcfg.ConfigError):
            rcfg.update("recce", {"refresh_seconds": 1})
        self.assertEqual(rcfg.load("recce")["radius_ly"], 60)             # the bad update changed nothing
        with self.assertRaises(rcfg.ConfigError):
            rcfg.update("recce", {"name": "other"})

    def test_bad_names_and_broken_files(self):
        with self.assertRaises(rcfg.ConfigError):
            rcfg.load("../evil")
        self.assertFalse(rcfg.exists("../evil"))
        rcfg.dashboards_dir().mkdir(parents=True)
        rcfg.path_for("broken").write_text("{nope", encoding="utf-8")
        with self.assertRaises(rcfg.ConfigError) as cm:
            rcfg.load("broken")
        self.assertIn("not valid JSON", str(cm.exception))


class AnchorTests(StoreTests):
    def test_new_dashboard_is_anchored_on_the_current_system(self):
        cfg, note = rcfg.save_new({"name": "a"}, where=lambda: {"system": "Sol"})
        self.assertEqual((cfg["origin"], note), ("Sol", None))
        self.assertEqual(rcfg.load("a")["origin"], "Sol")
        cfg, _ = rcfg.save_new(dict(rcfg.DEFAULT_CONFIG, name="b"), where=lambda: {"system": "Achenar"})
        self.assertEqual(cfg["origin"], "Achenar")

    def test_unreadable_journal_keeps_following_with_a_note(self):
        cfg, note = rcfg.save_new({"name": "a"}, where=lambda: {"error": "no journal files"})
        self.assertEqual(cfg["origin"], "current")
        self.assertIn("no journal files", note)
        cfg, note = rcfg.save_new({"name": "b"}, where=lambda: 1 / 0)
        self.assertEqual(cfg["origin"], "current")
        self.assertIn("ZeroDivisionError", note)

    def test_explicit_origin_is_never_changed_and_update_does_not_pin(self):
        cfg, note = rcfg.save_new({"name": "a", "origin": "Hub"}, where=lambda: {"system": "Sol"})
        self.assertEqual((cfg["origin"], note), ("Hub", None))
        self.assertEqual(rcfg.update("a", {"origin": "current"})["origin"], "current")    # explicit follow mode

    def test_default_dashboard_is_anchored_when_created(self):
        with mock.patch("eddn_helper.journal.context.where_am_i", return_value={"system": "Colonia"}):
            rcfg.ensure_default()
        self.assertEqual(rcfg.load("recce")["origin"], "Colonia")


class SettingsTests(StoreTests):
    def test_ingest_url_from_settings_file_and_env_wins(self):
        env = {"ELITE_HELPER_INGEST_DATABASE_URL": ""}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(rcfg.ingest_url())
            rcfg.settings_path().write_text('{"ingest_database_url": "  sqlite:///w.db "}', encoding="utf-8")
            self.assertEqual(rcfg.ingest_url(), "sqlite:///w.db")
            rcfg.settings_path().write_text("{broken", encoding="utf-8")
            self.assertIsNone(rcfg.ingest_url())                       # unreadable file is ignored
            rcfg.settings_path().write_text('{"ingest_database_url": "sqlite:///w.db"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"ELITE_HELPER_INGEST_DATABASE_URL": "sqlite:///env.db"}):
            self.assertEqual(rcfg.ingest_url(), "sqlite:///env.db")

    def test_reader_url_from_settings_file_and_env_wins(self):
        with mock.patch.dict(os.environ, {"ELITE_HELPER_DATABASE_URL": ""}):
            self.assertIsNone(rcfg.database_url())
            rcfg.settings_path().write_text('{"database_url": "sqlite:///r.db"}', encoding="utf-8")
            self.assertEqual(rcfg.database_url(), "sqlite:///r.db")
        with mock.patch.dict(os.environ, {"ELITE_HELPER_DATABASE_URL": "sqlite:///env.db"}):
            self.assertIsNone(rcfg.database_url())                       # the variable is used as before

    def test_default_core_writer_uses_the_settings_url(self):
        from eddn_helper import live
        from eddn_helper.recce.core import Core
        rcfg.settings_path().write_text('{"ingest_database_url": "sqlite://"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"ELITE_HELPER_INGEST_DATABASE_URL": ""}),                 mock.patch.object(live, "writer_session", return_value="SESSION") as ws:
            self.assertEqual(Core(lambda: None).writer(), "SESSION")
        ws.assert_called_once_with("sqlite://")


class CliTests(StoreTests):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_new_edit_show_delete(self):
        code, out, _ = self.run_cli("new", "nearby", "--origin", "Sol", "--radius", "30", "--economy", "Industrial",
                                    "--min-pop", "2000000", "--state-in", "pending", "--refresh", "30")
        self.assertEqual(code, 0, out)
        cfg = rcfg.load("nearby")
        self.assertEqual((cfg["origin"], cfg["radius_ly"], cfg["filters"]["economy"], cfg["filters"]["min_population"],
                          cfg["filters"]["state_in"], cfg["refresh_seconds"]),
                         ("Sol", 30, "Industrial", 2000000, "pending", 30))
        self.assertEqual(self.run_cli("new", "nearby")[0], 1)                 # refuses to overwrite
        code, _, _ = self.run_cli("edit", "nearby", "--set", "filters.min_population=100", "--set", "radius_ly=45")
        self.assertEqual(code, 0)
        cfg = rcfg.load("nearby")
        self.assertEqual((cfg["filters"]["min_population"], cfg["radius_ly"]), (100, 45))
        code, out, _ = self.run_cli("show", "nearby")
        self.assertEqual(json.loads(out)["name"], "nearby")
        self.assertEqual(self.run_cli("delete", "nearby")[0], 0)
        self.assertEqual(self.run_cli("show", "nearby")[0], 1)

    def test_invalid_edit_prints_each_problem_and_saves_nothing(self):
        rcfg.ensure_default()
        code, _, err = self.run_cli("edit", "recce", "--set", "refresh_seconds=2", "--set", "radius_ly=0")
        self.assertEqual(code, 1)
        self.assertIn("refresh_seconds: must be at least 5", err)
        self.assertIn("radius_ly: must be at least 1", err)
        self.assertEqual(rcfg.load("recce")["refresh_seconds"], 20)


if __name__ == "__main__":
    unittest.main()
