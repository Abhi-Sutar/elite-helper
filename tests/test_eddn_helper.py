import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import func, select, update

from eddn_helper import parsers, queries, store
from eddn_helper.models import CommodityPrice, Market, PriceHistory
from helpers import new_session


def ts(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def commodity_msg(market_id, station, system, items, when=None):
    return {"$schemaRef": "https://eddn.edcd.io/schemas/commodity/3",
            "message": {"timestamp": when or ts(1), "systemName": system, "stationName": station,
                        "marketId": market_id, "stationType": "Orbis",
                        "commodities": [{"name": n, "meanPrice": 1000, "buyPrice": b, "stock": st,
                                         "stockBracket": 2, "sellPrice": s, "demand": d,
                                         "demandBracket": 2} for n, b, st, s, d in items]}}


def jump_msg(name, id64, pos, when=None, infl=(0.6, 0.4), state="Boom", event="FSDJump"):
    return {"$schemaRef": "https://eddn.edcd.io/schemas/journal/1",
            "message": {"timestamp": when or ts(1), "event": event, "StarSystem": name,
                        "SystemAddress": id64, "StarPos": pos, "Population": 1000,
                        "SystemAllegiance": "Federation", "SystemEconomy": "$economy_HighTech;",
                        "SystemSecondEconomy": "$economy_None;", "SystemGovernment": "$government_Democracy;",
                        "SystemSecurity": "$system_security_high;",
                        "SystemFaction": {"Name": "Alpha", "FactionState": state},
                        "Factions": [
                            {"Name": "Alpha", "FactionState": state, "Government": "$government_Democracy;",
                             "Influence": infl[0], "Allegiance": "Federation",
                             "Happiness": "$Faction_HappinessBand2;",
                             "ActiveStates": [{"State": state}], "PendingStates": [{"State": "Expansion", "Trend": 0}]},
                            {"Name": "Beta", "FactionState": "None", "Government": "$government_Anarchy;",
                             "Influence": infl[1], "Allegiance": "Independent"}]}}


class ParserTests(unittest.TestCase):
    def test_commodity(self):
        p = parsers.parse_message(commodity_msg(1, "ABC-123", "Sol", [("Gold", 9000, 50, 0, 0)]))
        self.assertEqual(p["kind"], "market")
        self.assertTrue(p["market"]["is_carrier"])
        self.assertEqual(p["prices"][0]["commodity"], "gold")

    def test_journal_clean_tokens(self):
        p = parsers.parse_message(jump_msg("Sol", 1, [0, 0, 0]))
        s = p["system"]
        self.assertEqual((s["economy"], s["second_economy"], s["government"], s["security"]),
                         ("HighTech", None, "Democracy", "high"))
        a = p["factions"][0]
        self.assertEqual((a["happiness"], a["is_controlling"], a["pending_states"]),
                         ("Happy", True, [{"state": "Expansion", "trend": 0}]))

    def test_rejects_test_schema_and_other_events(self):
        m = commodity_msg(1, "X", "Y", [("gold", 1, 1, 1, 1)])
        m["$schemaRef"] += "/test"
        self.assertIsNone(parsers.parse_message(m))
        j = jump_msg("Sol", 1, [0, 0, 0], event="Docked")
        self.assertIsNone(parsers.parse_message(j))

    def test_future_timestamp_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(parsers.parse_message(jump_msg("Sol", 1, [0, 0, 0], when=future)))


class StoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.s = new_session()

    def tearDown(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def feed(self, msg):
        wrote = store.apply_parsed(self.s, parsers.parse_message(msg))
        self.s.commit()
        return wrote

    def test_stale_system_does_not_overwrite(self):
        self.feed(jump_msg("Sol", 1, [0, 0, 0], when=ts(1), infl=(0.7, 0.3)))
        self.feed(jump_msg("Sol", 1, [0, 0, 0], when=ts(5), infl=(0.1, 0.9)))
        info = queries.system_info(self.s, "sol")
        self.assertEqual(info["factions"][0]["influence_pct"], 70.0)
        self.assertEqual(info["economy"], "HighTech")

    def test_history_only_on_change(self):
        patcher = mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: "thresholded"})  # PostgreSQL defaults to full
        patcher.start()
        self.addCleanup(patcher.stop)
        self.feed(jump_msg("Sol", 1, [0, 0, 0], when=ts(10), infl=(0.5, 0.5)))
        self.feed(jump_msg("Sol", 1, [0, 0, 0], when=ts(8), infl=(0.5, 0.5)))
        self.feed(jump_msg("Sol", 1, [0, 0, 0], when=ts(2), infl=(0.55, 0.45)))
        h = queries.faction_history(self.s, "Sol", 7, "Alpha")["history"]
        self.assertEqual([r["influence_pct"] for r in h], [50.0, 55.0])

    def test_stale_market_ignored(self):
        self.feed(commodity_msg(5, "Hub", "Sol", [("gold", 9000, 50, 0, 0)], when=ts(1)))
        self.feed(commodity_msg(5, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(9)))
        m = queries.station_market(self.s, "hub")
        self.assertEqual(m["commodities"][0]["buy_price"], 9000)

    def test_unknown_market_older_than_retention_is_stored_but_not_a_superseded_report(self):
        self.assertTrue(self.feed(commodity_msg(8, "Old", "Sol", [("gold", 1, 1, 0, 0)], when=ts(24 * 40))))
        self.assertIsNotNone(self.s.get(Market, 8))
        self.assertFalse(self.feed(commodity_msg(8, "Old", "Sol", [("gold", 2, 1, 0, 0)], when=ts(24 * 50))))

    def test_market_links_to_system(self):
        self.feed(commodity_msg(5, "Hub", "Sol", [("gold", 9000, 50, 0, 0)]))
        self.assertIsNone(self.s.get(Market, 5).system_id64)
        self.feed(jump_msg("Sol", 1, [0, 0, 0]))
        self.assertEqual(store.link_markets_to_systems(self.s), 1)
        self.s.expire_all()
        self.assertEqual(self.s.get(Market, 5).system_id64, 1)

    def test_duplicate_system_name(self):
        """Two real systems can share a name; market reports must not fail and must stay on their system."""
        self.feed(jump_msg("Twin", 1, [0, 0, 0], when=ts(5)))
        self.feed(jump_msg("Twin", 2, [50, 0, 0], when=ts(1)))
        self.assertTrue(self.feed(commodity_msg(20, "Hub", "Twin", [("gold", 9000, 50, 0, 0)], when=ts(3))))
        self.assertEqual(self.s.get(Market, 20).system_id64, 2)        # most recently updated system
        self.s.execute(update(Market).where(Market.market_id == 20).values(system_id64=1))
        self.s.commit()
        self.assertTrue(self.feed(commodity_msg(20, "Hub", "Twin", [("gold", 9100, 50, 0, 0)], when=ts(2))))
        self.s.expire_all()
        self.assertEqual(self.s.get(Market, 20).system_id64, 1)        # a linked market keeps its system
        self.assertEqual(store.resolve_system_id(self.s, "twin"), 2)
        self.assertEqual(queries.find_system(self.s, "TWIN").id64, 2)

    def test_link_markets_skips_ambiguous_names(self):
        self.feed(commodity_msg(21, "Hub", "Twin", [("gold", 9000, 50, 0, 0)]))
        self.feed(commodity_msg(22, "Port", "Solo", [("gold", 9000, 50, 0, 0)]))
        self.feed(jump_msg("Twin", 1, [0, 0, 0]))
        self.feed(jump_msg("Twin", 2, [50, 0, 0]))
        self.feed(jump_msg("Solo", 3, [9, 0, 0]))
        self.assertEqual(store.link_markets_to_systems(self.s), 1)
        self.s.expire_all()
        self.assertIsNone(self.s.get(Market, 21).system_id64)
        self.assertEqual(self.s.get(Market, 22).system_id64, 3)

    def test_find_commodity_lists_a_linked_market_once_despite_duplicate_name(self):
        self.feed(jump_msg("Sol", 1, [0, 0, 0]))
        self.feed(jump_msg("Twin", 2, [10, 0, 0]))
        self.feed(jump_msg("Twin", 3, [60, 0, 0]))
        self.feed(commodity_msg(23, "Hub", "Twin", [("gold", 8000, 500, 0, 0)]))
        rows = queries.find_commodity(self.s, "gold", "buy", "Sol", 100)
        self.assertEqual([r["station"] for r in rows["results"]].count("Hub"), 1)

    def test_find_commodity_and_trades(self):
        self.feed(jump_msg("Sol", 1, [0, 0, 0]))
        self.feed(jump_msg("Near", 2, [10, 0, 0]))
        self.feed(jump_msg("Far", 3, [500, 0, 0]))
        self.feed(commodity_msg(10, "Mine", "Sol", [("gold", 8000, 500, 0, 0)]))
        self.feed(commodity_msg(11, "Shop", "Near", [("gold", 0, 0, 9500, 400)]))
        self.feed(commodity_msg(12, "Faraway", "Far", [("gold", 0, 0, 20000, 400)]))
        r = queries.find_commodity(self.s, "Gold", "sell", "Sol", 50)
        self.assertEqual([x["station"] for x in r["results"]], ["Shop"])
        t = queries.best_trades(self.s, "Sol", 50, cargo=100)
        self.assertEqual(t["trades"][0]["profit_per_ton"], 1500)
        self.assertEqual(t["trades"][0]["total_profit"], 150000)

    def test_price_history_threshold_throttle_and_prune(self):
        patcher = mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: "thresholded"})  # PostgreSQL defaults to full
        patcher.start()
        self.addCleanup(patcher.stop)

        def mk(price, h):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", price, 100, 0, 0)], when=ts(h)))
        mk(1000, 40)    # first sight: recorded
        mk(1010, 30)    # +1%: skipped
        mk(1100, 29)    # +10%: recorded
        mk(1300, 28.9)  # big change but inside the 1h throttle: skipped
        h = queries.price_history(self.s, "hub", "Gold", days=30)
        self.assertEqual([r["buy_price"] for r in h["series"]], [1000, 1100])
        self.assertEqual(h["buy_price_stats"]["max"], 1100)
        if not store.history_is_hypertable(self.s):
            # old history rows are pruned (on a hypertable the retention policy does this, and a row cannot
            # be moved to another chunk by UPDATE, so that case is covered in test_pg_timescale.py)
            old = self.s.execute(select(PriceHistory).where(PriceHistory.buy_price == 1000)).scalar_one()
            old.ts = datetime(2000, 1, 1, tzinfo=timezone.utc)
            self.s.commit()
            store.prune(self.s, 30)
            self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 1)
        # a stale market keeps its last report; only older snapshot rows and history go
        self.s.get(Market, 7).updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.s.execute(update(CommodityPrice).values(seen_at=datetime(2000, 1, 1, tzinfo=timezone.utc)))
        self.s.commit()
        store.prune(self.s, 30)
        self.assertIsNotNone(self.s.get(Market, 7))
        self.assertEqual(self.s.execute(select(func.count()).select_from(CommodityPrice)).scalar_one(), 1)

    def test_prune_drops_snapshot_rows_superseded_by_a_newer_report(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(24 * 40)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 5, 1, 0, 0)], when=ts(1)))
        store.prune(self.s, 30)
        self.assertEqual([c["commodity"] for c in queries.station_market(self.s, "hub")["commodities"]], ["gold"])

    def test_commodity_name_normalization(self):
        self.assertEqual(queries.norm_commodity("Liquid Oxygen"), "liquidoxygen")


if __name__ == "__main__":
    unittest.main()
