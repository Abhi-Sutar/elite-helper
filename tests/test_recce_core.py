"""Recce core: the query selection, the Expansion run logic, row flags, the history cache and the EDSM fetch cap."""

import unittest
from datetime import timedelta
from unittest import mock

from eddn_helper import external, queries
from eddn_helper.recce import config as rcfg
from eddn_helper.recce import core
from recce_helpers import NOW, RecceDb, expanding, pending, plain
from test_traffic import at, fac


def point(ts, present=True, state="None", active=(), pend=(), recov=()):
    return {"faction": "F", "ts": ts, "present": present, "state": state, "active_states": list(active),
            "pending_states": [{"state": s, "trend": 0} for s in pend],
            "recovering_states": [{"state": s, "trend": 0} for s in recov]}


class StateRunTests(unittest.TestCase):
    def test_history_starting_inside_the_run_is_a_lower_bound(self):
        run = core.state_run([point(at(30), pend=["Expansion"]), point(at(10), pend=["Expansion"])], "pending", "expansion")
        self.assertEqual(core.iso(run["first_seen"]), at(30))
        self.assertIsNone(run["last_absent"])
        self.assertEqual(core.run_text(run, "pending", NOW), "at least 30 h")

    def test_previous_reading_without_the_state_gives_a_range(self):
        pts = [point(at(50)), point(at(30), pend=["Expansion"]), point(at(10), pend=["Expansion"])]
        run = core.state_run(pts, "pending", "expansion")
        self.assertEqual(core.iso(run["first_seen"]), at(30))
        self.assertEqual(core.iso(run["last_absent"]), at(50))
        self.assertEqual(core.run_text(run, "pending", NOW), "30 h to 50 h")

    def test_input_order_does_not_matter(self):
        pts = [point(at(10), pend=["Expansion"]), point(at(50)), point(at(30), pend=["Expansion"])]
        self.assertEqual(core.run_text(core.state_run(pts, "pending", "expansion"), "pending", NOW), "30 h to 50 h")

    def test_only_the_current_unbroken_run_counts(self):
        pts = [point(at(90), pend=["Expansion"]), point(at(70)), point(at(20), pend=["Expansion"]),
               point(at(5), pend=["Expansion"])]
        run = core.state_run(pts, "pending", "expansion")
        self.assertEqual(core.iso(run["first_seen"]), at(20))
        self.assertEqual(core.iso(run["last_absent"]), at(70))

    def test_faction_leaving_breaks_the_run(self):
        pts = [point(at(40), pend=["Expansion"]), point(at(30), present=False), point(at(10), pend=["Expansion"])]
        run = core.state_run(pts, "pending", "expansion")
        self.assertEqual(core.iso(run["first_seen"]), at(10))
        self.assertEqual(core.iso(run["last_absent"]), at(30))

    def test_no_run_when_newest_reading_lacks_the_state_or_no_history(self):
        self.assertIsNone(core.state_run([point(at(30), pend=["Expansion"]), point(at(5))], "pending", "expansion"))
        self.assertIsNone(core.state_run([], "pending", "expansion"))
        self.assertEqual(core.run_text(None, "pending", NOW), "unknown")

    def test_active_uses_active_list_or_main_state_and_says_so(self):
        run = core.state_run([point(at(5), state="Expansion"), point(at(20), state="Expansion", active=["Expansion"])],
                             "active", "expansion")
        self.assertEqual(core.run_text(run, "active", NOW), "active for at least 20 h")

    def test_hours_format(self):
        self.assertEqual(core.fmt_hours(25 * 60), "25 min")
        self.assertEqual(core.fmt_hours(3.44 * 3600), "3.4 h")
        self.assertEqual(core.fmt_hours(31.2 * 3600), "31 h")
        self.assertEqual(core.fmt_hours(5), "1 min")


class CoreCase(RecceDb):
    def setUp(self):
        super().setUp()
        self.core = core.Core(lambda: self.factory, writer=lambda: None, where=lambda: {"error": "no journal"})
        self.cfg = rcfg.validate(dict(rcfg.DEFAULT_CONFIG, origin="Hub"))
        self.put(9999, "Hub", [(3, plain("HubFolk"))], economy="Industrial")        # the origin system

    def snap(self, cfg=None):
        return self.core.snapshot(cfg or self.cfg, now=NOW)

    def by_system(self, data):
        return {r["system"]: r for r in data["rows"]}


class SelectionTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.put(2, "Pending Agri", [(3, pending("PlayerP"))], pos=(10, 0, 0))
        self.put(3, "Active Agri", [(3, expanding("PlayerA"))], pos=(20, 0, 0))
        self.put(4, "Npc Agri", [(3, pending("NpcN"))], pos=(30, 0, 0))
        self.put(5, "Far Agri", [(3, pending("PlayerF"))], pos=(500, 0, 0))
        self.put(6, "Industrial", [(3, pending("PlayerI"))], pos=(40, 0, 0), economy="Industrial")
        self.put(7, "Tiny Agri", [(3, pending("PlayerT"))], pos=(50, 0, 0), population=500_000)
        self.put(8, "Recovering Agri", [(3, fac("PlayerR", 0.6, recovering=[("Expansion", 0)]))], pos=(60, 0, 0))
        self.put(9, "Unknown Agri", [(3, pending("Mystery"))], pos=(70, 0, 0))
        self.players({"PlayerP": True, "PlayerA": True, "NpcN": False, "PlayerF": True, "PlayerI": True,
                      "PlayerT": True, "PlayerR": True})

    def test_default_dashboard_rows(self):
        data = self.snap()
        self.assertEqual(sorted(self.by_system(data)), ["Active Agri", "Pending Agri"])
        self.assertEqual(self.by_system(data)["Active Agri"]["kind"], "active")
        self.assertEqual(self.by_system(data)["Pending Agri"]["kind"], "pending")
        self.assertEqual(data["origin"], {"name": "Hub", "source": "config"})

    def test_unknown_player_status_is_hidden_and_counted(self):
        data = self.snap()
        self.assertNotIn("Unknown Agri", self.by_system(data))
        self.assertEqual(data["hidden_unknown_player"], 1)

    def test_sorted_active_first_then_longest_running(self):
        self.assertEqual([r["system"] for r in self.snap()["rows"]], ["Active Agri", "Pending Agri"])
        flipped = rcfg.validate(dict(self.cfg, sort={"by": "state", "order": "desc"}))
        self.assertEqual([r["system"] for r in self.snap(flipped)["rows"]], ["Pending Agri", "Active Agri"])
        far_first = rcfg.validate(dict(self.cfg, sort={"by": "distance", "order": "desc"}))
        self.assertEqual([r["system"] for r in self.snap(far_first)["rows"]], ["Active Agri", "Pending Agri"])

    def test_radius_filters_by_distance(self):
        near = rcfg.validate(dict(self.cfg, radius_ly=15))
        self.assertEqual([r["system"] for r in self.snap(near)["rows"]], ["Pending Agri"])

    def test_filters_come_from_the_config(self):
        recov = rcfg.validate(dict(self.cfg, filters=dict(self.cfg["filters"], state_in="recovering")))
        self.assertEqual([r["system"] for r in self.snap(recov)["rows"]], ["Recovering Agri"])
        indus = rcfg.validate(dict(self.cfg, filters=dict(self.cfg["filters"], economy="Industrial")))
        self.assertEqual([r["system"] for r in self.snap(indus)["rows"]], ["Industrial"])
        tiny = rcfg.validate(dict(self.cfg, filters=dict(self.cfg["filters"], min_population=100_000)))
        self.assertIn("Tiny Agri", self.by_system(self.snap(tiny)))

    def test_origin_current_reads_the_journal_and_remembers_the_last_position(self):
        pos = {"system": "Hub"}
        c = core.Core(lambda: self.factory, writer=lambda: None, where=lambda: pos)
        cfg = rcfg.validate(dict(rcfg.DEFAULT_CONFIG))
        self.assertEqual(c.snapshot(cfg, now=NOW)["origin"], {"name": "Hub", "source": "journal"})
        pos.clear()
        pos["error"] = "journal gone"
        self.assertEqual(c.snapshot(cfg, now=NOW)["origin"], {"name": "Hub", "source": "journal (last known)"})

    def test_origin_current_without_a_journal_is_a_clear_error(self):
        cfg = rcfg.validate(dict(rcfg.DEFAULT_CONFIG))
        with self.assertRaises(core.CoreError) as cm:
            self.snap(cfg)
        self.assertIn("cannot work out your current position", str(cm.exception))

    def test_unknown_origin_system_is_reported(self):
        with self.assertRaises(core.CoreError):
            self.snap(rcfg.validate(dict(self.cfg, origin="Nowhere At All")))

    def test_stamps_report_the_database_timestamps(self):
        stamps = self.core.stamps()
        self.assertEqual(stamps["newest_faction_update"], at(3))
        self.assertIn("newest_market_update", stamps)


class RecentreTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.put(1, "Near Hub", [(3, pending("P1"))], pos=(10, 0, 0))
        self.put(2, "Far Away", [(3, pending("P2"))], pos=(5000, 0, 0))
        self.put(3, "Far Hub", [(3, plain("FH"))], pos=(5000, 5, 0), economy="Industrial")
        self.players({"P1": True, "P2": True})
        self.pos = {"system": "Far Hub"}
        self.c = core.Core(lambda: self.factory, writer=lambda: None, where=lambda: self.pos)
        self.name = self.cfg["name"]
        rcfg.save(dict(self.cfg, origin="Hub"))

    def systems(self):
        return [r["system"] for r in self.c.snapshot(rcfg.load(self.name), now=NOW)["rows"]]

    def test_recentre_writes_the_origin_and_the_snapshot_follows(self):
        self.assertEqual(self.systems(), ["Near Hub"])
        res = self.c.recentre(self.name)
        self.assertEqual(res, {"name": self.name, "origin": "Far Hub", "previous": "Hub"})
        self.assertEqual(self.systems(), ["Far Away"])

    def test_unknown_position_changes_nothing(self):
        self.pos.clear()
        self.pos["error"] = "journal gone"
        with self.assertRaises(core.CoreError) as cm:
            self.c.recentre(self.name)
        self.assertIn("journal gone", str(cm.exception))
        self.assertEqual(rcfg.load(self.name)["origin"], "Hub")

    def test_already_there_is_a_no_op(self):
        self.pos["system"] = "Hub"
        stamp = rcfg.stamp(self.name)
        self.assertEqual(self.c.recentre(self.name)["previous"], "Hub")
        self.assertEqual(rcfg.stamp(self.name), stamp)                   # file untouched, so no pointless reload

    def test_anchored_snapshot_ignores_where_you_are(self):
        self.pos["system"] = "Far Hub"
        self.assertEqual(self.systems(), ["Near Hub"])                   # still centred on Hub


class RowFlagTests(CoreCase):
    def test_pending_run_text_in_rows(self):
        self.put(1, "Range", [(50, plain("P1")), (30, pending("P1")), (10, pending("P1", 0.61))])
        self.put(2, "Lower", [(20, pending("P2"))], pos=(5, 0, 0))
        self.put(3, "Old", [(600, pending("P3"))], pos=(8, 0, 0))              # history older than the 14 day window
        self.players({"P1": True, "P2": True, "P3": True})
        rows = self.by_system(self.snap())
        self.assertEqual(rows["Range"]["pending_text"], "30 h to 50 h")
        self.assertEqual(rows["Lower"]["pending_text"], "at least 20 h")
        self.assertEqual(rows["Old"]["pending_text"], "unknown")

    def test_war_chip_and_stale_flag_and_last_update(self):
        war = [{"WarType": "war", "Status": "active", "Faction1": {"Name": "P1"}, "Faction2": {"Name": "X"}}]
        self.put(1, "Warring", [(2, pending("P1"))], conflicts=war)
        self.put(2, "Quiet", [(30, pending("P2"))], pos=(5, 0, 0))
        self.players({"P1": True, "P2": True})
        rows = self.by_system(self.snap())
        self.assertTrue(rows["Warring"]["war"])
        self.assertFalse(rows["Quiet"]["war"])
        self.assertFalse(rows["Warring"]["stale"])
        self.assertTrue(rows["Quiet"]["stale"])                                   # data 30 h old is over 24 h
        age = NOW - core.parse_ts(rows["Warring"]["last_update_at"])
        self.assertAlmostEqual(age.total_seconds() / 3600, 2, delta=0.2)

    def test_highlight_rule_active_under_48_hours(self):
        self.put(1, "Fresh", [(60, plain("P1")), (20, expanding("P1"))])
        self.put(2, "Aged", [(100, plain("P2")), (80, expanding("P2"))], pos=(5, 0, 0))
        self.put(3, "Waiting", [(10, pending("P3"))], pos=(8, 0, 0))
        self.players({"P1": True, "P2": True, "P3": True})
        rows = self.by_system(self.snap())
        self.assertEqual((rows["Fresh"]["highlight"], rows["Aged"]["highlight"], rows["Waiting"]["highlight"]),
                         (True, False, False))
        none = rcfg.validate(dict(self.cfg, highlight_rules=[]))
        self.assertFalse(any(r["highlight"] for r in self.snap(none)["rows"]))
        over = rcfg.validate(dict(self.cfg, highlight_rules=[{"when": "pending_for_over_hours", "hours": 5}]))
        self.assertEqual({r["system"] for r in self.snap(over)["rows"] if r["highlight"]}, {"Waiting"})

    def test_highlight_helper_handles_time_passing(self):
        row = {"kind": "active", "run": {"first_seen": core.iso(NOW - timedelta(hours=47)), "last_absent": None}}
        rules = [{"when": "active_for_under_hours", "hours": 48}]
        self.assertTrue(core.is_highlighted(row, rules, NOW))
        self.assertFalse(core.is_highlighted(row, rules, NOW + timedelta(hours=2)))


