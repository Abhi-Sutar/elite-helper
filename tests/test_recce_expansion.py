"""Expansion chance: each rule on its own, how results combine, and that the rules are editable data."""

import copy
import unittest
from datetime import datetime, timezone
from unittest import mock

from eddn_helper.recce import config as rcfg
from eddn_helper.recce import core, expansion
from eddn_helper.recce.expansion_rules import RULESET
from recce_helpers import RecceDb, pending
from test_traffic import fac

FULL = tuple(f"Filler{i}" for i in range(6))        # with the controlling faction: 7 factions, a full system


class ExpansionCase(RecceDb):
    def setUp(self):
        super().setUp()
        self.players({"Alpha": True})

    def origin(self, infl=0.8, faction=None, **kw):
        self.put(1, "Origin", [(3, faction or pending("Alpha", infl))], pos=(0, 0, 0), **kw)

    def neighbour(self, id64, name, dist, others=("Filler",), population=1_000_000):
        self.put(id64, name, [(3, fac(f"Boss{id64}", 0.5))], pos=(dist, 0, 0), others=others, population=population)

    def evaluate(self, ruleset=RULESET, checks=expansion.CHECKS, **row):
        row = dict({"system": "Origin", "faction": "Alpha", "influence_pct": 80.0, "run": None,
                    "pending_text": "unknown"}, **row)
        s = self.factory()
        try:
            return expansion.evaluate(expansion.Context(s, ruleset["params"]), row, ruleset, checks)
        finally:
            s.close()

    @staticmethod
    def rule(out, rule_id):
        return next(r for r in out["rules"] if r["id"] == rule_id)

    def result(self, out, rule_id):
        return self.rule(out, rule_id)["result"]


