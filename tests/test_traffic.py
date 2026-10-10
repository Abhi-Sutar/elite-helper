"""Default 'changes' mode: update log (traffic) + change-only history, anchors, partial messages, late messages."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import func, select

from eddn_helper import cli, parsers, queries, store
from eddn_helper.models import (CommodityPrice, Faction, FactionHistory, MarketUpdate, PriceHistory, SystemUpdate)
from helpers import new_session
from test_eddn_helper import commodity_msg, jump_msg

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def hdr(msg, uploader="cmdr-a", software="EDMC"):
    msg = dict(msg)
    msg["header"] = {"uploaderID": uploader, "softwareName": software}
    return msg


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


GOLD_TEA = [("gold", 100, 50, 0, 0), ("tea", 5, 5, 0, 0)]


class ChangesCase(unittest.TestCase):
    mode = "changes"

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

    def count(self, model, **where):
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return self.s.execute(q).scalar_one()

    def events(self):
        return [(r.changed, r.commodities) for r in self.s.execute(
            select(MarketUpdate).order_by(MarketUpdate.ts, MarketUpdate.uploader)).scalars()]


class MarketChangeLogTests(ChangesCase):
    def test_repeated_identical_updates_store_no_new_price_rows_but_every_update_is_logged(self):
        for h in (5, 4, 3):
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(h)))
        self.assertEqual(self.count(PriceHistory), 2)                 # first sighting of gold and tea only
        self.assertEqual(self.events(), [(2, 2), (0, 2), (0, 2)])     # but three update events

    def test_only_the_changed_commodity_gets_a_row(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(5)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 40, 0, 0), ("tea", 5, 5, 0, 0)], when=at(4)))
        self.assertEqual(self.count(PriceHistory), 3)
        d = queries.price_history(self.s, "Hub", "gold", max_points=0)
        self.assertEqual([p["stock"] for p in d["series"]], [50, 40])
        self.assertEqual((d["updates"], d["rows"], d["updates_source"]), (2, 2, "update log"))
        self.assertEqual(queries.price_history(self.s, "Hub", "tea", max_points=0)["rows"], 1)

    def test_a_new_commodity_gets_a_row(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(5)))
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(4)))
        self.assertEqual(self.count(PriceHistory), 2)

    def test_mean_price_or_bracket_change_counts_as_a_change(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(5)))
        m = commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(4))
        m["message"]["commodities"][0]["meanPrice"] = 1234
        self.feed(m)
        self.assertEqual(self.count(PriceHistory), 2)

    def test_an_anchor_row_is_written_when_nothing_was_logged_for_a_day(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(30)))
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(1)))   # unchanged, but 29 h since the last row
        self.assertEqual(self.count(PriceHistory), 4)
        self.assertEqual(self.events(), [(2, 2), (2, 2)])
        with mock.patch.dict(os.environ, {store.ANCHOR_HOURS_ENV: "0"}):   # anchors can be switched off
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(0.5)))
        self.assertEqual(self.count(PriceHistory), 4)

    def test_snapshot_logged_at_only_moves_when_a_row_is_written(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(5)))
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(4)))
        logged = self.s.execute(select(CommodityPrice.logged_at)).scalar_one()
        self.assertEqual(logged.strftime("%Y-%m-%dT%H:%M:%SZ"), at(5))

    def test_partial_messages_do_not_flip_flop_the_history(self):
        """EDDN reports can be partial: the same station arrives with 368 items from most uploaders and 4 from one."""
        full = [(f"item{i:03d}", 10 + i, 5, 0, 0) for i in range(40)]
        for h, items in ((6, full), (5, full[:4]), (4, full), (3, full[:4]), (2, full)):
            self.feed(commodity_msg(7, "Hub", "Sol", items, when=at(h)))
        self.assertEqual(self.count(PriceHistory), 40)                  # only the first sighting of each item
        self.assertEqual([e[0] for e in self.events()], [40, 0, 0, 0, 0])
        snap = queries.station_market(self.s, "Hub")["commodities"]
        self.assertEqual((len(snap), sum(c["stale"] for c in snap)), (40, 0))
        self.assertEqual(self.count(CommodityPrice), 40)

    def test_a_late_message_only_adds_to_the_update_log(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(1)))
        self.assertTrue(self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 1, 1, 0, 0)], when=at(5))))
        self.assertEqual(self.count(PriceHistory), 2)
        self.assertEqual(self.count(MarketUpdate), 2)
        self.assertEqual(queries.station_market(self.s, "Hub")["commodities"][0]["buy_price"], 100)

    def test_uploader_and_software_are_recorded_and_duplicates_ignored(self):
        self.feed(hdr(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(3)), "cmdr-a", "EDMC"))
        self.feed(hdr(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(3)), "cmdr-a", "EDMC"))   # same second, same uploader
        self.feed(hdr(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(3)), "cmdr-b", "EDDiscovery"))
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(2)))                          # no header at all
        rows = self.s.execute(select(MarketUpdate.uploader, MarketUpdate.software)
                              .order_by(MarketUpdate.ts, MarketUpdate.uploader)).all()
        self.assertEqual([tuple(r) for r in rows], [("cmdr-a", "EDMC"), ("cmdr-b", "EDDiscovery"), ("", "")])


class FactionChangeLogTests(ChangesCase):
    def n_rows(self, **where):
        return self.count(FactionHistory, **where)

    def test_repeats_log_no_faction_rows_but_every_update_is_logged(self):
        for h in (5, 4, 3):
            self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(h)))
        self.assertEqual(self.n_rows(), 2)
        self.assertEqual([(r.factions, r.changed) for r in self.s.execute(select(SystemUpdate).order_by(SystemUpdate.ts)).scalars()],
                         [(2, 2), (2, 0), (2, 0)])

    def test_any_tracked_change_is_logged(self):
        self.feed(sys_msg([fac("A", 0.6000)], at(9)))
        self.feed(sys_msg([fac("A", 0.6002)], at(8)))                                   # below the influence epsilon
        self.assertEqual(self.n_rows(), 1)
        self.feed(sys_msg([fac("A", 0.6002, band=4)], at(7)))                           # happiness
        self.feed(sys_msg([fac("A", 0.6002, band=4, pending=[("War", 0)])], at(6)))     # pending state appears
        self.feed(sys_msg([fac("A", 0.6002, band=4, pending=[("War", 1)])], at(5)))     # only its trend changes
        self.feed(sys_msg([fac("A", 0.6502, band=4, pending=[("War", 1)])], at(4)))     # influence
        self.assertEqual(self.n_rows(), 5)

    def test_left_marker_and_new_faction(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(6)))
        self.feed(sys_msg([fac("A", 0.6)], at(5)))
        self.feed(sys_msg([fac("A", 0.6), fac("C", 0.1)], at(4)))
        self.assertEqual(self.n_rows(faction="B", present=False), 1)
        self.assertEqual(self.n_rows(faction="C"), 1)
        self.assertEqual(self.n_rows(faction="A"), 1)

    def test_anchor_row_after_a_day_and_late_message_is_event_only(self):
        self.feed(sys_msg([fac("A", 0.6)], at(30)))
        self.feed(sys_msg([fac("A", 0.6)], at(1)))
        self.assertEqual(self.n_rows(), 2)
        self.assertTrue(self.feed(sys_msg([fac("A", 0.1)], at(10))))                      # late
        self.assertEqual(self.n_rows(), 2)
        self.assertEqual(self.count(SystemUpdate), 3)
        self.assertEqual(self.s.execute(select(Faction.influence)).scalar_one(), 0.6)

    def test_timeline_last_seen_comes_from_the_update_log(self):
        for h in (10, 8, 6):
            self.feed(sys_msg([fac("A", 0.5, pending=[("Expansion", 0)])], at(h)))
        self.feed(sys_msg([fac("A", 0.5, state="Expansion", active=["Expansion"])], at(4)))
        self.assertEqual(self.n_rows(), 2)                                               # stored rows: first + the change
        t = queries.faction_timeline(self.s, "Sol", "A")
        pend = [iv for iv in t["intervals"] if (iv["field"], iv["value"]) == ("pending", "Expansion")][0]
        self.assertEqual((pend["start"], pend["last_seen"], pend["ended_at"]), (at(10), at(6), at(4)))
        self.assertEqual((t["observations"], t["rows"]), (4, 2))
        h = queries.faction_history(self.s, "Sol", 7)
        self.assertEqual((h["observations"], h["rows"], h["updates_source"]), (4, 2, "update log"))


class TrafficQueryTests(ChangesCase):
    def test_station_traffic_buckets_uploaders_and_rates(self):
        for h, who in ((5.0, "a"), (4.9, "b"), (4.8, "a"), (2.0, "c")):
            self.feed(hdr(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(h)), who, "EDMC"))
        t = queries.station_traffic(self.s, "hub", days=1, bucket_minutes=60)
        self.assertEqual((t["updates"], t["distinct_uploaders"], t["blank_uploader_updates"]), (4, 3, 0))
        self.assertEqual(sum(b["updates"] for b in t["series"]), 4)
        self.assertEqual(max(b["distinct_uploaders"] for b in t["series"]), 2)
        self.assertEqual(t["busiest_bucket"]["updates"], max(b["updates"] for b in t["series"]))
        self.assertEqual(t["software"], [{"software": "EDMC", "updates": 4}])
        self.assertGreater(t["updates_per_hour"], 0)
        zero_buckets = [b for b in t["series"] if b["updates"] == 0]
        self.assertTrue(zero_buckets)                                                    # quiet hours are filled with zeros

    def test_busiest_stations_and_systems(self):
        for h in (5, 4, 3):
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(h)))
        self.feed(commodity_msg(8, "ABC-123", "Sol", GOLD_TEA, when=at(2)))                # a fleet carrier (callsign name)
        for h in (5, 4):
            self.feed(sys_msg([fac("A", 0.5)], at(h), id64=1, name="Sol"))
        self.feed(sys_msg([fac("A", 0.5)], at(3), id64=2, name="Lave"))
        b = queries.busiest_stations(self.s, days=1)
        self.assertEqual([(r["station"], r["updates"], r["is_carrier"]) for r in b["stations"]],
                         [("Hub", 3, False), ("ABC-123", 1, True)])
        self.assertEqual(b["total_updates"], 4)
        self.assertEqual(b["stations"][0]["share_of_all_updates_pct"], 75.0)
        self.assertEqual([r["station"] for r in queries.busiest_stations(self.s, days=1, carriers=False)["stations"]],
                         ["Hub"])
        sy = queries.busiest_systems(self.s, days=1)
        self.assertEqual([(r["system"], r["updates"]) for r in sy["systems"]], [("Sol", 2), ("Lave", 1)])
        st = queries.system_traffic(self.s, "Sol", days=1)
        self.assertEqual(st["updates"], 2)

    def test_station_activity_counts_the_update_log(self):
        for h in (30, 29, 28):
            self.feed(commodity_msg(7, "N0B-T4X", "Sol", GOLD_TEA, when=at(h)))
        a = queries.station_activity(self.s, "n0b-t4x")
        self.assertEqual((a["updates"], a["price_rows"], a["updates_source"], a["history_mode"]),
                         (3, 2, "update log", "changes"))
        self.assertEqual(sum(d["updates"] for d in a["per_day"]), 3)

    def test_traffic_csv_columns(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(2)))
        cols, rows = cli.traffic_csv(queries.station_traffic(self.s, "Hub", days=1))
        self.assertEqual(cols, cli.TRAFFIC_COLUMNS)
        self.assertEqual(sum(r["updates"] for r in rows), 1)


class StepSeriesQueryTests(ChangesCase):
    def test_state_at_start_comes_from_the_row_before_the_window(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 50, 0, 0)], when=at(25.5)))  # just before a 1-day window
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 50, 0, 0)], when=at(3)))     # unchanged update inside it
        d = queries.price_history(self.s, "Hub", "gold", days=1, max_points=0)
        self.assertEqual((d["rows"], d["updates"]), (0, 1))
        self.assertEqual((d["state_at_start"]["buy_price"], d["state_at_start"]["stock"]), (100, 50))
        self.assertEqual(d["buy_price_stats"]["avg"], 100)                                  # stats include the carried state

    def test_resample_carries_the_last_state_forward_and_counts_updates(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 50, 0, 0)], when=at(8)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 50, 0, 0)], when=at(7)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 200, 50, 0, 0)], when=at(5)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 200, 50, 0, 0)], when=at(4)))
        d = queries.price_history(self.s, "Hub", "gold", days=1, resample_minutes=60)
        prices = [p["buy_price"] for p in d["series"]]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual({100, 200}, set(prices))
        self.assertTrue(any(p["carried"] for p in d["series"]))
        self.assertEqual(sum(p["updates"] for p in d["series"]), 4)
        self.assertEqual(d["resample_minutes"], 60)

    def test_buckets_report_true_updates_and_stored_changes_separately(self):
        for h, gold in ((8, 100), (7.5, 100), (7.2, 100), (7.0, 120)):
            self.feed(commodity_msg(7, "Hub", "Sol", [("gold", gold, 50, 0, 0)], when=at(h)))
        d = queries.price_history(self.s, "Hub", "gold", days=1, bucket_minutes=24 * 60)
        self.assertEqual(sum(b["updates"] for b in d["series"]), 4)
        self.assertEqual(sum(b["changes"] for b in d["series"]), 2)
        b = d["series"][0] if len(d["series"]) == 1 else max(d["series"], key=lambda x: x["changes"])
        self.assertEqual((b["buy_min"], b["buy_max"]), (100, 120))

    def test_a_state_older_than_the_anchor_horizon_is_marked_stale_not_carried_forever(self):
        self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(70)))                    # tea last reported 70 h ago
        for h in (60, 40, 20, 4):                                                           # gold keeps arriving (and anchoring)
            self.feed(commodity_msg(7, "Hub", "Sol", GOLD_TEA[:1], when=at(h)))
        d = queries.price_history(self.s, "Hub", "tea", days=3, resample_minutes=360)
        self.assertEqual(d["series"][0]["buy_price"], 5)
        self.assertFalse(d["series"][0]["stale"])
        self.assertTrue(d["series"][-1]["stale"])
        self.assertIsNone(d["series"][-1]["buy_price"])
        self.assertFalse(d["series"][-1]["listed"])
        gold = queries.price_history(self.s, "Hub", "gold", days=3, resample_minutes=360)
        self.assertFalse(any(p["stale"] for p in gold["series"]))                          # anchored daily, never stale

    def test_find_commodity_uses_per_commodity_freshness(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 0, 0, 900, 50), ("tea", 0, 0, 80, 50)], when=at(100)))
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 0, 0, 950, 50)], when=at(1)))   # a later, partial report
        fresh = queries.find_commodity(self.s, "gold", "sell", max_age_hours=48)
        self.assertEqual([r["price"] for r in fresh["results"]], [950])
        self.assertEqual(queries.find_commodity(self.s, "tea", "sell", max_age_hours=48)["results"], [])    # not seen for 100 h
        self.assertEqual(len(queries.find_commodity(self.s, "tea", "sell", max_age_hours=200)["results"]), 1)

    def test_csv_for_resampled_series(self):
        self.feed(commodity_msg(7, "Hub", "Sol", [("gold", 100, 50, 0, 0)], when=at(3)))
        cols, rows = cli.price_csv(queries.price_history(self.s, "Hub", "gold", days=1, resample_minutes=60))
        self.assertEqual(cols, cli.PRICE_RESAMPLED)
        self.assertTrue(rows)


class ParserHeaderTests(unittest.TestCase):
    def test_uploader_and_software_are_parsed_and_trimmed(self):
        m = hdr(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(1)), "x" * 100, "S" * 80)
        p = parsers.parse_message(m)
        self.assertEqual((len(p["uploader"]), len(p["software"])), (64, 40))
        p = parsers.parse_message(commodity_msg(7, "Hub", "Sol", GOLD_TEA, when=at(1)))
        self.assertEqual((p["uploader"], p["software"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
