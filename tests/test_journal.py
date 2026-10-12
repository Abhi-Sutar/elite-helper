"""Journal layer: commander state replay, Status.json, credit-rate analysis, CLI and MCP wiring (synthetic journals)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from eddn_helper import cli, mcp_server
from eddn_helper.journal import context, income, state, status
from eddn_helper.journal import files as jfiles

T0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)


def ev(minutes, name, **fields):
    ts = (T0 + timedelta(minutes=minutes)).strftime(jfiles.TS_FORMAT)
    return {"timestamp": ts, "event": name, **fields}


def write_journal(directory, name, events, trailing=""):
    path = Path(directory) / f"Journal.{name}.log"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n" + trailing, encoding="utf-8")
    return str(path)


SESSION = [
    ev(0, "Fileheader"),
    ev(0, "Commander", Name="CMDR Test", FID="F1"),
    ev(1, "LoadGame", Commander="CMDR Test", Ship="python", Ship_Localised="Python", ShipName="Snake", ShipIdent="TS-01",
       ShipID=3, Credits=1000, GameMode="Solo"),
    ev(2, "Location", StarSystem="Sol", SystemAddress=10477373803, StarPos=[0, 0, 0], Body="Earth", BodyType="Planet",
       Docked=False, SystemEconomy_Localised="High Tech", Population=22780919531),
    ev(3, "FSDJump", StarSystem="Alpha Centauri", SystemAddress=5, StarPos=[3.03, -0.09, 3.16], Body="Alpha Centauri A",
       BodyType="Star"),
    ev(4, "SupercruiseExit", StarSystem="Alpha Centauri", Body="Hutton Orbital", BodyType="Station"),
    ev(5, "Docked", StationName="Hutton Orbital", StationType="Coriolis", MarketID=99, StarSystem="Alpha Centauri",
       SystemAddress=5, StationFaction={"Name": "Alliance Ring"}, StationEconomy_Localised="Refinery"),
    ev(6, "MarketBuy", Type="gold", Count=10, TotalCost=1000),
    ev(7, "Undocked", StationName="Hutton Orbital"),
]


class StateTests(unittest.TestCase):
    def replay(self, events):
        return state.replay([(jfiles.parse_timestamp(e["timestamp"]), e) for e in events])

    def test_replay_follows_the_commander_through_a_session(self):
        s = self.replay(SESSION[:3])
        self.assertTrue(s.game_running)
        self.assertEqual((s.commander, s.credits, s.ship["type"], s.ship["name"]), ("CMDR Test", 1000, "Python", "Snake"))
        s = self.replay(SESSION[:4])
        self.assertEqual((s.system, s.body, s.docked), ("Sol", "Earth", False))
        self.assertEqual(s.system_info["economy"], "High Tech")
        s = self.replay(SESSION[:5])
        self.assertEqual((s.system, s.star_pos, s.supercruise), ("Alpha Centauri", [3.03, -0.09, 3.16], True))
        self.assertIsNone(s.station)
        s = self.replay(SESSION[:7])
        self.assertTrue(s.docked)
        self.assertFalse(s.supercruise)
        self.assertEqual((s.station["name"], s.station["type"], s.station["faction"]),
                         ("Hutton Orbital", "Coriolis", "Alliance Ring"))
        s = self.replay(SESSION)
        self.assertFalse(s.docked)
        self.assertEqual(s.last_event, "Undocked")

    def test_shutdown_stops_the_game(self):
        s = self.replay(SESSION + [ev(10, "Shutdown")])
        self.assertFalse(s.game_running)
        self.assertEqual(s.system, "Alpha Centauri")   # last known position survives

    def test_carrier_jump_counts_as_docked_location(self):
        s = self.replay([ev(0, "CarrierJump", StarSystem="Colonia", SystemAddress=7, StarPos=[1, 2, 3],
                            StationName="N0B-T4X", StationType="FleetCarrier", MarketID=5)])
        self.assertTrue(s.docked)
        self.assertEqual((s.system, s.station["name"]), ("Colonia", "N0B-T4X"))

    def test_new_event_handlers_are_registered_with_a_decorator(self):
        seen = []

        @state.handles("TestOnlyEvent")
        def handler(st, e, dt):
            seen.append(e["Value"])
        try:
            self.replay([ev(0, "TestOnlyEvent", Value=7)])
        finally:
            state.HANDLERS.pop("TestOnlyEvent")
        self.assertEqual(seen, [7])

    def test_current_state_falls_back_to_the_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
            write_journal(d, "2026-10-11T120000.01", [ev(120, "Fileheader"), ev(120, "Commander", Name="CMDR Test")])
            st, path = state.current_state(d)
            self.assertTrue(path.endswith("120000.01.log"))
            self.assertEqual(st.system, "Alpha Centauri")
            self.assertEqual(st.commander, "CMDR Test")

    def test_partial_last_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_journal(d, "2026-10-11T100000.01", SESSION, trailing='{"timestamp":"2026-10-11T10:30:0')
            self.assertEqual(len(jfiles.read_events(path)), len(SESSION))


class WhereAmITests(unittest.TestCase):
    def test_reports_game_not_running_after_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
            out = context.where_am_i(d)
        self.assertEqual((out["system"], out["docked"], out["station"]["name"]), ("Alpha Centauri", False, "Hutton Orbital"))
        self.assertFalse(out["game_running"])
        self.assertIn("last known", out["note"])
        self.assertNotIn("status", out)

    def test_silent_journal_without_shutdown_is_treated_as_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION)   # events are from 2026-10-11, long ago
            out = context.where_am_i(d)
        self.assertFalse(out["game_running"])
        self.assertIn("crashed", out["note"])

    def test_missing_journals_give_an_error_with_a_hint(self):
        with tempfile.TemporaryDirectory() as d:
            out = context.where_am_i(d)
        self.assertIn("error", out)
        self.assertIn(jfiles.ENV_JOURNAL_DIR, out["error"])

    def test_format_where_is_readable(self):
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION[:7])
            text = "\n".join(context.format_where(context.where_am_i(d)))
        self.assertIn("docked at Hutton Orbital (Coriolis)", text)
        self.assertIn("Alpha Centauri", text)

    def test_enrich_joins_the_eddn_database(self):
        from helpers import new_session
        from eddn_helper import parsers, store
        session = new_session()
        store.apply_parsed(session, parsers.parse_message({
            "$schemaRef": "https://eddn.edcd.io/schemas/journal/1",
            "header": {"uploaderID": "u", "softwareName": "t", "softwareVersion": "1", "gatewayTimestamp": "2026-10-11T10:00:00Z"},
            "message": {"event": "FSDJump", "timestamp": "2026-10-11T10:00:00Z", "StarSystem": "Alpha Centauri",
                        "SystemAddress": 5, "StarPos": [3.03, -0.09, 3.16], "Population": 100,
                        "SystemEconomy": "$economy_Refinery;", "Factions": []}}))
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION[:7])
            out = context.enrich(session, context.where_am_i(d))
        self.assertTrue(out["eddn_system"]["found"])
        self.assertFalse(out["eddn_market"]["found"])


class StatusTests(unittest.TestCase):
    def test_flags_decode(self):
        self.assertEqual(status.decode_flags(0), [])
        self.assertEqual(status.decode_flags(1 | 1 << 4 | 1 << 24), ["docked", "supercruise", "in_main_ship"])

    def test_read_status(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "Status.json").write_text(json.dumps({
                "timestamp": "2026-10-11T11:03:08Z", "event": "Status", "Flags": 1 | 1 << 24, "Balance": 5,
                "Fuel": {"FuelMain": 12.5, "FuelReservoir": 0.6}, "Destination": {"System": 1, "Body": 2, "Name": "X"}}))
            out = status.read_status(d)
            self.assertEqual(out["flags"], ["docked", "in_main_ship"])
            self.assertEqual((out["balance"], out["fuel_main"], out["destination"]["name"]), (5, 12.5, "X"))
            self.assertIsNone(status.read_status(tempfile.gettempdir() + "/definitely-missing"))


CARRIER_SESSION_A = [
    ev(0, "Fileheader"),
    ev(1, "CarrierStats", CarrierID=1, Finance={"CarrierBalance": 1000}),
    ev(2, "CarrierBankTransfer", CarrierID=1, Deposit=500, Withdraw=0),
    ev(3, "CarrierBuy", Price=7000),
]
CARRIER_SESSION_B = [
    ev(600, "Fileheader"),
    ev(601, "CarrierFinance", CarrierID=1, CarrierBalance=2200),   # +1200, of which 500 was our own deposit
    ev(602, "CarrierDecommission", Refund=300),
]


class CreditRateTests(unittest.TestCase):
    def run_rate(self, sessions, **kw):
        with tempfile.TemporaryDirectory() as d:
            paths = [write_journal(d, f"2026-10-1{i}T000000.01", evs) for i, evs in enumerate(sessions)]
            return income.credit_rate(paths, **kw)

    def test_all_streams_and_totals(self):
        events = [
            ev(0, "Fileheader"),
            ev(1, "MarketBuy", Type="gold", Type_Localised="Gold", Count=10, TotalCost=1000),
            ev(2, "MarketSell", Type="gold", Type_Localised="Gold", Count=10, TotalSale=5000),
            ev(3, "RedeemVoucher", Type="bounty", Amount=700),
            ev(3, "RedeemVoucher", Type="scannable", Amount=999),
            ev(4, "MissionCompleted", Reward=2000),
            ev(5, "MultiSellExplorationData", TotalEarningsBonus=400),
            ev(6, "RepairAll", Cost=50), ev(6, "BuyDrones", TotalCost=30), ev(7, "Resurrected", Cost=100),
            ev(8, "NpcCrewPaidWage", NpcCrewName="Bob", Amount=25),
            ev(60, "Shutdown"),
        ]
        out = self.run_rate([events], crew_wages=True)
        s = out["streams"]
        self.assertEqual((s["trading"]["earned"], s["trading"]["spent"]), (5000, 1000))
        self.assertEqual(s["bounty"]["earned"], 700)
        self.assertEqual(s["mission"]["earned"], 2000)
        self.assertEqual(s["exploration"]["earned"], 400)
        self.assertEqual((s["maintenance"]["spent"], s["maintenance"]["count"]), (80, 2))
        self.assertEqual(out["maintenance"]["limpets"]["spent"], 30)
        self.assertEqual(s["rebuy"]["spent"], 100)
        self.assertEqual(out["crew_wages"], {"Bob": {"spent": 25, "count": 1}})
        self.assertEqual(out["per_commodity"]["Gold"]["profit"], 4000)
        self.assertEqual(out["totals"], {"earned": 8100, "spent": 1205, "net": 6895})
        self.assertEqual(out["playtime_hours"], 1.0)
        self.assertEqual(out["credits_per_hour"], 6895)

    def test_playtime_is_stitched_not_wall_clock(self):
        a = [ev(0, "Fileheader"), ev(60, "Shutdown")]
        b = [ev(60 * 48, "Fileheader"), ev(60 * 48 + 30, "Shutdown")]   # two days later
        self.assertEqual(self.run_rate([a, b])["playtime_hours"], 1.5)

    def test_max_gap_excludes_afk_time(self):
        events = [ev(0, "Fileheader"), ev(10, "Music"), ev(130, "Music"), ev(140, "Shutdown")]
        self.assertEqual(self.run_rate([events])["playtime_hours"], round(140 / 60, 3))
        self.assertEqual(self.run_rate([events], max_gap_minutes=30)["playtime_hours"], round(20 / 60, 3))

    def test_carrier_streams_and_market_diff_across_sessions(self):
        out = self.run_rate([CARRIER_SESSION_A, CARRIER_SESSION_B])
        s = out["streams"]
        self.assertEqual((s["carrier"]["spent"], s["carrier"]["earned"]), (7500, 300))   # buy + deposit, refund
        self.assertEqual((s["carrier_market"]["earned"], s["carrier_market"]["spent"]), (700, 0))   # 1200 - 500 deposit

    def test_stream_filter_and_unknown_stream(self):
        events = [ev(0, "Fileheader"), ev(1, "MissionCompleted", Reward=10), ev(2, "RepairAll", Cost=5), ev(60, "Shutdown")]
        out = self.run_rate([events], streams="mission")
        self.assertEqual(list(out["streams"]), ["mission"])
        self.assertEqual(out["totals"]["net"], 10)
        self.assertIn("error", self.run_rate([events], streams="bogus"))

    def test_since_and_unusable_input(self):
        events = [ev(0, "Fileheader"), ev(1, "MissionCompleted", Reward=10), ev(60, "Shutdown")]
        out = self.run_rate([events], since=(T0 + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(out["sessions_used"], 0)
        self.assertIn("note", out)
        self.assertEqual(income.format_report(out), [out["note"]])

    def test_report_text_matches_the_original_layout(self):
        events = [ev(0, "Fileheader"), ev(1, "MarketSell", Type="gold", Count=1, TotalSale=3600000), ev(60, "Shutdown")]
        text = "\n".join(income.format_report(self.run_rate([events], max_gap_minutes=90)))
        self.assertIn("Total active playtime: 1.000 hours  (stitched across sessions, gaps > 90 min excluded)", text)
        self.assertIn("Credits/hour:  3,600,000 Cr", text)
        self.assertIn("Per-commodity trading breakdown:", text)


class CliAndMcpTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        args = cli.build_parser().parse_args(argv)
        self.assertTrue(args.raw)   # journal commands must not need the database
        with contextlib.redirect_stdout(out):
            rc = args.func(args, None)
        return rc, out.getvalue()

    def test_cli_where_and_credit_rate_json(self):
        with tempfile.TemporaryDirectory() as d:
            write_journal(d, "2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
            rc, out = self.run_cli(["where", "--journal-dir", d, "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["system"], "Alpha Centauri")
            rc, out = self.run_cli(["credit-rate", "--journal-dir", d, "--session", "--json"])
            self.assertEqual(json.loads(out)["streams"]["trading"]["spent"], 1000)
            rc, out = self.run_cli(["credit-rate", "--journal-dir", d])
            self.assertIn("Income by stream:", out)

    def test_cli_reports_missing_journals(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_cli(["where", "--journal-dir", d, "--json"])
            self.assertEqual(rc, 1)
            self.assertIn("error", json.loads(out))

    def test_mcp_tools_read_the_env_directory_without_a_database(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {jfiles.ENV_JOURNAL_DIR: d}):
            write_journal(d, "2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
            self.assertEqual(mcp_server.where_am_i()["system"], "Alpha Centauri")
            with mock.patch.object(mcp_server, "_run", return_value={"error": "no database"}):   # files only
                rate = mcp_server.credit_rate(session_only=True, streams=["trading"], include_crew=True)
            self.assertEqual(rate["source"], "files")
            self.assertEqual(rate["streams"]["trading"]["spent"], 1000)
            self.assertIn("crew_wages", rate)
            self.assertIn("error", mcp_server.credit_rate(streams=["nope"], source="files"))

    def test_mcp_enrich_failure_keeps_the_journal_answer(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {jfiles.ENV_JOURNAL_DIR: d}):
            write_journal(d, "2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
            with mock.patch.object(mcp_server, "_run", return_value={"error": "database is down"}):
                out = mcp_server.where_am_i(enrich=True)
        self.assertEqual(out["system"], "Alpha Centauri")
        self.assertEqual(out["enrich_error"], "database is down")


if __name__ == "__main__":
    unittest.main()