class RuleTests(ExpansionCase):
    def test_everything_passing_is_likely(self):
        self.origin()
        self.neighbour(2, "Near", 10)
        out = self.evaluate()
        self.assertEqual(out["label"], "Likely")
        self.assertEqual(out["probability"], RULESET["params"]["base_probability"])
        self.assertEqual({r["result"] for r in out["rules"] if r["kind"] != "info"}, {"pass"})

    def test_influence_at_the_threshold_fails_the_gate(self):
        self.origin(0.75)
        self.neighbour(2, "Near", 10)
        out = self.evaluate(influence_pct=75.0)
        self.assertEqual(self.result(out, "influence_above_threshold"), "fail")
        self.assertEqual(out["label"], "Unlikely")
        self.assertLessEqual(out["probability"], 0.05)

    def test_unknown_influence_is_unknown_not_a_pass(self):
        self.origin()
        self.neighbour(2, "Near", 10)
        out = self.evaluate(influence_pct=None)
        self.assertEqual(self.result(out, "influence_above_threshold"), "unknown")
        self.assertEqual(out["label"], "Uncertain")

    def test_margin_just_above_the_threshold_lowers_the_chance(self):
        self.origin(0.76)
        self.neighbour(2, "Near", 10)
        out = self.evaluate(influence_pct=76.0)
        self.assertEqual(self.result(out, "influence_margin"), "fail")
        self.assertAlmostEqual(out["probability"], 0.85 * 0.75, places=2)

    def test_war_blocks(self):
        self.origin(faction=fac("Alpha", 0.8, state="War", pending=[("Expansion", 0)]))
        self.neighbour(2, "Near", 10)
        out = self.evaluate()
        self.assertEqual(self.result(out, "not_blocked_by_conflict"), "fail")
        self.assertLessEqual(out["probability"], 0.15)

    def test_an_election_the_faction_is_in_blocks(self):
        self.origin(conflicts=[{"WarType": "election", "Status": "active", "Faction1": {"Name": "Alpha"},
                                "Faction2": {"Name": "X"}}])
        self.neighbour(2, "Near", 10)
        self.assertEqual(self.result(self.evaluate(), "not_blocked_by_conflict"), "fail")

    def test_a_war_between_other_factions_does_not_block(self):
        self.origin(conflicts=[{"WarType": "war", "Status": "active", "Faction1": {"Name": "P"},
                                "Faction2": {"Name": "Q"}}])
        self.neighbour(2, "Near", 10)
        self.assertEqual(self.result(self.evaluate(), "not_blocked_by_conflict"), "pass")

    def test_full_neighbours_mean_investment(self):
        self.origin()
        self.neighbour(2, "Full", 10, others=FULL)
        out = self.evaluate()
        self.assertEqual(self.result(out, "target_in_range"), "fail")
        self.assertEqual(out["label"], "Unlikely")
        self.assertIn("already have 7 factions", self.rule(out, "target_in_range")["detail"])

    def test_no_neighbours_stored_fails(self):
        self.origin()
        self.assertEqual(self.result(self.evaluate(), "target_in_range"), "fail")

    def test_room_beyond_the_first_radius_only_shows_in_the_ladder(self):
        self.origin()
        self.neighbour(2, "Far", 25)
        out = self.evaluate()
        self.assertEqual(self.result(out, "target_in_range"), "fail")
        detail = self.rule(out, "fallback_ladder")["detail"]
        self.assertIn("20 ly: 0 with room", detail)
        self.assertIn("30 ly: 1 with room", detail)
        self.assertIn("Investment", detail)

    def test_unpopulated_systems_are_not_targets(self):
        self.origin()
        self.neighbour(2, "Empty", 10, population=0)
        self.assertEqual(self.result(self.evaluate(), "target_in_range"), "fail")

    def test_happier_origin_elsewhere_loses_the_tiebreak(self):
        self.origin(0.8)
        self.neighbour(2, "Near", 10)
        self.put(3, "Happy Place", [(3, fac("Alpha", 0.9, band=1, pending=[("Expansion", 0)]))], pos=(0, 5, 0))
        out = self.evaluate()
        self.assertEqual(self.result(out, "origin_by_happiness"), "fail")
        self.assertAlmostEqual(out["probability"], 0.85 * 0.15, places=2)

    def test_happiest_system_keeps_the_origin(self):
        self.origin(faction=fac("Alpha", 0.8, band=1, pending=[("Expansion", 0)]))
        self.neighbour(2, "Near", 10)
        self.put(3, "Sad Place", [(3, fac("Alpha", 0.9, band=4, pending=[("Expansion", 0)]))], pos=(0, 5, 0))
        self.assertEqual(self.result(self.evaluate(), "origin_by_happiness"), "pass")

    def test_equal_happiness_is_unknown(self):
        self.origin(0.8)
        self.neighbour(2, "Near", 10)
        self.put(3, "Twin", [(3, fac("Alpha", 0.9, band=2, pending=[("Expansion", 0)]))], pos=(0, 5, 0))
        self.assertEqual(self.result(self.evaluate(), "origin_by_happiness"), "unknown")

    def test_timing_is_information_only(self):
        self.origin()
        self.neighbour(2, "Near", 10)
        out = self.evaluate(run={"first_seen": "x", "last_absent": None}, pending_text="at least 30 h")
        timing = self.rule(out, "pending_timing")
        self.assertEqual(timing["result"], "info")
        self.assertIn("at least 30 h", timing["detail"])