class HistoryCacheTests(CoreCase):
    def test_history_is_cached_until_the_faction_row_changes(self):
        self.put(1, "Cached", [(30, pending("P1")), (20, pending("P1"))])
        self.players({"P1": True})
        with mock.patch.object(queries, "faction_history", wraps=queries.faction_history) as spy:
            self.snap()
            self.snap()
            self.assertEqual(spy.call_count, 1)                                    # second snapshot used the cache
            self.put(1, "Cached", [(1, pending("P1", 0.7))])                          # the faction row changes (influence)
            self.snap()
            self.assertEqual(spy.call_count, 2)


class LiveFetchTests(CoreCase):
    def setUp(self):
        super().setUp()
        for i in range(12):
            self.put(100 + i, f"Sys {i}", [(3, pending(f"U{i}"))], pos=(i + 1, 0, 0))
        self.fetches = []

        def edsm(path, **p):
            self.fetches.append(p["systemName"])
            i = int(p["systemName"].split()[1])
            return {"factions": [{"name": f"U{i}", "isPlayer": True}]}
        patcher = mock.patch.object(external, "_get", side_effect=edsm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.live_core = core.Core(lambda: self.factory, writer=lambda: self.factory(), where=lambda: {})

    def test_at_most_ten_requests_per_refresh_and_results_are_stored(self):
        data = self.live_core.snapshot(self.cfg, now=NOW)
        self.assertEqual(len(self.fetches), 10)
        self.assertEqual(len(set(self.fetches)), 10)                                # one request per faction
        self.assertEqual(data["live_fetch"]["requests"], 10)
        self.assertEqual(data["count"], 10)
        self.assertEqual(data["hidden_unknown_player"], 2)                          # the other two stay hidden
        again = self.live_core.snapshot(self.cfg, now=NOW)                          # cooldown: no more requests now
        self.assertEqual(len(self.fetches), 10)
        self.assertIn("waiting", again["live_fetch"]["note"])
        self.assertEqual(again["count"], 10)                                        # stored flags are reused

    def test_configured_budget_is_respected(self):
        cfg = rcfg.validate(dict(self.cfg, live_fetch={"enabled": True, "max_requests": 3}))
        self.live_core.snapshot(cfg, now=NOW)
        self.assertEqual(len(self.fetches), 3)

    def test_disabled_or_no_writer_means_no_requests_and_read_only_use(self):
        off = rcfg.validate(dict(self.cfg, live_fetch={"enabled": False}))
        data = self.live_core.snapshot(off, now=NOW)
        self.assertEqual((self.fetches, data["count"], data["live_fetch"]["note"]), ([], 0, "live fetch is off"))
        data = self.core.snapshot(self.cfg, now=NOW)                                # self.core has no writer
        self.assertEqual(self.fetches, [])
        self.assertIn("no writer connection", data["live_fetch"]["note"])

    def test_a_failing_fetch_does_not_break_the_snapshot(self):
        external._get.side_effect = external.LiveLookupError("EDSM down")
        data = self.live_core.snapshot(self.cfg, now=NOW)
        self.assertEqual(data["count"], 0)
        self.assertIn("EDSM", data["live_fetch"]["note"])


if __name__ == "__main__":
    unittest.main()
