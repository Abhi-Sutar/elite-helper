"""Journal history queries and current-state views over synthetic journals ingested into a test database."""
import json
import os
import unittest
from unittest import mock

from eddn_helper import mcp_server
from eddn_helper.journal import files as jfiles
from eddn_helper.journal import income, ingest, queries, views
from test_journal import SESSION, T0, ev
from test_journal_db import DbTest

FID = "F1"


def stamp(minutes):
    return ev(minutes, "x")["timestamp"]


# A fuller session: jump, dock, trade, missions, snapshots, deaths. Minutes are offsets from T0 (2026-10-11 10:00Z).
RICH = [
    ev(0, "Fileheader"),
    ev(0, "Commander", Name="CMDR Test", FID=FID),
    ev(1, "LoadGame", Commander="CMDR Test", Ship="python", Ship_Localised="Python", ShipName="Snake", ShipIdent="TS-01",
       ShipID=3, Credits=1000, GameMode="Solo"),
    ev(1, "Rank", Combat=4, Trade=13, Explore=5, Soldier=0, Exobiologist=3, CQC=0, Federation=12, Empire=12),
    ev(1, "Progress", Combat=15, Trade=100, Explore=56, Soldier=0, Exobiologist=0, CQC=1, Federation=4, Empire=6),
    ev(1, "Reputation", Empire=75.0, Federation=50.5, Independent=0.0, Alliance=1.5),
    ev(1, "Statistics", Bank_Account={"Current_Wealth": 5000}, Combat={"Bounties_Claimed": 7}),
    ev(1, "Cargo", Vessel="Ship", Count=10, Inventory=[{"Name": "gold", "Count": 10}]),
    ev(1, "Materials", Raw=[{"Name": "iron", "Count": 100}], Manufactured=[{"Name": "heatdispersionplate", "Count": 20}],
       Encoded=[{"Name": "shielddensityreports", "Name_Localised": "Untypical Shield Scans", "Count": 30}]),
    ev(1, "Loadout", Ship="python", ShipID=3, ShipName="Snake", ShipIdent="TS-01", HullValue=50000, ModulesValue=900000,
       Rebuy=47000, CargoCapacity=64, MaxJumpRange=18.5, UnladenMass=300.0,
       Modules=[{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size5_class5", "On": True, "Priority": 0,
                 "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 1.0}}]),
    ev(1, "StoredShips", StationName="Hutton Orbital", StarSystem="Alpha Centauri", MarketID=99,
       ShipsHere=[{"ShipID": 4, "ShipType": "sidewinder", "ShipType_Localised": "Sidewinder", "Value": 5000, "Hot": False}],
       ShipsRemote=[{"ShipID": 5, "ShipType": "cobramkiii", "Name": "Cobra", "StarSystem": "Sol", "Value": 90000,
                     "TransferPrice": 1200, "TransferTime": 300}]),
    ev(2, "Location", StarSystem="Sol", SystemAddress=10477373803, StarPos=[0, 0, 0], Body="Earth", BodyType="Planet", Docked=False),
    ev(3, "FSDJump", StarSystem="Alpha Centauri", SystemAddress=5, StarPos=[3.03, -0.09, 3.16], JumpDist=4.38, FuelUsed=0.5,
       FuelLevel=30.0, Body="Alpha Centauri A", BodyType="Star"),
    ev(5, "Docked", StationName="Hutton Orbital", StationType="Coriolis", MarketID=99, StarSystem="Alpha Centauri", SystemAddress=5),
    ev(6, "MarketBuy", Type="silver", Type_Localised="Silver", Count=5, TotalCost=500),
    ev(7, "MarketSell", Type="gold", Count=4, TotalSale=4000),
    ev(8, "MissionAccepted", MissionID=1, LocalisedName="Deliver goods", Name="Mission_Delivery", Faction="Alliance Ring",
       Expiry=stamp(60 * 24 * 400), Reward=10000, DestinationSystem="Sol"),
    ev(8, "MissionAccepted", MissionID=2, LocalisedName="Old job", Name="Mission_Old", Faction="Alliance Ring",
       Expiry=stamp(-60 * 24), Reward=500),
    ev(9, "MissionAccepted", MissionID=3, LocalisedName="Doomed job", Name="Mission_Doomed", Faction="X", Expiry=stamp(60 * 24 * 400)),
    ev(10, "MissionCompleted", MissionID=3, LocalisedName="Doomed job", Faction="X", Reward=2500),
    ev(11, "MaterialCollected", Category="Raw", Name="iron", Count=5),
    ev(11, "MaterialDiscarded", Category="Manufactured", Name="heatdispersionplate", Count=4),
    ev(12, "EjectCargo", Type="gold", Count=1),
    ev(12, "MiningRefined", Type="$platinum_name;", Type_Localised="Platinum"),
    ev(13, "RepairAll", Cost=100),
    ev(14, "CarrierStats", CarrierID=1, Callsign="N0B-T4X", Name="LAST BREATH", FuelLevel=700,
       Finance={"CarrierBalance": 1000}, SpaceUsage={"TotalCapacity": 25000, "FreeSpace": 20000}),
    ev(15, "Undocked", StationName="Hutton Orbital"),
]


class QueryTests(DbTest):
    def setUp(self):
        super().setUp()
        self.journal("2026-10-11T100000.01", RICH)
        self.q = dict(directory=str(self.jdir))

    def test_search_filters_and_summaries(self):
        r = queries.journal_events(self.session, event="docked,FSDJump", **self.q)
        self.assertEqual((r["matched"], [e["event"] for e in r["events"]]), (2, ["Docked", "FSDJump"]))   # newest first
        self.assertEqual(r["events"][0]["summary"], "docked at Hutton Orbital")
        self.assertEqual(r["events"][1]["summary"], "to Alpha Centauri (4.4 ly)")
        self.assertEqual(r["journal_freshness"]["events_ingested_now"], len(RICH))
        r = queries.journal_events(self.session, station="hutton orbital", event="MarketSell", **self.q)
        self.assertEqual(r["events"][0]["summary"], "sold 4 gold for 4,000 Cr")
        self.assertEqual(queries.journal_events(self.session, text="PLATINUM", **self.q)["matched"], 1)
        r = queries.journal_events(self.session, since=stamp(9), until=stamp(11), oldest_first=True, **self.q)
        self.assertEqual([e["event"] for e in r["events"]][:2], ["MissionAccepted", "MissionCompleted"])
        self.assertNotIn("data", r["events"][0])
        self.assertEqual(queries.journal_events(self.session, event="Docked", include_data=True, **self.q)
                         ["events"][0]["data"]["MarketID"], 99)

    def test_unknown_commander_and_bad_time(self):
        self.assertFalse(queries.journal_events(self.session, commander="nobody", **self.q)["found"])
        self.assertIn("cannot read time", queries.journal_events(self.session, since="yesterday-ish", **self.q)["error"])

    def test_travel_visited_sessions(self):
        r = queries.travel_log(self.session, days=None, **self.q)
        self.assertEqual((r["jumps"], r["total_ly"], r["distinct_systems"]), (1, 4.4, 1))
        self.assertEqual(r["log"][0]["system"], "Alpha Centauri")
        v = queries.visited_systems(self.session, fresh=False)
        self.assertEqual([(s["system"], s["visits"]) for s in v["systems"]], [("Alpha Centauri", 1)])
        st = queries.visited_stations(self.session, fresh=False)
        self.assertEqual([(s["station"], s["system"], s["dockings"]) for s in st["stations"]],
                         [("Hutton Orbital", "Alpha Centauri", 1)])
        se = queries.sessions(self.session, fresh=False)
        self.assertEqual((len(se["sessions"]), se["sessions"][0]["events"], se["sessions"][0]["hours"]),
                         (1, len(RICH), 0.25))

    def test_ledger_categories_and_running_total(self):
        r = queries.ledger(self.session, days=None, since="2026-10-11", **self.q)
        cats = r["by_category"]
        self.assertEqual((cats["trading"]["earned"], cats["trading"]["spent"]), (4000, 500))
        self.assertEqual(cats["mission"]["earned"], 2500)
        self.assertEqual(cats["maintenance"]["spent"], 100)
        self.assertEqual(r["net"], 4000 - 500 + 2500 - 100)
        self.assertEqual(r["entries"][0]["running"], r["net"])                 # newest entry carries the total
        only = queries.ledger(self.session, days=None, since="2026-10-11", category="mission", fresh=False)
        self.assertEqual([e["category"] for e in only["entries"]], ["mission"])

    def test_db_credit_rate_equals_file_credit_rate(self):
        files = jfiles.find_journal_files(str(self.jdir))
        for kw in ({}, {"max_gap_minutes": 3}, {"streams": "trading,mission"}, {"crew_wages": True}):
            db = queries.credit_rate(self.session, **kw, **self.q)
            for k in ("journal_freshness", "commander", "source"):
                db.pop(k)
            self.assertEqual(db, income.credit_rate(files, None if "streams" not in kw else kw["streams"], None,
                                                    kw.get("max_gap_minutes"), kw.get("crew_wages", False)))

    def test_fresh_false_never_ingests(self):
        self.assertEqual(queries.journal_events(self.session, fresh=False, directory=str(self.jdir)).get("error"),
                         "no commander in the database - are there journal files? (journal-status)")


class ViewTests(DbTest):
    def setUp(self):
        super().setUp()
        self.journal("2026-10-11T100000.01", RICH)
        self.q = dict(directory=str(self.jdir))

    def test_cargo_replays_trades_from_the_snapshot(self):
        r = views.cargo(self.session, **self.q)
        items = {i["name"]: i["count"] for i in r["items"]}
        # 10 gold -4 sold -1 ejected = 5; +5 silver bought; +1 platinum mined ("$platinum_name;" normalised)
        self.assertEqual(items, {"gold": 5, "silver": 5, "platinum": 1})
        self.assertEqual((r["total"], r["capacity"]), (11, 64))

    def test_cargo_clears_on_death_and_follows_carrier_transfers(self):
        with open(self.jdir / "Journal.2026-10-11T100000.01.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(20, "CargoTransfer", Transfers=[{"Type": "gold", "Count": 2, "Direction": "tocarrier"},
                                                                   {"Type": "tin", "Count": 3, "Direction": "toship"}])) + "\n")
        items = {i["name"]: i["count"] for i in views.cargo(self.session, **self.q)["items"]}
        self.assertEqual(items, {"gold": 3, "silver": 5, "platinum": 1, "tin": 3})
        with open(self.jdir / "Journal.2026-10-11T100000.01.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(21, "Died")) + "\n")
        self.assertEqual(views.cargo(self.session, **self.q)["items"], [])

    def test_materials_apply_later_changes(self):
        r = views.materials(self.session, **self.q)
        raw = {m["name"]: m["count"] for m in r["materials"]["Raw"]}
        man = {m["name"]: m["count"] for m in r["materials"]["Manufactured"]}
        self.assertEqual((raw["iron"], man["heatdispersionplate"]), (105, 16))
        self.assertEqual(r["materials"]["Encoded"][0]["localised"], "Untypical Shield Scans")
        self.assertEqual(list(views.materials(self.session, category="raw", fresh=False)["materials"]), ["Raw"])

    def test_ships_current_and_stored(self):
        r = views.ships(self.session, **self.q)
        self.assertEqual((r["current"]["name"], r["current"]["cargo_capacity"], r["current"]["rebuy"]), ("Snake", 64, 47000))
        self.assertEqual(r["current"]["modules"][0]["engineering"]["blueprint"], "FSD_LongRange")
        self.assertEqual([s["type"] for s in r["stored"]["here"]], ["Sidewinder"])
        self.assertEqual((r["stored"]["remote"][0]["system"], r["stored"]["remote"][0]["transfer_price"]), ("Sol", 1200))
        self.assertNotIn("modules", views.ships(self.session, modules=False, fresh=False)["current"])

    def test_missions_active_expired_and_finished(self):
        r = views.missions(self.session, recent_days=36500, **self.q)
        self.assertEqual([m["id"] for m in r["active"]], [1])
        self.assertEqual([m["id"] for m in r["expired_unresolved"]], [2])
        self.assertEqual([(m["id"], m["result"], m["reward"]) for m in r["recent"]], [(3, "completed", 2500)])
        self.assertEqual(r["active"][0]["destination_system"], "Sol")

    def test_missions_snapshot_drops_unlisted_ones(self):
        with open(self.jdir / "Journal.2026-10-11T100000.01.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(30, "Missions", Active=[], Failed=[], Complete=[])) + "\n")
        r = views.missions(self.session, **self.q)
        self.assertEqual((r["active_count"], r["expired_unresolved"]), (0, []))

    def test_reputation_ranks_and_promotions(self):
        r = views.reputation(self.session, **self.q)
        self.assertEqual((r["ranks"]["Combat"]["name"], r["ranks"]["Trade"]["name"], r["ranks"]["Federation"]["name"]),
                         ("Expert", "Elite V", "Rear Admiral"))
        self.assertEqual(r["ranks"]["Explore"]["progress_pct"], 56)
        self.assertEqual(r["reputation"]["Federation"], 50.5)
        with open(self.jdir / "Journal.2026-10-11T100000.01.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(40, "Promotion", Combat=5)) + "\n")
        self.assertEqual(views.reputation(self.session, **self.q)["ranks"]["Combat"]["name"], "Master")

    def test_stats_and_carrier(self):
        r = views.commander_stats(self.session, section="bank_account", **self.q)
        self.assertEqual(r["statistics"], {"Bank_Account": {"Current_Wealth": 5000}})
        self.assertEqual(sorted(views.commander_stats(self.session, fresh=False)["sections"]), ["Bank_Account", "Combat"])
        self.assertFalse(views.commander_stats(self.session, section="nope", fresh=False)["found"])
        c = views.carrier(self.session, fresh=False)
        self.assertEqual((c["carrier"]["Callsign"], c["carrier"]["FuelLevel"]), ("N0B-T4X", 700))


class McpToolTests(DbTest):
    def setUp(self):
        super().setUp()
        self.journal("2026-10-11T100000.01", RICH)
        os.environ[jfiles.ENV_JOURNAL_DIR] = str(self.jdir)
        patch = mock.patch.object(mcp_server, "_run", side_effect=lambda fn, *a, **k: fn(self.session, *a, **k))
        patch.start()
        self.addCleanup(patch.stop)

    def test_tools_answer_from_the_database_with_freshness(self):
        r = mcp_server.journal_search(event="Docked")
        self.assertEqual((r["matched"], r["journal_freshness"]["db_behind_journal"]), (1, False))
        self.assertEqual(mcp_server.travel_log(days=None)["jumps"], 1)
        self.assertEqual(mcp_server.visited_systems()["distinct_systems"], 1)
        self.assertEqual(mcp_server.visited_stations()["distinct_stations"], 1)
        self.assertEqual(mcp_server.play_sessions()["sessions"][0]["events"], len(RICH))
        self.assertEqual(mcp_server.ledger(days=36500)["by_category"]["mission"]["earned"], 2500)
        self.assertEqual(mcp_server.cargo()["total"], 11)
        self.assertIn("Raw", mcp_server.materials()["materials"])
        self.assertEqual(mcp_server.ships(modules=False)["current"]["name"], "Snake")
        self.assertEqual(mcp_server.missions()["active_count"], 1)
        self.assertEqual(mcp_server.reputation()["ranks"]["Combat"]["name"], "Expert")
        self.assertIn("Bank_Account", mcp_server.commander_stats()["statistics"])
        self.assertEqual(mcp_server.carrier_status()["carrier"]["Name"], "LAST BREATH")
        self.assertEqual(mcp_server.journal_sync_status()["state"], "current")

    def test_credit_rate_auto_uses_the_database(self):
        r = mcp_server.credit_rate(streams=["trading"])
        self.assertEqual((r["source"], r["streams"]["trading"]["earned"]), ("database", 4000))

    def test_credit_rate_falls_back_to_files_when_the_database_is_unusable(self):
        with mock.patch.object(mcp_server, "_run", return_value={"error": "database schema is 0004, code needs 0006"}):
            r = mcp_server.credit_rate(streams=["trading"])
        self.assertEqual((r["source"], r["streams"]["trading"]["earned"]), ("files", 4000))
        self.assertIn("0004", r["fallback_reason"])
        with mock.patch.object(mcp_server, "_run", side_effect=RuntimeError("no connection")):
            self.assertEqual(mcp_server.credit_rate()["source"], "files")

    def test_credit_rate_falls_back_when_the_database_is_behind(self):
        reader = self.readonly_session()
        self.journal("2026-10-12T100000.01", [ev(2000, "Fileheader"), ev(2001, "Shutdown")])   # not ingested yet
        with mock.patch.object(mcp_server, "_run", side_effect=lambda fn, *a, **k: fn(reader, *a, **k)):
            r = mcp_server.credit_rate()
            self.assertEqual(r["source"], "files")
            self.assertIn("cannot write", r["fallback_reason"])
            self.assertEqual(mcp_server.credit_rate(source="db")["journal_freshness"]["db_behind_journal"], True)

    def test_source_is_validated_and_files_can_be_forced(self):
        self.assertIn("error", mcp_server.credit_rate(source="nope"))
        r = mcp_server.credit_rate(source="files", streams=["trading"])
        self.assertEqual(r["source"], "files")


if __name__ == "__main__":
    unittest.main()