class CombinationTests(ExpansionCase):
    def test_probability_is_always_clamped(self):
        self.origin(0.5)
        out = self.evaluate(influence_pct=50.0)
        self.assertGreaterEqual(out["probability"], RULESET["params"]["min_probability"])
        self.assertLessEqual(out["probability"], RULESET["params"]["max_probability"])

    def test_editing_the_ruleset_changes_the_answer_without_code_changes(self):
        self.origin(0.8)
        self.neighbour(2, "Near", 10, others=("A", "B", "C"))          # 4 factions
        self.assertEqual(self.evaluate()["label"], "Likely")
        tight = copy.deepcopy(RULESET)
        tight["params"]["max_factions"] = 4                              # now a 4-faction system is full
        self.assertEqual(self.result(self.evaluate(tight), "target_in_range"), "fail")
        strict = copy.deepcopy(RULESET)
        strict["params"]["influence_threshold_pct"] = 85.0
        self.assertEqual(self.result(self.evaluate(strict), "influence_above_threshold"), "fail")
        narrow = copy.deepcopy(RULESET)
        narrow["params"]["radius_ladder_ly"] = [5, 30]
        self.assertEqual(self.result(self.evaluate(narrow), "target_in_range"), "fail")   # Near is 10 ly away

    def test_a_new_rule_is_one_function_plus_one_entry(self):
        self.origin()
        self.neighbour(2, "Near", 10)
        extra = copy.deepcopy(RULESET)
        extra["rules"].append({"id": "always_bad", "title": "t", "check": "always_bad", "kind": "modifier",
                               "fail_factor": 0.5, "unknown_factor": 1, "confidence": "uncertain", "source": "s",
                               "explain": "e"})
        checks = dict(expansion.CHECKS, always_bad=lambda ctx, row, p, rule: {"result": "fail", "detail": "no"})
        self.assertEqual(expansion.validate_ruleset(extra, checks), [])
        self.assertAlmostEqual(self.evaluate(extra, checks)["probability"], 0.85 * 0.5, delta=0.01)


class RulesetValidationTests(unittest.TestCase):
    def test_the_shipped_ruleset_is_valid(self):
        self.assertEqual(expansion.validate_ruleset(RULESET), [])

    def test_problems_are_listed(self):
        bad = copy.deepcopy(RULESET)
        bad["rules"][0]["check"] = "nope"
        bad["rules"][1]["id"] = bad["rules"][0]["id"]
        bad["rules"][2]["kind"] = "weird"
        del bad["rules"][3]["fail_factor"]
        errors = "\n".join(expansion.validate_ruleset(bad))
        for needle in ("unknown check 'nope'", "duplicate id", "kind must be", "missing fail_factor"):
            self.assertIn(needle, errors)


class SnapshotTests(ExpansionCase):
    def setUp(self):
        super().setUp()
        self.core = core.Core(lambda: self.factory, writer=lambda: None, where=lambda: {"error": "no journal"})
        self.cfg = rcfg.validate(dict(rcfg.DEFAULT_CONFIG, origin="Hub"))
        self.put(9999, "Hub", [(3, fac("HubFolk", 0.6))], economy="Industrial")
        self.put(1, "Pend", [(3, pending("Alpha", 0.8))], pos=(5, 0, 0))
        self.put(2, "Act", [(3, fac("Beta", 0.8, state="Expansion", active=["Expansion"]))], pos=(8, 0, 0))
        self.players({"Alpha": True, "Beta": True})

    def rows(self, cfg=None):
        data = self.core.snapshot(cfg or self.cfg, now=datetime.now(timezone.utc))
        return {r["system"]: r for r in data["rows"]}

    def test_pending_rows_get_an_estimate_and_active_rows_do_not(self):
        rows = self.rows()
        self.assertIn(rows["Pend"]["expansion"]["label"], ("Likely", "Possible", "Unlikely", "Uncertain"))
        self.assertTrue(rows["Pend"]["expansion"]["rules"])
        self.assertIsNone(rows["Act"]["expansion"])

    def test_estimate_can_be_turned_off(self):
        cfg = rcfg.validate(dict(self.cfg, expansion_estimate=False))
        self.assertIsNone(self.rows(cfg)["Pend"]["expansion"])

    def test_an_evaluator_failure_does_not_break_the_snapshot(self):
        with mock.patch.object(expansion, "evaluate", side_effect=RuntimeError("boom")):
            rows = self.rows()
        self.assertIn("boom", rows["Pend"]["expansion"]["error"])
        self.assertIn("Act", rows)

    def test_column_label_and_default_column(self):
        self.assertEqual(core.column_labels(self.cfg)["expansion_chance"], "Expansion chance")
        self.assertIn("expansion_chance", self.cfg["columns"])
        self.assertTrue(self.cfg["expansion_estimate"])


if __name__ == "__main__":
    unittest.main()
