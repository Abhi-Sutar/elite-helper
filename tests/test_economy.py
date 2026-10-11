"""Economy lookup and find_systems (economy + faction state + distance + demand), CLI and live (EDSM) paths."""
import contextlib
import io
import json
import unittest
from unittest import mock

from sqlalchemy import delete, select

from eddn_helper import cli, external, live, parsers, queries, store
from eddn_helper.models import Station, StationSync
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


def world(name, id64, x, economy, second="None", factions=None, when=None, population=1000, allegiance="Federation",
          government="Democracy", security="high", conflicts=None):
    msg = sys_msg(factions or [fac("Alpha", 0.6), fac("Beta", 0.4)], when or at(1), id64=id64, name=name)
    m = msg["message"]
    m.update({"StarPos": [x, 0, 0], "SystemEconomy": f"$economy_{economy};", "SystemSecondEconomy": f"$economy_{second};",
              "Population": population, "SystemAllegiance": allegiance, "SystemGovernment": f"$government_{government};",
              "SystemSecurity": f"$system_security_{security};"})
    if conflicts:
        m["Conflicts"] = conflicts
    return msg


def station(market_id, name, system_name, economies, stype):
    msg = market(market_id, name, system_name, economies)
    msg["message"]["stationType"] = stype
    return msg


WAR = [{"WarType": "war", "Status": "active", "Faction1": {"Name": "Dela", "Stake": "", "WonDays": 1},
        "Faction2": {"Name": "Delb", "Stake": "", "WonDays": 0}}]


