"""Player-faction flag (faction_info, from EDSM) and the find_systems filters built on it:
player_faction, and state_since_hours (when the current run of a faction state began, from faction_history)."""
import contextlib
import io
import json
import unittest
from unittest import mock

from sqlalchemy import select

from eddn_helper import cli, external, mcp_server, parsers, queries, store
from eddn_helper.models import FactionInfo
from helpers import new_session
from test_traffic import at, fac, sys_msg


def expanding(name, infl=0.6):
    return fac(name, infl, state="Expansion", active=["Expansion"])


class PlayerCase(unittest.TestCase):
    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.close)

    def close(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def feed(self, id64, name, observations, economy="Industrial", others=("Filler",), controller=None):
        """observations: [(hours_ago, controlling faction dict)], oldest first."""
        for hours_ago, f in observations:
            msg = sys_msg([f] + [fac(o, 0.2) for o in others], at(hours_ago), id64=id64, name=name)
            msg["message"]["SystemEconomy"] = f"$economy_{economy};"
            if controller:
                msg["message"]["SystemFaction"] = {"Name": controller}
            store.apply_parsed(self.s, parsers.parse_message(msg))
        self.s.commit()

    def flags(self, players):
        store.upsert_faction_info(self.s, players)
        self.s.commit()

    def names(self, **kw):
        res = queries.find_systems(self.s, **kw)
        self.assertNotIn("error", res)
        return [r["system"] for r in res["results"]]

    def find(self, **kw):
        res = queries.find_systems(self.s, **kw)
        self.assertNotIn("error", res)
        return res


class FactionInfoStoreTests(PlayerCase):
    def test_upsert_inserts_updates_and_matches_case_insensitively(self):
        self.assertEqual(store.upsert_faction_info(self.s, {"Winged Hussars": True, "Nobody": None}), 1)
        store.upsert_faction_info(self.s, {"WINGED HUSSARS": False})
        self.s.commit()
        rows = self.s.execute(select(FactionInfo)).scalars().all()
        self.assertEqual([(r.name, r.is_player) for r in rows], [("Winged Hussars", False)])  # spelling kept

    def test_listener_path_leaves_faction_info_alone(self):
        self.flags({"P1": True})
        self.feed(1, "Sol", [(2, fac("P1", 0.6))])
        self.assertEqual([r.name for r in self.s.execute(select(FactionInfo)).scalars()], ["P1"])

    def test_edsm_flags_are_read_from_the_factions_payload(self):
        payload = {"factions": [{"name": "A", "isPlayer": True}, {"name": "B", "isPlayer": False},
                                {"name": "C"}, {"name": "D", "isPlayer": None}]}
        self.assertEqual(external._player_flags(payload["factions"]), {"A": True, "B": False})
        self.assertEqual(external._player_flags(None), {})

    def test_refresh_stations_stores_flags_and_survives_a_failed_factions_call(self):
        self.feed(7, "Hub", [(2, fac("P1", 0.6))])

        def edsm(path, **p):
            if path.endswith("/factions"):
                return {"factions": [{"name": "P1", "isPlayer": True}, {"name": "Filler", "isPlayer": False}]}
            return {"id64": 7, "name": "Hub", "stations": []}
        with mock.patch.object(external, "_get", side_effect=edsm):
            self.assertEqual(external.refresh_stations(self.s, "Hub"), 0)
        self.assertEqual(queries._player_flags(self.s, ["p1", "FILLER", "x"]), {"p1": True, "filler": False})

        def broken(path, **p):
            if path.endswith("/factions"):
                raise external.LiveLookupError("down")
            return {"id64": 7, "name": "Hub", "stations": []}
        with mock.patch.object(external, "_get", side_effect=broken):
            self.assertEqual(external.refresh_stations(self.s, "Hub"), 0)


class PlayerFilterTests(PlayerCase):
    def setUp(self):
        super().setUp()
        self.feed(1, "Alpha", [(2, fac("PlayerOne", 0.6))])
        self.feed(2, "Beta", [(2, fac("NpcOne", 0.6))])
        self.feed(3, "Gamma", [(2, fac("Mystery", 0.6))])
        self.flags({"PlayerOne": True, "NpcOne": False})

    def test_player_faction_true_and_false_keep_unknown_and_flag_it(self):
        res = self.find(controlling_only=True, player_faction=True, sort="name")
        self.assertEqual([r["system"] for r in res["results"]], ["Alpha", "Gamma"])
        self.assertEqual(res["faction_players_unknown"], 1)
        self.assertIn("no player status", res["note"])
        self.assertEqual(self.names(controlling_only=True, player_faction=False, sort="name"), ["Beta", "Gamma"])

    def test_results_carry_is_player(self):
        by = {r["system"]: r["matching_factions"][0]["is_player"]
              for r in self.find(controlling_only=True, player_faction=True)["results"]}
        self.assertEqual(by, {"Alpha": True, "Gamma": None})

    def test_no_player_filter_means_no_unknown_count(self):
        self.assertEqual(self.find(controlling_only=True)["faction_players_unknown"], 0)

    def test_system_info_and_presence_show_is_player(self):
        flags = {f["name"]: f["is_player"] for f in queries.system_info(self.s, "Alpha")["factions"]}
        self.assertEqual(flags, {"PlayerOne": True, "Filler": None})
        self.assertTrue(queries.faction_presence(self.s, "PlayerOne")["systems"][0]["is_player"])


class StateSinceTests(PlayerCase):
    def setUp(self):
        super().setUp()
        self.feed(1, "Fresh", [(30, fac("P1", 0.5)), (20, fac("P1", 0.5)), (5, expanding("P1"))])
        self.feed(2, "Old", [(100, expanding("P2")), (70, expanding("P2")), (40, expanding("P2")), (10, expanding("P2"))])
        self.feed(3, "Npc", [(30, fac("N1", 0.5)), (5, expanding("N1"))])
        self.feed(4, "Unknown", [(30, fac("U1", 0.5)), (5, expanding("U1"))])
        self.feed(5, "Ended", [(40, fac("P3", 0.5)), (30, expanding("P3")), (10, fac("P3", 0.5))])
        self.feed(6, "Lonely", [(3, expanding("P4"))])
        self.feed(7, "NotBoss", [(30, fac("P5", 0.3)), (5, expanding("P5", 0.3))],
                  others=("Boss",), controller="Boss")
        self.feed(8, "Farmland", [(30, fac("P6", 0.5)), (5, expanding("P6"))], economy="Agri")
        self.flags({"P1": True, "P2": True, "N1": False, "P3": True, "P4": True, "P5": True, "P6": True})

    def q(self, **kw):
        kw = dict(faction_state="Expansion", state_in="active", state_since_hours=48, sort="name") | kw
        return self.find(**kw)

    def test_only_runs_that_began_inside_the_window_match(self):
        names = [r["system"] for r in self.q()["results"]]
        self.assertIn("Fresh", names)
        self.assertNotIn("Old", names)            # in Expansion for ~100h, anchor rows must not reset the start
        self.assertNotIn("Ended", names)          # expansion is over
        self.assertIn("Old", [r["system"] for r in self.q(state_since_hours=200)["results"]])

    def test_start_time_and_age_are_reported(self):
        f = next(r for r in self.q()["results"] if r["system"] == "Fresh")["matching_factions"][0]
        self.assertAlmostEqual(f["state_hours_ago"], 5, delta=0.2)
        self.assertTrue(f["state_started_at"].endswith("Z"))
        self.assertFalse(f["start_is_lower_bound"])

    def test_history_starting_inside_the_run_is_flagged_as_lower_bound(self):
        f = next(r for r in self.q()["results"] if r["system"] == "Lonely")["matching_factions"][0]
        self.assertTrue(f["start_is_lower_bound"])

    def test_pending_does_not_count_as_active(self):
        self.feed(9, "Pending", [(30, fac("P7", 0.5)), (5, fac("P7", 0.5, pending=[("Expansion", 1)]))])
        self.assertNotIn("Pending", [r["system"] for r in self.q()["results"]])
        self.assertIn("Pending", [r["system"] for r in self.q(state_in="pending")["results"]])

    def test_the_full_query(self):
        res = self.q(economies=["Industrial"], min_population=500, controlling_only=True, player_faction=True)
        self.assertEqual([r["system"] for r in res["results"]], ["Fresh", "Lonely", "Unknown"])
        self.assertEqual(res["faction_players_unknown"], 1)          # Unknown: kept, flagged
        res = self.q(economies=["Industrial"], controlling_only=True, player_faction=True, min_population=2000)
        self.assertEqual(res["results"], [])

    def test_validation(self):
        self.assertIn("error", queries.find_systems(self.s, state_since_hours=5))
        self.assertIn("error", queries.find_systems(self.s, faction_state="Expansion", state_since_hours=0))


class InterfaceTests(PlayerCase):
    def setUp(self):
        super().setUp()
        self.feed(1, "Fresh", [(30, fac("P1", 0.5)), (5, expanding("P1"))])
        self.flags({"P1": True})

    def run_cli(self, *argv):
        args = cli.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            args.func(args, self.s)
        return out.getvalue()

    def test_cli_flags(self):
        data = json.loads(self.run_cli("find-systems", "--economy", "Industrial", "--controlling-only",
                                       "--player-faction", "yes", "--state", "Expansion", "--state-in", "active",
                                       "--state-since-hours", "48", "--json"))
        self.assertEqual([r["system"] for r in data["results"]], ["Fresh"])
        self.assertTrue(data["results"][0]["matching_factions"][0]["is_player"])
        text = self.run_cli("find-systems", "--controlling-only", "--player-faction", "yes", "--state", "Expansion",
                            "--state-since-hours", "48")
        self.assertIn("[P] P1", text)
        self.assertIn("since ", text)
        csv_text = self.run_cli("find-systems", "--controlling-only", "--player-faction", "yes", "--csv")
        self.assertIn("[P] P1", csv_text)

    def test_mcp_tool_passes_the_options_through(self):
        with mock.patch.object(mcp_server, "_run") as run:
            mcp_server.find_systems(player_faction=True, state_since_hours=24.0)
        kw = run.call_args.kwargs
        self.assertEqual((kw["player_faction"], kw["state_since_hours"]), (True, 24.0))


if __name__ == "__main__":
    unittest.main()
