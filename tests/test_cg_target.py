"""CG board target: the MCP-side config rewrite and the board's baseline reset on a target change."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from eddn_helper import cg_target, parsers, queries, store
from helpers import new_session
from test_economy import system
from test_traffic import at

CONFIG = {"title": "t", "refresh_seconds": 30, "target": {"name": "Zero Squared", "system": "HIP 74116"},
          "ship": {"laden_ly": 31.34}, "tabs": [{"key": "super", "commodity": "superconductors"}]}


def board_module():
    spec = importlib.util.spec_from_file_location(
        "cg_board_mod", Path(__file__).resolve().parent.parent / "cg_board" / "cg_board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SetTargetTests(unittest.TestCase):
    def setUp(self):
        self.session = new_session()
        for i, name in enumerate(["Alpha Sys", "Beta Sys"], 1):
            store.apply_parsed(self.session, parsers.parse_message(system(name, 1000 + i, i * 10, "Industrial")))
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps(CONFIG), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        self.session.close()

    def cfg(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_valid_system_rewrites_only_target(self):
        res = cg_target.set_target(self.session, "alpha sys", station="Cabot Vision", path=self.path)
        self.assertEqual((res["system"], res["station"], res["changed"]), ("Alpha Sys", "Cabot Vision", True))
        self.assertEqual(res["previous"], {"system": "HIP 74116", "station": "Zero Squared"})
        cfg = self.cfg()
        self.assertEqual(cfg["target"], {"name": "Cabot Vision", "system": "Alpha Sys"})
        self.assertEqual({k: v for k, v in cfg.items() if k != "target"},
                         {k: v for k, v in CONFIG.items() if k != "target"})

    def test_station_name_defaults_to_system_and_is_kept_when_unchanged(self):
        cg_target.set_target(self.session, "Alpha Sys", station="Cabot Vision", path=self.path)
        res = cg_target.set_target(self.session, "Alpha Sys", path=self.path)
        self.assertEqual((res["station"], res["changed"]), ("Cabot Vision", False))
        res = cg_target.set_target(self.session, "Beta Sys", path=self.path)
        self.assertEqual(res["station"], "Beta Sys")

    def test_current_uses_journal_position(self):
        res = cg_target.set_target(self.session, "current", path=self.path, where=lambda: {"system": "Beta Sys"})
        self.assertEqual(res["system"], "Beta Sys")
        self.assertEqual(self.cfg()["target"]["system"], "Beta Sys")

    def test_system_without_coordinates_is_rejected(self):
        row = queries.find_system(self.session, "Alpha Sys")
        row.x = None
        self.session.flush()
        self.assertIn("coordinates", cg_target.set_target(self.session, "Alpha Sys", path=self.path)["error"])
        self.assertEqual(self.cfg(), CONFIG)

    def test_unknown_system_changes_nothing(self):
        res = cg_target.set_target(self.session, "Nowhere", path=self.path)
        self.assertIn("error", res)
        self.assertEqual(self.cfg(), CONFIG)

    def test_get_target(self):
        res = cg_target.get_target(self.path)
        self.assertEqual((res["system"], res["station"], res["tabs"]), ("HIP 74116", "Zero Squared", ["super"]))

    def test_current_without_position_errors(self):
        res = cg_target.set_target(self.session, "current", path=self.path, where=lambda: {"error": "no journal"})
        self.assertIn("no journal", res["error"])
        self.assertEqual(self.cfg(), CONFIG)

    def test_bad_config_file_reports_error(self):
        self.path.write_text("{nope", encoding="utf-8")
        self.assertIn("error", cg_target.set_target(self.session, "Alpha Sys", path=self.path))
        self.assertIn("error", cg_target.get_target(self.path))


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.board = board_module()

    def test_missing_marker_is_stamped_not_reset(self):
        b = self.board.apply_target_change({"super": {"base": {"A": 1}}}, "Sol")
        self.assertEqual(b["super"]["base"], {"A": 1})
        self.assertEqual(b["_target"], "Sol")

    def test_same_target_keeps_baselines_case_insensitively(self):
        b = self.board.apply_target_change({"_target": "Sol", "super": {"base": {"A": 1}}}, "SOL")
        self.assertIn("super", b)

    def test_moved_target_resets(self):
        b = self.board.apply_target_change({"_target": "Sol", "super": {"base": {"A": 1}}}, "Achenar")
        self.assertEqual(b, {"_target": "Achenar"})


if __name__ == "__main__":
    unittest.main()
