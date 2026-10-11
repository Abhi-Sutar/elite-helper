"""Economy lookup and find_systems (economy + faction state + distance + demand), CLI and live (EDSM) paths."""
import contextlib
import io
import json
import unittest
from unittest import mock

from eddn_helper import cli, external, live, parsers, queries, store
from helpers import new_session
from test_eddn_helper import commodity_msg
from test_traffic import at, fac, sys_msg


def system(name, id64, x, economy, second="None", factions=None):
    msg = sys_msg(factions or [fac("Alpha", 0.6), fac("Beta", 0.4)], at(1), id64=id64, name=name)
    msg["message"]["StarPos"] = [x, 0, 0]
    msg["message"]["SystemEconomy"] = f"$economy_{economy};"
    msg["message"]["SystemSecondEconomy"] = f"$economy_{second};"
    return msg


def market(market_id, station, system_name, economies, items=(("gold", 10, 5, 0, 0),)):
    msg = commodity_msg(market_id, station, system_name, list(items), when=at(1))
    msg["message"]["economies"] = [{"name": e, "proportion": 1.0} for e in economies]
    return msg


class EconomyCase(unittest.TestCase):
    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.close)
        feed = lambda m: store.apply_parsed(self.s, parsers.parse_message(m))   # noqa: E731
        feed(system("Sol", 1, 0, "HighTech"))
        feed(system("Near", 2, 10, "Extraction", "Refinery",
                    [fac("Miners", 0.7, pending=[("Expansion", 1)]), fac("Other", 0.3)]))
        feed(system("Far", 3, 50, "Extraction",
                    factions=[fac("Remote", 0.5, state="Expansion", active=["Expansion"]), fac("Else", 0.5)]))
        feed(system("Farm", 4, 5, "Agri", factions=[fac("Growers", 0.9), fac("Side", 0.1, pending=[("Expansion", 0)])]))
        feed(market(10, "Mine Hub", "Near", ["Extraction"], [("bertrandite", 0, 0, 900, 400), ("gold", 10, 5, 0, 0)]))
        feed(market(11, "Sol Works", "Sol", ["Industrial", "Extraction"]))
        feed(market(12, "ABC-123", "Near", ["Carrier"]))                          # a fleet carrier
        store.link_markets_to_systems(self.s)
        self.s.commit()

    def close(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def names(self, **kw):
        res = queries.find_systems(self.s, **kw)
        self.assertNotIn("error", res)
        return [r["system"] for r in res["results"]]

    def test_normalisation_follows_edsm_and_eddn_spellings(self):
        self.assertEqual({queries.norm_key(v) for v in ("High Tech", "hightech", "HighTech", "high_tech")}, {"hightech"})
        self.assertEqual(queries.norm_key("Agriculture"), queries.norm_key("Agri"))

    def test_system_economy_lists_stations_carriers_last(self):
        d = queries.system_economy(self.s, "near")
        self.assertEqual((d["economy"], d["second_economy"]), ("Extraction", "Refinery"))
        self.assertEqual([x["station"] for x in d["stations"]], ["Mine Hub", "ABC-123"])
        self.assertEqual(d["stations"][0]["economies"], ["Extraction"])
        self.assertTrue(d["stations"][1]["is_carrier"])
        self.assertFalse(queries.system_economy(self.s, "Nowhere")["found"])

    def test_economy_search_orders_by_distance_and_honours_range(self):
        self.assertEqual(self.names(economies="extraction", near="Sol"), ["Near", "Far"])
        self.assertEqual(self.names(economies=["Extraction"], near="Sol", ly=30), ["Near"])
        self.assertEqual(self.names(economies="Agriculture"), ["Farm"])           # alias of Agri
        self.assertEqual(self.names(economies="Extraction,HighTech", near="Sol"), ["Sol", "Near", "Far"])

    def test_second_economy_can_be_excluded(self):
        self.assertEqual(self.names(economies="Refinery"), ["Near"])
        self.assertEqual(self.names(economies="Refinery", include_second=False), [])

    def test_station_scope_finds_stations_in_other_economies(self):
        res = queries.find_systems(self.s, economies="Industrial", economy_scope="station")
        self.assertEqual([r["system"] for r in res["results"]], ["Sol"])
        self.assertEqual(res["results"][0]["matched_economy"], "station")
        self.assertEqual(self.names(economies="Industrial", economy_scope="system"), [])
        self.assertEqual(sorted(self.names(economies="Extraction", economy_scope="any")), ["Far", "Near", "Sol"])

    def test_faction_state_in_pending_active_and_any(self):
        pending = queries.find_systems(self.s, faction_state="Expansion", state_in="pending", near="Sol")
        self.assertEqual([r["system"] for r in pending["results"]], ["Farm", "Near"])
        near = next(r for r in pending["results"] if r["system"] == "Near")
        self.assertEqual(near["matching_factions"][0]["faction"], "Miners")
        self.assertEqual(near["matching_factions"][0]["found_in"], [{"in": "pending", "trend": 1}])
        self.assertEqual(self.names(faction_state="expansion", state_in="active"), ["Far"])
        self.assertEqual(sorted(self.names(faction_state="Expansion")), ["Far", "Farm", "Near"])
        far = queries.find_systems(self.s, faction_state="Expansion")["results"]
        far = next(r for r in far if r["system"] == "Far")
        self.assertEqual(len(far["matching_factions"]), 1)                       # one entry per faction, not per hit
        self.assertEqual([h["in"] for h in far["matching_factions"][0]["found_in"]], ["state", "active"])

    def test_state_combined_with_economy_distance_and_controlling_only(self):
        self.assertEqual(self.names(economies="Extraction", faction_state="Expansion", near="Sol", ly=30), ["Near"])
        self.assertEqual(self.names(economies="Agri", faction_state="Expansion", state_in="pending"), ["Farm"])
        # Farm's pending Expansion belongs to a non-controlling faction
        self.assertEqual(self.names(economies="Agri", faction_state="Expansion", state_in="pending",
                                    controlling_only=True), [])

    def test_demand_filter_uses_station_markets(self):
        res = queries.find_systems(self.s, demand_commodities=["Bertrandite", "coltan"])
        self.assertEqual([r["system"] for r in res["results"]], ["Near"])
        self.assertEqual(res["results"][0]["demand"][0]["station"], "Mine Hub")
        self.assertEqual(self.names(economies="Extraction", faction_state="Expansion",
                                    demand_commodities="bertrandite"), ["Near"])
        self.assertEqual(self.names(demand_commodities="coltan"), [])

    def test_bad_input_is_reported(self):
        self.assertIn("error", queries.find_systems(self.s, economy_scope="galaxy"))
        self.assertIn("error", queries.find_systems(self.s, state_in="soon"))
        self.assertIn("error", queries.find_systems(self.s, near="Unknown", economies="Agri"))

    def test_cli_find_systems_and_economy(self):
        def run(*argv):
            args = cli.build_parser().parse_args(argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                args.func(args, self.s)
            return out.getvalue()

        data = json.loads(run("find-systems", "--economy", "Extraction", "--state", "Expansion", "--near", "Sol",
                              "--ly", "100", "--json"))
        self.assertEqual([r["system"] for r in data["results"]], ["Near", "Far"])
        csv_text = run("find-systems", "--economy", "Extraction", "--state", "Expansion", "--csv")
        self.assertTrue(csv_text.startswith("system,distance_ly,population,economy"))
        self.assertIn("Miners", csv_text)
        text = run("find-systems", "--state", "Expansion", "--state-in", "pending", "--near", "Sol")
        self.assertIn("2 matching systems", text)
        self.assertIn("Miners", text)
        text = run("economy", "Near")
        self.assertIn("Extraction / Refinery", text)
        self.assertIn("Mine Hub", text)


EDSM = {
    "/api-v1/system": {"id64": 99, "name": "Zed", "coords": {"x": 1, "y": 2, "z": 3},
                       "information": {"population": 5000, "allegiance": "Empire", "government": "Dictatorship",
                                       "security": "Medium", "economy": "Industrial", "secondEconomy": "Refinery",
                                       "faction": "Zed Holdings"}},
    "/api-system-v1/factions": {"controllingFaction": {"name": "Zed Holdings"}, "factions": [
        {"name": "Zed Holdings", "influence": 0.8, "state": "None", "lastUpdate": 1760000000},
        {"name": "Zed Rivals", "influence": 0.2, "state": "None", "lastUpdate": 1760000000}]},
    "/api-system-v1/stations": {"name": "Zed", "stations": [
        {"name": "Zed Port", "type": "Coriolis Starport", "economy": "Industrial", "secondEconomy": "Refinery",
         "haveMarket": True, "marketId": 777, "distanceToArrival": 120},
        {"name": "Zed Outpost", "type": "Outpost", "economy": "Extraction", "secondEconomy": "Extraction",
         "haveMarket": False}]},
}


class LiveEconomyTests(unittest.TestCase):
    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.close)

    def close(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def test_unknown_system_is_fetched_from_edsm_with_its_stations(self):
        with mock.patch.object(external, "_get", side_effect=lambda path, **p: EDSM[path]):
            d = live.system_economy(self.s, "Zed", live=True)
        self.assertTrue(d["found"])
        self.assertEqual((d["economy"], d["second_economy"], d["source"]), ("Industrial", "Refinery", "edsm"))
        self.assertEqual([x["station"] for x in d["stations_live"]], ["Zed Port", "Zed Outpost"])
        self.assertEqual(d["stations_live"][0]["economies"], ["Industrial", "Refinery"])
        self.assertEqual(d["stations_live"][1]["economies"], ["Extraction"])    # EDSM repeats a missing second economy
        self.assertEqual(queries.system_economy(self.s, "zed")["economy"], "Industrial")      # stored for next time

    def test_without_live_nothing_is_fetched(self):
        with mock.patch.object(external, "_get", side_effect=AssertionError("no network without --live")):
            self.assertFalse(live.system_economy(self.s, "Zed")["found"])

    def test_edsm_failure_is_reported_not_raised(self):
        with mock.patch.object(external, "_get", side_effect=external.LiveLookupError("EDSM request failed: boom")):
            d = live.system_economy(self.s, "Zed", live=True)
        self.assertFalse(d["found"])
        self.assertIn("boom", d["live_error"])


if __name__ == "__main__":
    unittest.main()
