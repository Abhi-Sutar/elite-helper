"""PostgreSQL + TimescaleDB behaviour. Skipped unless TEST_DATABASE_URL points at the (disposable!) elite_test DB."""

import shutil
import tempfile
import unittest
from pathlib import Path

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

    def scalar(self, sql):
        return self.s.execute(text(sql)).scalar()

    def test_history_defaults_to_full_on_postgres(self):
        self.assertEqual(store.history_mode(self.s), "full")

    def test_price_history_is_a_compressed_hypertable_with_both_policies(self):
        self.assertTrue(store.history_is_hypertable(self.s))
        self.assertTrue(self.scalar("SELECT compression_enabled FROM timescaledb_information.hypertables "
                                    "WHERE hypertable_name = 'price_history'"))
        procs = {r[0] for r in self.s.execute(text(
            "SELECT proc_name FROM timescaledb_information.jobs WHERE hypertable_name = 'price_history'"))}
        self.assertTrue({"policy_compression", "policy_retention"} <= procs, procs)
        self.assertEqual(migrate.status(self.s.get_bind())["current"], "0002")

    def test_every_update_is_stored_and_late_upload_leaves_snapshot_alone(self):
        for h in (5, 4.9, 4.8):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0), ("tea", 5, 5, 0, 0)], when=ts(h)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(9)))  # late
        self.assertEqual(self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 7)
        self.assertEqual(queries.station_market(self.s, "hub")["commodities"][0]["buy_price"], 1000)
        self.assertEqual(queries.station_activity(self.s, "hub")["updates"], 4)

    def test_snapshot_upsert_drops_vanished_commodities(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(3)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 9, 1, 0, 0)], when=ts(2)))
        self.assertEqual([(c["commodity"], c["buy_price"]) for c in
                          queries.station_market(self.s, "hub")["commodities"]], [("gold", 9)])

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
