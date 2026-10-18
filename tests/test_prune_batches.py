"""store.prune_* steps: bounded deletes in committed batches that a later call can resume."""

import time
import unittest

from sqlalchemy import func, select

from eddn_helper import parsers, store
from eddn_helper.models import CommodityPrice, MarketUpdate, PriceHistory, SystemUpdate
from helpers import new_session
from test_eddn_helper import commodity_msg, ts
from test_traffic import at, fac, sys_msg

GOLD = [("gold", 9000, 50, 0, 0)]


class BatchedPruneTests(unittest.TestCase):
    def setUp(self):
        self.s = new_session()
        self.addCleanup(self.s.get_bind().dispose)
        self.addCleanup(self.s.close)

    def feed(self, msg):
        store.apply_parsed(self.s, parsers.parse_message(msg))
        self.s.commit()

    def count(self, model):
        self.s.expire_all()
        return self.s.execute(select(func.count()).select_from(model)).scalar_one()

    def old_events(self):
        """Market 7: five rows over a year old plus a fresh one (5 expire); market 8: three old (2 expire);
        system 1: four old plus a fresh one (4 expire)."""
        # oldest first: a message older than 30 days that is not the market's newest report is dropped outright
        for d in range(404, 399, -1):
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD, when=ts(24 * d)))
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD, when=ts(1)))
        for d in range(402, 399, -1):
            self.feed(commodity_msg(8, "Old Hub", "Sol", GOLD, when=ts(24 * d)))
        for d in range(403, 399, -1):
            self.feed(sys_msg([fac("A", 0.5)], at(24 * d)))
        self.feed(sys_msg([fac("A", 0.5)], at(1)))

    def test_events_are_deleted_across_several_batches_and_the_newest_row_per_key_survives(self):
        self.old_events()
        out, finished = store.prune_events(self.s, batch=2)
        self.assertEqual((out, finished), ({"market_update": 7, "system_update": 4}, True))
        self.assertEqual((self.count(MarketUpdate), self.count(SystemUpdate)), (2, 1))
        per_market = dict(self.s.execute(select(MarketUpdate.market_id, func.count())
                                         .group_by(MarketUpdate.market_id)).all())
        self.assertEqual(per_market, {7: 1, 8: 1})

    def test_a_spent_time_budget_stops_after_a_batch_and_the_next_call_resumes(self):
        self.old_events()
        out, finished = store.prune_events(self.s, batch=2, deadline=time.monotonic() - 1)
        self.assertEqual((out, finished), ({"market_update": 2}, False))
        self.s.rollback()                       # each batch was committed, so none of it is undone
        self.assertEqual(self.count(MarketUpdate), 9 - 2)
        out, finished = store.prune_events(self.s, batch=2)
        self.assertEqual((out, finished), ({"market_update": 5, "system_update": 4}, True))

    def test_nothing_expired_is_a_finished_noop(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD, when=ts(1)))
        self.assertEqual(store.prune_events(self.s, batch=2), ({"market_update": 0, "system_update": 0}, True))

    def test_commodity_prices_superseded_by_a_newer_report_go_in_batches(self):
        names = ["gold", "tea", "tin", "iron", "copper"]
        self.feed(commodity_msg(7, "Hub", "Sol", [(n, 1, 1, 0, 0) for n in names], when=ts(24 * 40)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 5, 1, 0, 0)], when=ts(1)))
        out, finished = store.prune_commodity_prices(self.s, 30, batch=2)
        self.assertEqual((out, finished), ({"commodity_prices": 4}, True))
        self.assertEqual(self.count(CommodityPrice), 1)

    def test_a_markets_last_report_is_kept_however_old(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0), ("tea", 1, 1, 0, 0)], when=ts(24 * 90)))
        self.assertEqual(store.prune_commodity_prices(self.s, 30, batch=1), ({"commodity_prices": 0}, True))
        self.assertEqual(self.count(CommodityPrice), 2)

    def test_price_history_goes_in_batches(self):
        for d, price in ((42, 10), (41, 20), (40, 30)):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", price, 1, 0, 0)], when=ts(24 * d)))
        if store.history_is_hypertable(self.s, "price_history"):
            self.skipTest("a hypertable's retention policy expires history, not this step")
        self.assertEqual(self.count(PriceHistory), 3)
        self.assertEqual(store.prune_price_history(self.s, 30, batch=2), ({"price_history": 3}, True))
        self.assertEqual(self.count(PriceHistory), 0)

    def test_prune_still_runs_every_step_to_completion(self):
        self.old_events()
        self.feed(commodity_msg(9, "Other", "Sol", [("gold", 1, 1, 0, 0), ("tea", 1, 1, 0, 0)], when=ts(24 * 40)))
        self.feed(commodity_msg(9, "Other", "Sol", [("gold", 5, 1, 0, 0)], when=ts(1)))
        out = store.prune(self.s, 30)
        self.assertEqual((out["market_update"], out["system_update"], out["commodity_prices"]), (7, 4, 1))


if __name__ == "__main__":
    unittest.main()
