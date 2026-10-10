import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import func, select

from eddn_helper import copydata, database, migrate, parsers, queries, store
from eddn_helper.models import Commodity, CommodityPrice, Market, PriceHistory

from helpers import new_session
from test_eddn_helper import commodity_msg, jump_msg, ts


def new_file_session(url):
    engine = database.make_engine(url)
    migrate.upgrade(engine, backup=False)
    return database.make_session_factory(engine)()


class FullHistoryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: "full"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s = new_session()

    def tearDown(self):
        engine = self.s.get_bind()
        self.s.close()
        engine.dispose()

    def feed(self, msg):
        wrote = store.apply_parsed(self.s, parsers.parse_message(msg))
        self.s.commit()
        return wrote

    def count_history(self):
        return self.s.execute(select(func.count()).select_from(PriceHistory)).scalar_one()

    def test_every_update_is_stored_even_when_nothing_changed(self):
        for h in (5, 4.9, 4.8, 4.7):  # same values, minutes apart: thresholded mode would keep only the first
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0), ("tea", 50, 5, 0, 0)],
                                    when=ts(h)))
        self.assertEqual(self.count_history(), 8)

    def test_same_timestamp_duplicates_are_ignored(self):
        when = ts(2)
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0)], when=when))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0)], when=when))
        self.assertEqual(self.count_history(), 1)

    def test_late_upload_goes_to_history_but_not_to_snapshot(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 2000, 100, 0, 0)], when=ts(1)))
        self.assertTrue(self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1500, 100, 0, 0)], when=ts(3))))
        self.assertEqual(self.count_history(), 2)
        self.assertEqual(queries.station_market(self.s, "hub")["commodities"][0]["buy_price"], 2000)

    def test_prune_expires_old_history_in_full_mode(self):
        if store.history_is_hypertable(self.s):
            self.skipTest("on a hypertable the retention policy drops old chunks (covered in test_pg_timescale)")
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(2)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 2, 1, 0, 0)], when=ts(1)))
        oldest = self.s.execute(select(PriceHistory).order_by(PriceHistory.ts)).scalars().first()
        oldest.ts = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.s.commit()
        self.assertEqual(store.prune(self.s, 30)["price_history"], 1)
        self.assertEqual(self.count_history(), 1)

    def test_market_message_older_than_retention_is_not_stored(self):
        self.assertFalse(self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=ts(24 * 40))))
        self.assertEqual(self.count_history(), 0)

    def test_snapshot_drops_commodities_that_disappear(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(3)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 5, 1, 0, 0)], when=ts(2)))
        snap = queries.station_market(self.s, "hub")["commodities"]
        self.assertEqual([(c["commodity"], c["buy_price"]) for c in snap], [("gold", 5)])
        # history still has every observation
        self.assertEqual(self.count_history(), 3)

    def test_activity_counts_distinct_updates(self):
        for h in (30, 29, 28):
            self.feed(commodity_msg(7, "N0B-T4X", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(h)))
        a = queries.station_activity(self.s, "n0b-t4x")
        self.assertEqual((a["updates"], a["history_mode"]), (3, "full"))
        self.assertEqual(sum(d["updates"] for d in a["per_day"]), 3)

    def test_price_history_auto_buckets_long_series(self):
        for i in range(60):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1000 + i, 10, 0, 0)], when=ts(10 - i / 10)))
        raw = queries.price_history(self.s, "hub", "gold", max_points=0)
        self.assertEqual((raw["updates"], raw["points"], "bucket_minutes" in raw), (60, 60, False))
        auto = queries.price_history(self.s, "hub", "gold", max_points=10)
        self.assertIn("bucket_minutes", auto)
        self.assertLessEqual(auto["points"], 11)
        self.assertEqual(auto["updates"], 60)
        self.assertEqual(sum(b["updates"] for b in auto["series"]), 60)
        manual = queries.price_history(self.s, "hub", "gold", bucket_minutes=120)
        self.assertEqual(manual["bucket_minutes"], 120)
        self.assertTrue(all(b["buy_min"] <= b["buy_avg"] <= b["buy_max"] for b in manual["series"]))


class ModeSelectionTests(unittest.TestCase):
    def test_default_is_thresholded_on_sqlite_and_env_overrides(self):
        engine = database.make_engine("sqlite://")
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(store.HISTORY_MODE_ENV, None)
            self.assertEqual(store.history_mode(engine), "thresholded")
            os.environ[store.HISTORY_MODE_ENV] = "full"
            self.assertEqual(store.history_mode(engine), "full")
            os.environ[store.HISTORY_MODE_ENV] = "bogus"
            with self.assertRaises(ValueError):
                store.history_mode(engine)
        engine.dispose()

    def test_thresholded_mode_still_skips_unchanged_updates(self):
        with mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: "thresholded"}):
            s = new_session()
            for h in (5, 4, 3):
                store.apply_parsed(s, parsers.parse_message(
                    commodity_msg(7, "Hub", "Sol", [("gold", 1000, 100, 0, 0)], when=ts(h))))
            s.commit()
            self.assertEqual(s.execute(select(func.count()).select_from(PriceHistory)).scalar_one(), 1)
            s.get_bind().dispose()


class CopyDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def url(self, name):
        return f"sqlite:///{(self.tmp / name).as_posix()}"

    def test_copy_preserves_rows_and_ids_and_is_idempotent(self):
        with mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: "full"}):
            src = new_file_session(self.url("src.sqlite"))
            for msg in (jump_msg("Sol", 1, [0, 0, 0]),
                        commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(3)),
                        commodity_msg(7, "Hub", "Sol", [("gold", 5, 1, 0, 0), ("tea", 2, 2, 0, 0)], when=ts(2))):
                store.apply_parsed(src, parsers.parse_message(msg))
            src.commit()
            ids_before = dict(src.execute(select(Commodity.symbol, Commodity.id)).all())
            src.get_bind().dispose()

        dst = database.make_engine(self.url("dst.sqlite"))
        migrate.upgrade(dst, backup=False)
        dst.dispose()

        quiet = lambda msg: None
        first = copydata.copy_data(self.url("src.sqlite"), self.url("dst.sqlite"), batch=2, progress=quiet)
        second = copydata.copy_data(self.url("src.sqlite"), self.url("dst.sqlite"), batch=2, progress=quiet)
        self.assertEqual(first, second)
        for table, n in first.items():
            self.assertEqual(n["source"], n["dest"], table)
        self.assertEqual(first["price_history"]["dest"], 4)

        out = new_file_session(self.url("dst.sqlite"))
        self.assertEqual(dict(out.execute(select(Commodity.symbol, Commodity.id)).all()), ids_before)
        self.assertEqual(out.execute(select(func.count()).select_from(CommodityPrice)).scalar_one(), 2)
        self.assertEqual(queries.station_market(out, "hub")["commodities"][0]["buy_price"], 5)
        self.assertEqual(out.get(Market, 7).system_id64, 1)
        out.get_bind().dispose()

    def test_refuses_same_database(self):
        e = database.make_engine(self.url("one.sqlite"))
        migrate.upgrade(e, backup=False)
        e.dispose()
        with self.assertRaises(ValueError):
            copydata.copy_data(self.url("one.sqlite"), self.url("one.sqlite"))


if __name__ == "__main__":
    unittest.main()
