"""Faction history (every observation, left markers, thinning), timelines, price/amount series and CSV output."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import func, select, text

from eddn_helper import cli, parsers, queries, store
from eddn_helper.models import FactionHistory, PriceHistory
from helpers import new_session
from test_eddn_helper import commodity_msg, jump_msg


NOW = datetime.now(timezone.utc).replace(microsecond=0)  # fixed once, so every `at()` is deterministic


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fac(name, infl, state="None", band=2, active=(), pending=(), recovering=()):
    return {"Name": name, "FactionState": state, "Government": "$government_Democracy;", "Influence": infl,
            "Allegiance": "Federation", "Happiness": f"$Faction_HappinessBand{band};",
            "ActiveStates": [{"State": a} for a in active],
            "PendingStates": [{"State": n, "Trend": t} for n, t in pending],
            "RecoveringStates": [{"State": n, "Trend": t} for n, t in recovering]}


def sys_msg(factions, when, id64=1, name="Sol"):
    m = jump_msg(name, id64, [0, 0, 0], when=when)
    m["message"]["Factions"] = factions
    m["message"]["SystemFaction"] = {"Name": factions[0]["Name"]}
    return m


class ModeCase(unittest.TestCase):
    mode = "full"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {store.HISTORY_MODE_ENV: self.mode})
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

    def n_faction_rows(self, **where):
        q = select(func.count()).select_from(FactionHistory)
        for k, v in where.items():
            q = q.where(getattr(FactionHistory, k) == v)
        return self.s.execute(q).scalar_one()


class ParserStateTests(unittest.TestCase):
    def test_trends_are_kept_for_pending_and_recovering(self):
        m = sys_msg([fac("A", 0.5, active=["Boom"], pending=[("Expansion", 1)], recovering=[("Public Holiday", -1)])],
                    at(1))
        f = parsers.parse_message(m)["factions"][0]
        self.assertEqual(f["active_states"], [{"state": "Boom", "trend": None}])
        self.assertEqual(f["pending_states"], [{"state": "Expansion", "trend": 1}])
        self.assertEqual(f["recovering_states"], [{"state": "Public Holiday", "trend": -1}])
        self.assertEqual(parsers.state_names(f["pending_states"]), ["Expansion"])
        self.assertEqual(parsers.state_names(["Boom"]), ["Boom"])  # legacy plain strings


class FullFactionHistoryTests(ModeCase):
    def test_every_observation_is_logged_even_when_nothing_changed(self):
        for h in (5, 4, 3):
            self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(h)))
        self.assertEqual(self.n_faction_rows(), 6)

    def test_all_fields_are_stored_in_history(self):
        self.feed(sys_msg([fac("A", 0.6, state="Boom", band=3, active=["Boom"], pending=[("Expansion", 0)],
                                recovering=[("Public Holiday", 1)])], at(2)))
        p = queries.faction_history(self.s, "Sol", 7)["history"][0]
        self.assertEqual((p["influence_pct"], p["state"], p["happiness"], p["present"]),
                         (60.0, "Boom", "Discontented", True))
        self.assertEqual(p["active_states"], ["Boom"])
        self.assertEqual(p["pending_states"], [{"state": "Expansion", "trend": 0}])
        self.assertEqual(p["recovering_states"], [{"state": "Public Holiday", "trend": 1}])

    def test_late_message_is_history_only(self):
        self.feed(sys_msg([fac("A", 0.7)], at(1)))
        self.assertTrue(self.feed(sys_msg([fac("A", 0.1)], at(5))))
        self.assertEqual(self.n_faction_rows(), 2)
        self.assertEqual(queries.system_info(self.s, "Sol")["factions"][0]["influence_pct"], 70.0)

    def test_left_marker_is_written_once(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(5)))
        self.feed(sys_msg([fac("A", 1.0)], at(4)))
        self.feed(sys_msg([fac("A", 1.0)], at(3)))
        rows = queries.faction_history(self.s, "Sol", 7, "B")["history"]
        self.assertEqual([(r["present"], r["influence_pct"]) for r in rows], [(True, 40.0), (False, None)])
        self.assertEqual(self.n_faction_rows(faction="A"), 3)

    def test_faction_history_older_than_a_year_is_not_stored_and_is_pruned(self):
        self.feed(sys_msg([fac("A", 0.5)], at(24 * 400)))          # over the 1-year retention: not logged
        self.assertEqual(self.n_faction_rows(), 0)
        self.feed(sys_msg([fac("A", 0.5)], at(2)))
        self.feed(sys_msg([fac("A", 0.5)], at(1)))
        if store.history_is_hypertable(self.s, "faction_history"):
            self.skipTest("on a hypertable the retention policy drops old chunks (covered in test_pg_timescale)")
        oldest = self.s.execute(select(FactionHistory).order_by(FactionHistory.ts)).scalars().first()
        oldest.ts = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.s.commit()
        self.assertEqual(store.prune(self.s)["faction_history"], 1)
        self.assertEqual(self.n_faction_rows(), 1)

    def test_no_left_marker_on_first_message(self):
        self.feed(sys_msg([fac("A", 1.0)], at(2)))
        self.assertEqual(self.n_faction_rows(present=False), 0)

    def test_thinning_keeps_every_change(self):
        for i in range(40):
            self.feed(sys_msg([fac("A", 0.5, state="Boom" if i == 25 else "None")], at(40 - i)))
        thin = queries.faction_history(self.s, "Sol", 7, "A", max_points=10)
        self.assertEqual(thin["observations"], 40)
        self.assertLess(thin["returned"], 40)
        states = [p["state"] for p in thin["history"]]
        self.assertIn("Boom", states)
        self.assertEqual(thin["history"][0]["ts"], at(40))
        self.assertEqual(thin["history"][-1]["ts"], at(1))


class ThresholdedFactionHistoryTests(ModeCase):
    mode = "thresholded"

    def test_only_changes_are_logged(self):
        self.feed(sys_msg([fac("A", 0.6000)], at(9)))
        self.feed(sys_msg([fac("A", 0.6002)], at(8)))                       # below 0.0005: skipped
        self.assertEqual(self.n_faction_rows(), 1)
        self.feed(sys_msg([fac("A", 0.6002, band=4)], at(7)))               # happiness changed
        self.feed(sys_msg([fac("A", 0.6002, band=4, pending=[("War", 0)])], at(6)))   # pending state appeared
        self.feed(sys_msg([fac("A", 0.6002, band=4, pending=[("War", 1)])], at(5)))   # only the trend changed
        self.feed(sys_msg([fac("A", 0.6002, band=4, pending=[("War", 1)])], at(4)))   # identical: skipped
        self.assertEqual(self.n_faction_rows(), 4)

    def test_left_marker_also_written_in_thresholded_mode(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(5)))
        self.feed(sys_msg([fac("A", 1.0)], at(4)))
        self.assertEqual(self.n_faction_rows(faction="B", present=False), 1)


class TimelineTests(ModeCase):
    def test_intervals_for_states_happiness_and_leaving(self):
        self.feed(sys_msg([fac("A", 0.30, pending=[("Expansion", 0)]), fac("B", 0.7)], at(10)))
        self.feed(sys_msg([fac("A", 0.31, pending=[("Expansion", 1)]), fac("B", 0.69)], at(8)))
        self.feed(sys_msg([fac("A", 0.33, state="Expansion", band=3, active=["Expansion"]), fac("B", 0.67)], at(6)))
        self.feed(sys_msg([fac("A", 0.34, state="Expansion", band=3, active=["Expansion"],
                               recovering=[("Public Holiday", 0)]), fac("B", 0.66)], at(4)))
        self.feed(sys_msg([fac("B", 1.0)], at(2)))  # A leaves

        t = queries.faction_timeline(self.s, "sol", "a", days=7)
        self.assertEqual((t["faction"], t["observations"], t["left_events"]), ("A", 5, [at(2)]))
        self.assertEqual([p["influence_pct"] for p in t["influence"]], [30.0, 31.0, 33.0, 34.0])

        def one(field, value):
            hits = [iv for iv in t["intervals"] if iv["field"] == field and iv["value"] == value]
            self.assertEqual(len(hits), 1, (field, value, hits))
            return hits[0]
        pend = one("pending", "Expansion")
        self.assertEqual((pend["start"], pend["last_seen"], pend["ended_at"], pend["trend"]),
                         (at(10), at(8), at(6), 1))
        state = one("state", "Expansion")
        self.assertEqual((state["start"], state["last_seen"], state["ended_at"]), (at(6), at(4), at(2)))
        self.assertEqual(one("happiness", "Happy")["ended_at"], at(6))
        self.assertEqual(one("happiness", "Discontented")["ended_at"], at(2))
        self.assertEqual(one("recovering", "Public Holiday")["start"], at(4))
        self.assertEqual(one("active", "Expansion")["ended_at"], at(2))
        self.assertFalse([iv for iv in t["intervals"] if iv["field"] == "state" and iv["value"] == "None"])

    def test_open_interval_has_no_end(self):
        self.feed(sys_msg([fac("A", 0.5, state="Boom")], at(3)))
        self.feed(sys_msg([fac("A", 0.5, state="Boom")], at(1)))
        iv = queries.faction_timeline(self.s, "Sol", "A")["intervals"]
        self.assertEqual([(i["field"], i["value"], i["ended_at"]) for i in iv if i["field"] == "state"],
                         [("state", "Boom", None)])

    def test_unknown_faction_or_system(self):
        self.feed(sys_msg([fac("A", 0.5)], at(1)))
        self.assertFalse(queries.faction_timeline(self.s, "Sol", "Nobody")["found"])
        self.assertFalse(queries.faction_timeline(self.s, "Nowhere", "A")["found"])

    def test_legacy_plain_string_states_do_not_break_readers(self):
        self.feed(sys_msg([fac("A", 0.5, state="Boom", active=["Boom"])], at(2)))
        self.s.execute(text("UPDATE factions SET active_states = '[\"Boom\"]', pending_states = '[\"War\"]'"))
        self.s.execute(text("UPDATE faction_history SET active_states = '[\"Boom\"]', pending_states = '[\"War\"]'"))
        self.s.commit()
        f = queries.system_info(self.s, "Sol")["factions"][0]
        self.assertEqual((f["active_states"], f["pending_states"]), (["Boom"], ["War"]))
        iv = queries.faction_timeline(self.s, "Sol", "A")["intervals"]
        self.assertIn(("pending", "War"), [(i["field"], i["value"]) for i in iv])

    def test_faction_presence_lists_systems_with_history_counts(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(3), id64=1, name="Sol"))
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(2), id64=1, name="Sol"))
        self.feed(sys_msg([fac("A", 0.9)], at(2), id64=2, name="Lave"))
        res = queries.faction_presence(self.s, "a")
        by_sys = {r["system"]: r for r in res["systems"]}
        self.assertEqual((by_sys["Sol"]["history_rows"], by_sys["Lave"]["history_rows"]), (2, 1))
        self.assertEqual(by_sys["Sol"]["first_seen"], at(3))


class PriceSeriesTests(ModeCase):
    def feed_station(self, items_by_hour, market_id=7, station="Hub"):
        for h, items in items_by_hour:
            self.feed(commodity_msg(market_id, station, "Sol", items, when=at(h)))

    def test_mean_price_and_brackets_are_stored_in_history(self):
        self.feed_station([(2, [("gold", 1000, 50, 0, 0)])])
        row = self.s.execute(select(PriceHistory)).scalar_one()
        self.assertEqual((row.mean_price, row.stock_bracket, row.demand_bracket), (1000, 2, 2))
        pt = queries.price_history(self.s, "Hub", "gold", max_points=0)["series"][0]
        self.assertEqual((pt["mean_price"], pt["stock_bracket"], pt["demand_bracket"]), (1000, 2, 2))

    def test_buckets_carry_min_avg_max_for_prices_stock_and_demand(self):
        self.feed_station([
            (5, [("gold", 100, 10, 0, 0)]),        # selling side only
            (4, [("gold", 120, 0, 0, 0)]),         # sold out: stock 0 is a real value
            (3, [("gold", 0, 0, 90, 40)]),         # buying side only
            (2, [("gold", 0, 0, 110, 60)])])
        d = queries.price_history(self.s, "Hub", "gold", bucket_minutes=24 * 60)
        self.assertEqual(d["updates"], 4)
        self.assertEqual((d["stock_stats"]["max"], d["demand_stats"]["max"]), (10, 60))
        bucket = d["series"][0] if len(d["series"]) == 1 else None
        self.assertIsNotNone(bucket, d["series"])  # all four updates fall into one 24h bucket
        self.assertEqual((bucket["buy_min"], bucket["buy_avg"], bucket["buy_max"]), (100, 110, 120))
        self.assertEqual((bucket["sell_min"], bucket["sell_avg"], bucket["sell_max"]), (90, 100, 110))
        self.assertEqual((bucket["stock_min"], bucket["stock_max"]), (0, 10))   # zeros counted for amounts
        self.assertEqual((bucket["demand_min"], bucket["demand_avg"], bucket["demand_max"]), (0, 25, 60))
        self.assertEqual((bucket["updates"], bucket["mean_avg"]), (4, 1000))

    def test_list_and_unknown_commodities(self):
        self.feed_station([(3, [("gold", 1, 1, 0, 0), ("tea", 2, 2, 0, 0)]), (2, [("gold", 1, 1, 0, 0)])])
        d = queries.price_history(self.s, "Hub", "Gold, Tea, nosuchthing", max_points=0)
        self.assertEqual(sorted(d["series_by_commodity"]), ["gold", "nosuchthing", "tea"])
        self.assertEqual((d["series_by_commodity"]["gold"]["rows"], d["series_by_commodity"]["tea"]["rows"],
                          d["series_by_commodity"]["nosuchthing"]["rows"]), (2, 1, 0))

    def test_all_commodities_is_capped_unless_lifted(self):
        items = [(f"item{i:02d}", 10, 10, 0, 0) for i in range(30)]
        items[0] = ("item00", 10, 10, 0, 0)
        self.feed_station([(3, items), (2, [items[0]])])  # item00 is the busiest
        capped = queries.price_history(self.s, "Hub", "all", max_commodities=25)
        self.assertEqual((len(capped["series_by_commodity"]), capped["truncated"], capped["commodities_available"]),
                         (25, True, 30))
        self.assertIn("item00", capped["series_by_commodity"])
        full = queries.price_history(self.s, "Hub", "all", all_commodities=True)
        self.assertEqual((len(full["series_by_commodity"]), full["truncated"]), (30, False))

    def test_single_commodity_keeps_the_old_reply_shape(self):
        self.feed_station([(2, [("gold", 5, 5, 0, 0)])])
        d = queries.price_history(self.s, "Hub", "gold")
        self.assertEqual(d["commodity"], "gold")
        self.assertIn("series", d)
        self.assertNotIn("series_by_commodity", d)


class CsvTests(ModeCase):
    def test_price_csv_leaves_inactive_sides_empty(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 5, 0, 0)], when=at(2)))
        cols, rows = cli.price_csv(queries.price_history(self.s, "Hub", "gold", max_points=0))
        self.assertEqual(cols, cli.PRICE_RAW)
        self.assertEqual((rows[0]["commodity"], rows[0]["buy_price"], rows[0]["sell_price"], rows[0]["stock"]),
                         ("gold", 100, None, 5))

    def test_price_csv_switches_to_bucket_columns(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 5, 0, 0), ("tea", 3, 3, 0, 0)], when=at(2)))
        cols, rows = cli.price_csv(queries.price_history(self.s, "Hub", "all", bucket_minutes=60))
        self.assertEqual(cols, cli.PRICE_BUCKET)
        self.assertEqual({r["commodity"] for r in rows}, {"gold", "tea"})

    def test_faction_csv_flattens_state_lists(self):
        self.feed(sys_msg([fac("A", 0.5, active=["Boom"], pending=[("War", 1)], recovering=[("Riot", 0)])], at(2)))
        res = {"history": queries.faction_history(self.s, "Sol", 7)}
        cols, rows = cli.faction_csv(res)
        self.assertEqual(cols, cli.FACTION_COLUMNS)
        self.assertEqual((rows[0]["active_states"], rows[0]["pending_states"], rows[0]["recovering_states"]),
                         ("Boom", "War:1", "Riot:0"))

    def test_emit_writes_a_csv_header_and_rows(self):
        import io
        import contextlib
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 5, 0, 0)], when=at(2)))
        args = mock.Mock(csv=True, json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.emit(args, queries.price_history(self.s, "Hub", "gold", max_points=0), None, cli.price_csv)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], ",".join(cli.PRICE_RAW))
        self.assertTrue(lines[1].startswith(at(2)) and ",gold,100,,1000,5,0,2,2" in lines[1], lines[1])


if __name__ == "__main__":
    unittest.main()