class FilterCase(unittest.TestCase):
    """Exclusions and the other find_systems filters on a small world (the origin is Alpha)."""

    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.close)
        feed = lambda m: store.apply_parsed(self.s, parsers.parse_message(m))   # noqa: E731
        feed(world("Alpha", 1, 0, "Agri", factions=[fac("Grow", 0.5, pending=[("Expansion", 1)], band=1),
                                                     fac("Rival", 0.5)]))
        feed(world("Bravo", 2, 2, "Industrial",
                   factions=[fac("Bravo Co", 0.6), fac("Brav2", 0.4, pending=[("Expansion", 0)])],
                   government="Corporate"))
        feed(world("Charlie", 3, 3, "Industrial", "Refinery",
                   [fac("Chas", 0.7, pending=[("Expansion", 0)]), fac("Cb", 0.3)], allegiance="Empire"))
        feed(world("Delta", 4, 4, "Agri",
                   factions=[fac("Dela", 0.5, state="War", active=["War"], pending=[("Expansion", 0)]),
                             fac("Delb", 0.5, state="War", active=["War"])],
                   when=at(0.2), conflicts=WAR, security="low"))
        feed(world("Echo", 5, 5, "Industrial",
                   factions=[fac("Echo Corp", 0.8, state="Expansion", active=["Expansion"]), fac("Ech2", 0.2)],
                   when=at(5), population=5_000_000, government="Corporate", security="low"))
        feed(station(20, "A Port", "Alpha", ["Agri"], "Coriolis"))
        feed(station(21, "B Outpost", "Bravo", ["Industrial", "Extraction"], "Outpost"))
        store.link_markets_to_systems(self.s)
        for id64 in range(1, 6):                      # EDSM has been asked for every system: station filters apply
            store.replace_stations(self.s, id64, [])
        self.s.commit()

    def close(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def names(self, **kw):
        res = queries.find_systems(self.s, **kw)
        self.assertNotIn("error", res, res)
        return [r["system"] for r in res["results"]]

    def test_the_agri_or_industrial_expansion_query_without_extraction_or_refinery(self):
        q = dict(economies=["Agricultural", "Industrial"], faction_state="Expansion", state_in="pending", near="Alpha")
        self.assertEqual(self.names(**q), ["Alpha", "Bravo", "Charlie", "Delta"])
        # Bravo has an Extraction station, Charlie a Refinery second economy
        self.assertEqual(self.names(exclude_economies=["Extraction", "Refinery"], **q), ["Alpha", "Delta"])
        self.assertEqual(self.names(exclude_economies="extraction,refinery", exclude_scope="system", **q),
                         ["Alpha", "Bravo", "Delta"])
        self.assertEqual(self.names(exclude_economies="Extraction", exclude_scope="station", **q),
                         ["Alpha", "Charlie", "Delta"])

    def test_exclude_states_systems_and_population(self):
        self.assertEqual(self.names(economies="Agri", exclude_states="War"), ["Alpha"])   # Delta has factions at war
        self.assertEqual(self.names(economies="Agri", exclude_states="Expansion", exclude_states_in="pending"), [])
        self.assertEqual(self.names(economies="Industrial", exclude_systems=["bravo", "Echo"]), ["Charlie"])
        self.assertEqual(self.names(max_population=10_000, near="Alpha"), ["Alpha", "Bravo", "Charlie", "Delta"])
        self.assertEqual(self.names(min_population=100_000), ["Echo"])

    def test_allegiance_government_security_and_conflict(self):
        self.assertEqual(self.names(allegiance="empire"), ["Charlie"])
        self.assertEqual(self.names(government="Corporate", near="Alpha"), ["Bravo", "Echo"])
        self.assertEqual(self.names(security=["low"], near="Alpha"), ["Delta", "Echo"])
        self.assertEqual(self.names(in_conflict=True), ["Delta"])
        self.assertNotIn("Delta", self.names(in_conflict=False))
        self.assertEqual(self.names(conflict_type="War"), ["Delta"])
        self.assertEqual(self.names(conflict_type="Election"), [])
        self.assertEqual(queries.find_systems(self.s, in_conflict=True)["results"][0]["conflict_types"], ["war"])

    def test_faction_criteria_must_hold_for_the_same_faction(self):
        res = queries.find_systems(self.s, faction_state="Expansion", state_in="pending",
                                   min_influence=45, max_influence=55)
        self.assertEqual([r["system"] for r in res["results"]], ["Alpha", "Delta"])   # Bravo 40% and Charlie 70% fall out
        self.assertEqual(self.names(faction_state="Expansion", state_in="pending", min_influence=65), ["Charlie"])
        self.assertEqual(self.names(faction_state="Expansion", state_in="active", min_influence=65), ["Echo"])
        self.assertEqual(sorted(self.names(faction_state="Expansion", state_in="pending", controlling_only=True)),
                         ["Alpha", "Charlie", "Delta"])                                # Bravo's pending is not controlling
        self.assertEqual(self.names(faction_name="echo corp"), ["Echo"])
        self.assertEqual(self.names(faction_name="Echo"), [])                          # exact name, not a prefix
        self.assertEqual(self.names(faction_state="Expansion", state_in="pending", state_trend=1), ["Alpha"])
        grow = queries.find_systems(self.s, faction_name="Grow")["results"][0]["matching_factions"][0]
        self.assertEqual(self.names(faction_name="Grow", happiness=[grow["happiness"]]), ["Alpha"])
        self.assertEqual(self.names(faction_name="Grow", happiness=["NotAMood"]), [])

    def test_station_filters(self):
        self.assertEqual(self.names(min_stations=1, near="Alpha"), ["Alpha", "Bravo"])
        self.assertEqual(self.names(station_types="coriolis"), ["Alpha"])
        self.assertEqual(self.names(station_types=["outpost", "coriolis"], near="Alpha"), ["Alpha", "Bravo"])
        self.assertEqual(self.names(min_stations=2), [])

    def test_sorting(self):
        self.assertEqual(self.names(sort="population")[0], "Echo")
        self.assertEqual(self.names(sort="name"), ["Alpha", "Bravo", "Charlie", "Delta", "Echo"])
        self.assertEqual(self.names(sort="freshest")[0], "Delta")
        self.assertEqual(self.names(sort="freshest")[-1], "Echo")
        self.assertEqual(self.names(faction_state="Expansion", sort="influence")[0], "Echo")   # 80% beats 70%, 50%
        self.assertEqual(self.names(near="Alpha")[:2], ["Alpha", "Bravo"])                      # distance is the default

    def test_bad_arguments(self):
        for kw in ({"sort": "random"}, {"exclude_scope": "galaxy"}, {"exclude_states_in": "soon"}, {"state_trend": 1}):
            self.assertIn("error", queries.find_systems(self.s, **kw), kw)

    def test_cli_and_mcp_pass_the_new_options_through(self):
        def run(*argv):
            args = cli.build_parser().parse_args(argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                args.func(args, self.s)
            return out.getvalue()

        data = json.loads(run("find-systems", "--economy", "Agricultural,Industrial", "--state", "Expansion",
                              "--state-in", "pending", "--exclude-economy", "Extraction,Refinery", "--near", "Alpha",
                              "--json"))
        self.assertEqual([r["system"] for r in data["results"]], ["Alpha", "Delta"])
        self.assertEqual(data["criteria"]["exclude_economies"], ["extraction", "refinery"])
        data = json.loads(run("find-systems", "--conflict", "yes", "--security", "low", "--sort", "name", "--json"))
        self.assertEqual([r["system"] for r in data["results"]], ["Delta"])
        self.assertIn("Alpha", run("find-systems", "--faction", "Grow", "--min-influence", "40"))

        from eddn_helper import mcp_server
        with mock.patch.object(mcp_server, "_run", side_effect=lambda fn, *a, **k: fn(self.s, *a, **k)):
            res = mcp_server.find_systems(economies=["Agri", "Industrial"], faction_state="Expansion",
                                          state_in="pending", exclude_economies=["Extraction", "Refinery"],
                                          near="Alpha")
        self.assertEqual([r["system"] for r in res["results"]], ["Alpha", "Delta"])


def edsm_station(name, stype, economy, faction, body=None, market=True, market_id=None, second=None):
    s = {"id": abs(hash(name)) % 10**6, "marketId": market_id, "type": stype, "name": name,
         "distanceToArrival": 26, "allegiance": "Federation", "government": "Democracy", "economy": economy,
         "secondEconomy": second, "haveMarket": market, "haveShipyard": False, "haveOutfitting": False,
         "otherServices": ["Refuel"], "updateTime": {"information": "2026-10-09 21:39:05"}}
    if body:
        s["body"] = {"id": 1, "name": body, "latitude": 1.0, "longitude": 2.0}
    if faction:
        s["controllingFaction"] = {"id": 5, "name": faction}
    return s


HUB_EDSM = {"id": 1, "id64": 7, "name": "Hub", "stations": [
    edsm_station("Hub Orbis", "Orbis Starport", "Industrial", "Grow", "Hub A 1", market_id=900),
    edsm_station("Evans Farm", "Odyssey Settlement", "Agriculture", "Grow", "Hub B 3", market_id=901),
    edsm_station("Mouhot Prospect", "Planetary Outpost", "Colony", "Rival", "Hub B 3", market=False),
    edsm_station("Pit Stop", "Outpost", "Extraction", "Grow", market_id=902, second="Extraction"),
    edsm_station("ABC-123", "Fleet Carrier", "Fleet Carrier", None, market_id=903),
    edsm_station("No Owner", "Odyssey Settlement", "Tourism", None, "Hub B 3")]}


class StationCase(unittest.TestCase):
    """EDSM station data: storage, settlements, controlling faction, unknown systems, CLI/MCP plumbing."""

    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.close)
        feed = lambda m: store.apply_parsed(self.s, parsers.parse_message(m))   # noqa: E731
        feed(world("Hub", 7, 0, "Agri", factions=[fac("Grow", 0.6, pending=[("Expansion", 0)]), fac("Rival", 0.4)]))
        feed(world("Lonely", 8, 3, "Agri", factions=[fac("Solo", 0.9, pending=[("Expansion", 0)]), fac("Two", 0.1)]))
        feed(market(40, "Hub Orbis", "Hub", ["Refinery"]))                   # a local market for a station EDSM also lists
        store.link_markets_to_systems(self.s)
        self.s.commit()

    def close(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def refresh(self, payload=HUB_EDSM, name="Hub"):
        with mock.patch.object(external, "_get", side_effect=lambda path, **p: payload):
            return external.refresh_stations(self.s, name)

    def test_refresh_stores_everything_but_carriers(self):
        self.assertEqual(self.refresh(), 5)
        rows = {r.name: r for r in self.s.execute(select(Station)).scalars()}
        self.assertEqual(sorted(rows), ["Evans Farm", "Hub Orbis", "Mouhot Prospect", "No Owner", "Pit Stop"])
        self.assertNotIn("ABC-123", rows)
        self.assertEqual(rows["Evans Farm"].controlling_faction, "Grow")
        self.assertEqual(rows["Evans Farm"].body, "Hub B 3")
        self.assertEqual(rows["Pit Stop"].economy, "Extraction")
        self.assertIsNone(rows["No Owner"].controlling_faction)
        sync = self.s.execute(select(StationSync)).scalar_one()
        self.assertEqual((sync.system_id64, sync.station_count), (7, 5))

    def test_refresh_replaces_and_a_system_without_stations_is_still_known(self):
        self.refresh()
        smaller = dict(HUB_EDSM, stations=HUB_EDSM["stations"][:1])
        self.assertEqual(self.refresh(smaller), 1)
        self.assertEqual([r.name for r in self.s.execute(select(Station)).scalars()], ["Hub Orbis"])
        self.assertEqual(self.refresh(dict(HUB_EDSM, stations=[])), 0)
        self.assertEqual(self.s.execute(select(StationSync.station_count)).scalar_one(), 0)
        d = queries.system_economy(self.s, "Hub")
        self.assertTrue(d["stations_known"])
        self.assertEqual([x["station"] for x in d["stations"]], ["Hub Orbis"])   # the market upload is still listed

    def test_unknown_system_or_not_in_database_is_not_stored(self):
        self.assertIsNone(self.refresh({}, "Hub"))                                  # EDSM does not know it
        self.assertIsNone(self.refresh(dict(HUB_EDSM, id64=999), "Hub"))            # not in our systems table

    def test_settlements_and_planetary_outposts_are_ordinary_stations(self):
        self.refresh()
        for types, expected in (("settlement", ["Evans Farm", "No Owner"]), (["settlements"], ["Evans Farm", "No Owner"]),
                                ("planetary", ["Mouhot Prospect"]), ("surface", ["Evans Farm", "Mouhot Prospect", "No Owner"]),
                                ("outpost", ["Mouhot Prospect", "Pit Stop"]), ("orbis", ["Hub Orbis"])):
            res = queries.find_systems(self.s, station_types=types, faction_state="Expansion", state_in="pending",
                                       near="Hub", ly=1)
            hub = res["results"][0]
            self.assertEqual(hub["system"], "Hub")
            kept = [st["station"] for st in hub["stations"]
                    if queries.station_matches_types(st, {queries.norm_key(t) for t in queries._as_list(types)})]
            self.assertEqual(sorted(kept), expected, types)
        self.assertEqual(queries.station_category("Odyssey Settlement"), "surface_settlement")
        self.assertEqual(queries.station_category("OnFootSettlement"), "surface_settlement")
        self.assertEqual(queries.station_category("Planetary Outpost"), "planetary_outpost")
        self.assertEqual(queries.station_category("CraterPort"), "planetary_port")
        self.assertEqual(queries.station_category("Outpost"), "outpost")
        self.assertEqual(queries.station_category("Coriolis Starport"), "starport")
        self.assertEqual(queries.station_category("Fleet Carrier"), "carrier")
        # min_stations counts settlements and outposts, not just starports
        hub = lambda **kw: [r["system"] for r in queries.find_systems(self.s, economies="Agri", near="Hub", ly=1, **kw)["results"]]  # noqa: E731
        self.assertEqual(hub(min_stations=5), ["Hub"])        # 5 stations: orbis, settlement x2, planetary outpost, outpost
        self.assertEqual(hub(min_stations=6), [])

    def test_unknown_systems_are_flagged_not_rejected_known_ones_are_filtered(self):
        # before any refresh nothing is known: station filters let everything through, flagged
        res = queries.find_systems(self.s, economies="Agri", min_stations=3, station_types="settlement")
        self.assertEqual(sorted(r["system"] for r in res["results"]), ["Hub", "Lonely"])
        self.assertEqual(res["stations_unknown"], 2)
        self.assertIn("refresh-stations", res["note"])
        self.assertFalse(res["results"][0]["stations_known"])
        # after refreshing Hub it is judged on its stations, Lonely is still only flagged
        self.refresh()
        res = queries.find_systems(self.s, economies="Agri", min_stations=6)
        self.assertEqual([r["system"] for r in res["results"]], ["Lonely"])
        res = queries.find_systems(self.s, economies="Agri", min_stations=5, station_types="settlement")
        self.assertEqual(sorted(r["system"] for r in res["results"]), ["Hub", "Lonely"])
        self.assertEqual(res["stations_unknown"], 1)
        self.assertEqual(res["unknown_systems"], ["Lonely"])

    def test_exclusion_sees_a_known_extraction_station_even_when_other_systems_are_unknown(self):
        self.refresh()
        names = [r["system"] for r in queries.find_systems(self.s, economies="Agri", exclude_economies="Extraction")["results"]]
        self.assertEqual(names, ["Lonely"])                                         # Hub's Pit Stop is Extraction

    def test_controlling_faction_of_stations(self):
        self.refresh()
        res = queries.find_systems(self.s, faction_state="Expansion", state_in="pending", near="Hub", ly=5)
        hub = next(r for r in res["results"] if r["system"] == "Hub")
        self.assertEqual(hub["stations_by_faction"], {"Grow": 3, "Rival": 1})
        self.assertEqual(hub["matching_factions"][0]["stations_controlled"], 3)
        flagged = {st["station"]: st["controlled_by_matching_faction"] for st in hub["stations"]}
        self.assertEqual(flagged, {"Evans Farm": True, "Hub Orbis": True, "Pit Stop": True, "Mouhot Prospect": False,
                                   "No Owner": False})
        merged = next(st for st in hub["stations"] if st["station"] == "Hub Orbis")
        self.assertEqual((merged["source"], merged["has_prices"], merged["economies"]), ("both", True, ["Industrial", "Refinery"]))
        self.assertEqual(self.names_of(min_faction_stations=3), ["Hub", "Lonely"])         # Lonely unknown -> kept
        self.assertEqual(self.names_of(min_faction_stations=4), ["Lonely"])

    def names_of(self, **kw):
        res = queries.find_systems(self.s, faction_state="Expansion", state_in="pending", **kw)
        return sorted(r["system"] for r in res["results"])

    def test_faction_stations_query(self):
        self.refresh()
        res = queries.faction_stations(self.s, "grow")
        self.assertEqual(res["count"], 3)
        self.assertEqual(sorted(r["station"] for r in res["stations"]), ["Evans Farm", "Hub Orbis", "Pit Stop"])
        res = queries.faction_stations(self.s, "Grow", station_types="settlement")
        self.assertEqual([r["station"] for r in res["stations"]], ["Evans Farm"])
        self.assertEqual(queries.faction_stations(self.s, "Grow", near="Lonely", ly=1)["count"], 0)
        self.assertEqual(queries.faction_stations(self.s, "Nobody")["count"], 0)
        self.assertIn("error", queries.faction_stations(self.s, "Grow", near="Nowhere"))

    def test_cli_refresh_and_find_systems_refresh(self):
        def run(*argv):
            args = cli.build_parser().parse_args(argv)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                args.func(args, self.s)
            return out.getvalue(), err.getvalue()

        with mock.patch.object(external, "_get", side_effect=lambda path, **p: HUB_EDSM):
            _, err = run("refresh-stations", "Hub")
            self.assertIn("Hub: 5 stations stored", err)
            _, err = run("refresh-stations", "Atlantis")                              # unknown to the payload's system id
            self.assertIn("Atlantis", err)
        text, _ = run("faction-stations", "Grow", "--station-type", "settlement")
        self.assertIn("Evans Farm", text)
        self.assertNotIn("Hub Orbis", text)
        text, _ = run("economy", "Hub")
        self.assertIn("Controlled by", text)
        self.assertIn("Odyssey Settlement", text)
        self.assertNotIn("ABC-123", text)

        # --refresh-stations fetches only the unknown result systems, in result order, within the budget
        self.s.execute(delete(StationSync.__table__))
        self.s.execute(delete(Station.__table__))
        self.s.commit()
        payloads = {"Hub": HUB_EDSM, "Lonely": {"id64": 8, "name": "Lonely", "stations": []}}
        calls = []

        def fake(path, **p):
            calls.append(p["systemName"])
            return payloads[p["systemName"]]

        with mock.patch.object(external, "_get", side_effect=fake):
            out, err = run("find-systems", "--economy", "Agri", "--refresh-stations", "--refresh-limit", "1",
                           "--near", "Hub", "--json")
        self.assertEqual(calls, ["Hub"])                                              # budget of one system
        data = json.loads(out)
        self.assertEqual(data["stations_unknown"], 1)
        self.assertIn("refreshed stations of Hub: 5 stored", err)

    def test_reader_role_cannot_refresh_and_says_so(self):
        from sqlalchemy.exc import ProgrammingError
        with mock.patch.object(external, "refresh_stations", side_effect=ProgrammingError("x", {}, Exception("denied"))):
            n, err = cli.refresh_system_stations(self.s, "Hub")
        self.assertIsNone(n)
        self.assertIn("read-only", err)

    def test_mcp_tools_accept_the_station_arguments(self):
        from eddn_helper import mcp_server
        self.refresh()
        with mock.patch.object(mcp_server, "_run", side_effect=lambda fn, *a, **k: fn(self.s, *a, **k)):
            res = mcp_server.find_systems(economies=["Agri"], min_faction_stations=1, faction_state="Expansion",
                                          state_in="pending", station_types=["settlement"], near="Hub", ly=1)
            fs = mcp_server.faction_stations("Grow", station_types=["settlement"])
        self.assertEqual([r["system"] for r in res["results"]], ["Hub"])
        self.assertEqual([r["station"] for r in fs["stations"]], ["Evans Farm"])


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
