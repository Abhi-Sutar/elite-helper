"""PostgreSQL + TimescaleDB behaviour. Skipped unless TEST_DATABASE_URL points at the (disposable!) elite_test DB."""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import func, select, text

import helpers
from eddn_helper import copydata, database, migrate, parsers, queries, store
from eddn_helper.models import Commodity, Market, PriceHistory
from test_eddn_helper import commodity_msg, jump_msg, ts


@unittest.skipUnless(helpers.PG_URL, "set TEST_DATABASE_URL to the elite_test database to run these")
class TimescaleTests(unittest.TestCase):
    def setUp(self):
        self.s = helpers.new_session()

    def tearDown(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def feed(self, msg):
        store.apply_parsed(self.s, parsers.parse_message(msg))
        self.s.commit()

    def use_mode(self, mode):
        patcher = mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: mode})
        patcher.start()
        self.addCleanup(patcher.stop)

    def scalar(self, sql):
        return self.s.execute(text(sql)).scalar()

    def test_history_defaults_to_changes_on_postgres(self):
        self.assertEqual(store.history_mode(self.s), "changes")

    def test_price_history_is_a_compressed_hypertable_with_both_policies(self):
        self.assertTrue(store.history_is_hypertable(self.s))
        self.assertTrue(self.scalar("SELECT compression_enabled FROM timescaledb_information.hypertables "
                                    "WHERE hypertable_name = 'price_history'"))
        procs = {r[0] for r in self.s.execute(text(
            "SELECT proc_name FROM timescaledb_information.jobs WHERE hypertable_name = 'price_history'"))}
        self.assertTrue({"policy_compression", "policy_retention"} <= procs, procs)
        self.assertEqual(migrate.status(self.s.get_bind())["current"], migrate.SCHEMA_HEAD)

    def test_faction_history_is_a_compressed_hypertable_without_a_retention_policy(self):
        self.assertTrue(store.history_is_hypertable(self.s, "faction_history"))
        self.assertTrue(self.scalar("SELECT compression_enabled FROM timescaledb_information.hypertables "
                                    "WHERE hypertable_name = 'faction_history'"))
        jobs = {r[0]: r[1] for r in self.s.execute(text(
            "SELECT proc_name, config::text FROM timescaledb_information.jobs WHERE hypertable_name = 'faction_history'"))}
        self.assertNotIn("policy_retention", jobs)
        self.assertIn("policy_compression", jobs)

    def test_faction_observations_states_and_left_markers_on_postgres(self):
        self.use_mode("full")  # these check full-mode behaviour (a row for every reading)
        from test_timeline import at, fac, sys_msg
        self.feed(sys_msg([fac("A", 0.6, pending=[("Expansion", 1)]), fac("B", 0.4)], at(5)))
        self.feed(sys_msg([fac("A", 0.6, pending=[("Expansion", 1)])], at(4)))
        rows = queries.faction_history(self.s, "Sol", 7)["history"]
        self.assertEqual(len(rows), 4)  # A twice, B once present, B once left
        a = [r for r in rows if r["faction"] == "A"][0]
        self.assertEqual(a["pending_states"], [{"state": "Expansion", "trend": 1}])
        t = queries.faction_timeline(self.s, "Sol", "B")
        self.assertEqual(len(t["left_events"]), 1)

    def test_event_logs_are_compressed_hypertables_pruned_by_store_prune(self):
        for table in ("market_update", "system_update"):
            self.assertTrue(store.history_is_hypertable(self.s, table), table)
            jobs = {r[0]: r[1] for r in self.s.execute(text(
                f"SELECT proc_name, config::text FROM timescaledb_information.jobs WHERE hypertable_name = '{table}'"))}
            self.assertNotIn("policy_retention", jobs, table)
            self.assertIn("policy_compression", jobs, table)

    def test_changes_mode_event_log_and_change_rows_on_postgres(self):
        from test_traffic import GOLD_TEA, at, fac, sys_msg
        from eddn_helper.models import MarketUpdate, SystemUpdate
        for h in (5, 4, 3):
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(h)))
            self.feed(sys_msg([fac("A", 0.6)], at(h)))
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 2)
        self.assertEqual(self.s.execute(select(func.count()).select_from(MarketUpdate)).scalar_one(), 3)
        self.assertEqual(self.s.execute(select(func.count()).select_from(SystemUpdate)).scalar_one(), 3)
        t = queries.station_traffic(self.s, "Hub", days=1)
        self.assertEqual(t["updates"], 3)
        self.assertEqual(queries.busiest_stations(self.s, days=1)["stations"][0]["updates"], 3)
        self.assertEqual(queries.price_history(self.s, "Hub", "gold", days=1, resample_minutes=60)["updates"], 3)

    def test_deleting_a_market_cascades_into_the_event_log(self):
        from eddn_helper.models import MarketUpdate
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(2)))
        self.s.delete(self.s.get(Market, 7))
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(MarketUpdate)).scalar_one(), 0)

    def test_deleting_a_system_cascades_into_the_faction_hypertable(self):
        from test_timeline import at, fac, sys_msg
        from eddn_helper.models import FactionHistory, System
        self.feed(sys_msg([fac("A", 0.6)], at(3)))
        self.s.delete(self.s.get(System, 1))
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(FactionHistory)).scalar_one(), 0)

    def test_new_price_columns_survive_compression(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0)], when=ts(60)))
        self.s.execute(text("SELECT compress_chunk(c, if_not_compressed => true) "
                            "FROM show_chunks('price_history', older_than => INTERVAL '1 day') c"))
        self.s.commit()
        pt = queries.price_history(self.s, "hub", "gold", max_points=0)["series"][0]
        self.assertEqual((pt["buy_price"], pt["mean_price"], pt["stock_bracket"]), (1000, 1000, 2))

    def test_every_update_is_stored_and_late_upload_leaves_snapshot_alone(self):
        self.use_mode("full")  # these check full-mode behaviour (a row for every reading)
        for h in (5, 4.9, 4.8):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0), ("tea", 5, 5, 0, 0)], when=ts(h)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(9)))  # late
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 7)
        self.assertEqual(queries.station_market(self.s, "hub")["commodities"][0]["buy_price"], 1000)
        self.assertEqual(queries.station_activity(self.s, "hub")["updates"], 4)

    def test_snapshot_upsert_keeps_commodities_missing_from_a_partial_message(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(3)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 9, 1, 0, 0)], when=ts(2)))
        self.assertEqual([(c["commodity"], c["buy_price"]) for c in
                          queries.station_market(self.s, "hub")["commodities"]], [("gold", 9), ("tea", 2)])

    def test_deleting_a_market_cascades_into_the_hypertable(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(2)))
        self.s.delete(self.s.get(Market, 7))
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 0)

    def test_compressed_chunks_stay_queryable_and_deletable(self):
        for h in (60, 59, 58):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000 + int(h), 100, 0, 0)], when=ts(h)))
        compressed = self.scalar("SELECT count(*) FROM (SELECT compress_chunk(c, if_not_compressed => true) "
                                 "FROM show_chunks('price_history', older_than => INTERVAL '1 day') c) x")
        self.s.commit()
        self.assertGreaterEqual(compressed, 1)
        hist = queries.price_history(self.s, "hub", "gold", max_points=0)
        self.assertEqual(hist["updates"], 3)
        self.s.delete(self.s.get(Market, 7))  # cascade through compressed data
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 0)

    def test_prune_keeps_the_newest_update_row_per_key_in_compressed_event_logs(self):
        from test_timeline import at, fac, sys_msg
        from eddn_helper.models import MarketUpdate, SystemUpdate
        for days_ago in (800, 790, 3):  # Hub: two rows over a year old, one recent
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(24 * days_ago)))
        for days_ago in (800, 790):  # Old Hub: nothing recent, its newest row must still survive
            self.feed(commodity_msg(8, "Old Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(24 * days_ago)))
        for days_ago in (800, 790, 3):
            self.feed(sys_msg([fac("A", 0.5)], at(24 * days_ago)))
        compressed = 0
        for table in ("market_update", "system_update"):
            compressed += self.scalar(f"SELECT count(*) FROM (SELECT compress_chunk(c, if_not_compressed => true) "
                                      f"FROM show_chunks('{table}', older_than => INTERVAL '1 day') c) x")
        self.s.commit()
        self.assertGreaterEqual(compressed, 2)

        out = store.prune(self.s)
        self.assertEqual((out["market_update"], out["system_update"]), (3, 2))
        rows = self.s.execute(select(MarketUpdate.market_id, func.count()).group_by(MarketUpdate.market_id)
                              .order_by(MarketUpdate.market_id)).all()
        self.assertEqual(rows, [(7, 1), (8, 1)])
        newest_old_hub = self.s.execute(select(func.max(MarketUpdate.ts)).where(MarketUpdate.market_id == 8)).scalar_one()
        self.assertLess(newest_old_hub, datetime.now(timezone.utc) - timedelta(days=700))
        self.assertEqual(self.s.execute(select(func.count()).select_from(SystemUpdate)).scalar_one(), 1)

    def test_prune_leaves_history_to_the_retention_policy(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(2)))
        self.assertEqual(store.prune(self.s)["price_history"], 0)
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 1)

    def test_commodity_ids_keep_working_after_copy_from_sqlite(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src_url = f"sqlite:///{(tmp / 'src.sqlite').as_posix()}"
        src_engine = database.make_engine(src_url)
        migrate.upgrade(src_engine, backup=False)
        src = database.make_session_factory(src_engine)()
        for msg in (jump_msg("Sol", 1, [0, 0, 0]),
                    commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(2))):
            store.apply_parsed(src, parsers.parse_message(msg))
        src.commit()
        src.close()
        src_engine.dispose()

        result = copydata.copy_data(src_url, helpers.PG_URL, progress=lambda m: None)
        for table, n in result.items():
            self.assertEqual(n["source"], n["dest"], table)
        self.s.expire_all()
        top = self.s.execute(select(func.max(Commodity.id))).scalar_one()
        new_id = store.commodity_ids(self.s, ["brandnewcommodity"])["brandnewcommodity"]
        self.assertGreater(new_id, top)  # the id sequence was reset after the copy


if __name__ == "__main__":
    unittest.main()
