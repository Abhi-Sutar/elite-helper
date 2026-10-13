"""Moving a cg dashboard's target (the cg_board_* MCP tools): checked against the database, only the target changes."""
import json
import unittest

from eddn_helper import cg_target, queries
from eddn_helper.recce import config as rcfg
from recce_helpers import RecceDb, plain

CFG = {"kind": "cg", "name": "cg", "target": {"name": "Zero Squared", "system": "HIP 74116"},
       "ship": {"laden_ly": 31.34, "unladen_ly": 45.22, "cargo": 1147},
       "tabs": [{"key": "super", "commodity": "superconductors"}]}


class SetTargetTests(RecceDb):
    def setUp(self):
        super().setUp()
        self.put(1, "Alpha Sys", [(1, plain("A"))], pos=(10, 0, 0))
        self.put(2, "Beta Sys", [(1, plain("A"))], pos=(20, 0, 0))
        rcfg.save(json.loads(json.dumps(CFG)))
        self.session = self.factory()
        self.addCleanup(self.session.close)

    def target(self):
        return rcfg.load("cg")["target"]

    def test_valid_system_rewrites_only_target(self):
        before = rcfg.load("cg")
        res = cg_target.set_target(self.session, "alpha sys", station="Cabot Vision")
        self.assertEqual((res["system"], res["station"], res["changed"]), ("Alpha Sys", "Cabot Vision", True))
        self.assertEqual(res["previous"], {"system": "HIP 74116", "station": "Zero Squared"})
        after = rcfg.load("cg")
        self.assertEqual(after["target"], {"name": "Cabot Vision", "system": "Alpha Sys"})
        self.assertEqual({k: v for k, v in after.items() if k != "target"},
                         {k: v for k, v in before.items() if k != "target"})

    def test_station_name_defaults_to_system_and_is_kept_when_unchanged(self):
        cg_target.set_target(self.session, "Alpha Sys", station="Cabot Vision")
        res = cg_target.set_target(self.session, "Alpha Sys")
        self.assertEqual((res["station"], res["changed"]), ("Cabot Vision", False))
        res = cg_target.set_target(self.session, "Beta Sys")
        self.assertEqual(res["station"], "Beta Sys")

    def test_current_follows_the_journal(self):
        res = cg_target.set_target(self.session, "current", station="Cabot Vision",
                                   where=lambda: {"system": "Beta Sys"})
        self.assertEqual((res["system"], res["resolved_system"]), ("current", "Beta Sys"))
        self.assertEqual(self.target(), {"name": "Cabot Vision", "system": "current"})

    def test_current_without_a_position_changes_nothing(self):
        res = cg_target.set_target(self.session, "current", where=lambda: {"error": "no journal"})
        self.assertIn("no journal", res["error"])
        self.assertEqual(self.target()["system"], "HIP 74116")

    def test_unknown_system_changes_nothing(self):
        self.assertIn("error", cg_target.set_target(self.session, "Nowhere"))
        self.assertEqual(self.target()["system"], "HIP 74116")

    def test_system_without_coordinates_is_rejected(self):
        queries.find_system(self.session, "Alpha Sys").x = None
        self.session.flush()
        self.assertIn("coordinates", cg_target.set_target(self.session, "Alpha Sys")["error"])
        self.assertEqual(self.target()["system"], "HIP 74116")

    def test_only_cg_dashboards_and_existing_ones(self):
        rcfg.save(rcfg.DEFAULT_CONFIG)
        self.assertIn("not a cg dashboard", cg_target.set_target(self.session, "Alpha Sys", name="recce")["error"])
        self.assertIn("no dashboard named", cg_target.set_target(self.session, "Alpha Sys", name="other")["error"])
        self.assertIn("error", cg_target.get_target("recce"))

    def test_get_target(self):
        res = cg_target.get_target()
        self.assertEqual((res["system"], res["station"], res["tabs"]), ("HIP 74116", "Zero Squared", ["super"]))


if __name__ == "__main__":
    unittest.main()
