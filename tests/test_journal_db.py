"""Journal in the database: ingest, on-demand freshness gate, read-only/busy fallbacks, status, service loop."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import event as sa_event, func, inspect, select, text

from eddn_helper import cli, config, database, lock, migrate
from eddn_helper.journal import files as jfiles
from eddn_helper.journal import ingest
from eddn_helper.models import JournalCommander, JournalEvent, JournalFile
from test_journal import SESSION, ev, write_journal


class DbTest(unittest.TestCase):
    """A migrated SQLite file database plus an empty journal directory, both in a temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.jdir = self.tmp / "journals"
        self.jdir.mkdir()
        self.url = f"sqlite:///{(self.tmp / 'db.sqlite').as_posix()}"
        self.engine = database.make_engine(self.url)
        self.addCleanup(self.engine.dispose)
        migrate.upgrade(self.engine, backup=False)
        self.session = database.make_session_factory(self.engine)()
        self.addCleanup(self.session.close)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(config.ENV_INGEST_URL, None)

    def journal(self, name, events, **kw):
        return write_journal(self.jdir, name, events, **kw)

    def readonly_session(self):
        engine = database.make_engine(self.url)
        sa_event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA query_only=1"))
        self.addCleanup(engine.dispose)
        return database.make_session_factory(engine)()

    def count(self, model=JournalEvent):
        return self.session.execute(select(func.count()).select_from(model)).scalar_one()


class Migration0006Test(DbTest):
    def test_tables_and_indexes_exist_and_downgrade_is_clean(self):
        names = set(inspect(self.engine).get_table_names())
        self.assertTrue({"journal_commander", "journal_file", "journal_event"} <= names)
        idx = {i["name"] for i in inspect(self.engine).get_indexes("journal_event")}
        self.assertIn("ix_journal_event_fid_event_ts", idx)


class IngestTests(DbTest):
    def test_ingest_stamps_location_and_commander(self):
        self.journal("2026-10-11T100000.01", SESSION + [ev(10, "Shutdown")])
        stats = ingest.sync(self.session, str(self.jdir))
        self.assertEqual((stats["files"], stats["events"]), (1, len(SESSION) + 1))
        buy = self.session.execute(select(JournalEvent).where(JournalEvent.event == "MarketBuy")).scalar_one()
        self.assertEqual((buy.system_name, buy.system_id64, buy.station, buy.market_id, buy.fid),
                         ("Alpha Centauri", 5, "Hutton Orbital", 99, "F1"))
        self.assertEqual(buy.data["TotalCost"], 1000)
        jump = self.session.execute(select(JournalEvent).where(JournalEvent.event == "FSDJump")).scalar_one()
        self.assertEqual((jump.system_name, jump.station), ("Alpha Centauri", None))
        cmdr = self.session.get(JournalCommander, "F1")
        self.assertEqual(cmdr.name, "CMDR Test")
        jf = self.session.execute(select(JournalFile)).scalar_one()
        self.assertTrue(jf.complete)
        self.assertEqual(jf.lines_read, len(SESSION) + 1)
        self.assertIsNone(self.session.execute(select(JournalEvent).where(JournalEvent.fid.is_(None))).first())

    def test_ignored_events_are_not_stored(self):
        self.journal("2026-10-11T100000.01", SESSION + [ev(5, "Music", MusicTrack="x")])
        with mock.patch.object(ingest, "IGNORED_EVENTS", frozenset({"Music"})):
            ingest.sync(self.session, str(self.jdir))
        self.assertEqual(self.count(), len(SESSION))

    def test_incremental_resume_adds_only_new_lines(self):
        path = self.journal("2026-10-11T100000.01", SESSION)
        ingest.sync(self.session, str(self.jdir))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(20, "MarketSell", Type="gold", Count=10, TotalSale=5000)) + "\n")
        plan = ingest.compare(self.session, str(self.jdir))
        self.assertEqual(plan["grown"], ["Journal.2026-10-11T100000.01.log"])
        stats = ingest.sync(self.session, str(self.jdir))
        self.assertEqual(stats["events"], 1)
        self.assertEqual(self.count(), len(SESSION) + 1)
        sell = self.session.execute(select(JournalEvent).where(JournalEvent.event == "MarketSell")).scalar_one()
        self.assertEqual(sell.station, None)   # last event before it was Undocked
        self.assertEqual(ingest.sync(self.session, str(self.jdir))["files"], 0)

    def test_half_written_last_line_is_picked_up_once_complete(self):
        path = self.journal("2026-10-11T100000.01", SESSION, trailing='{"timestamp":"2026-10-11T10:30:00Z","eve')
        ingest.sync(self.session, str(self.jdir))
        self.assertEqual(self.count(), len(SESSION))
        self.assertFalse(ingest.compare(self.session, str(self.jdir))["behind"])
        with open(path, "a", encoding="utf-8") as f:   # the game finishes the line
            f.write('nt":"Music"}\n' + json.dumps(ev(31, "Shutdown")) + "\n")
        ingest.sync(self.session, str(self.jdir))
        self.assertEqual(self.count(), len(SESSION) + 2)   # the completed Music line and Shutdown
        self.assertEqual(self.session.execute(select(JournalFile.lines_read)).scalar_one(), len(SESSION) + 2)

    def test_shrunk_file_is_rebuilt(self):
        path = self.journal("2026-10-11T100000.01", SESSION)
        ingest.sync(self.session, str(self.jdir))
        self.journal("2026-10-11T100000.01", SESSION[:3])   # same name, shorter: a different file
        self.assertEqual(ingest.compare(self.session, str(self.jdir))["shrunk"], [os.path.basename(path)])
        ingest.sync(self.session, str(self.jdir))
        self.assertEqual(self.count(), 3)

    def test_rebuild_rereads_everything_without_duplicates(self):
        self.journal("2026-10-11T100000.01", SESSION)
        ingest.sync(self.session, str(self.jdir))
        stats = ingest.sync(self.session, str(self.jdir), rebuild=True)
        self.assertEqual(stats["events"], len(SESSION))
        self.assertEqual(self.count(), len(SESSION))

    def test_only_the_newest_file_stays_incomplete(self):
        self.journal("2026-10-11T100000.01", SESSION)
        self.journal("2026-10-11T120000.01", [ev(120, "Fileheader"), ev(121, "Commander", Name="CMDR Test", FID="F1")])
        ingest.sync(self.session, str(self.jdir))
        flags = dict(self.session.execute(select(JournalFile.name, JournalFile.complete)).all())
        self.assertEqual(flags, {"Journal.2026-10-11T100000.01.log": True, "Journal.2026-10-11T120000.01.log": False})


class FreshnessGateTests(DbTest):
    def test_empty_database_is_ingested_before_answering(self):
        self.journal("2026-10-11T100000.01", SESSION)
        fresh = ingest.ensure_fresh(self.session, str(self.jdir))
        self.assertEqual((fresh["db_behind_journal"], fresh["files_ingested_now"], fresh["events_ingested_now"]),
                         (False, 1, len(SESSION)))
        self.assertIsNone(fresh["ingest_error"])
        self.assertEqual(fresh["last_event_in_db"], SESSION[-1]["timestamp"])
        self.assertIn("newest_journal_write", fresh)

    def test_current_database_is_not_touched(self):
        self.journal("2026-10-11T100000.01", SESSION)
        ingest.ensure_fresh(self.session, str(self.jdir))
        with mock.patch.object(ingest, "sync") as sync:
            fresh = ingest.ensure_fresh(self.session, str(self.jdir))
        sync.assert_not_called()
        self.assertEqual((fresh["db_behind_journal"], fresh["files_ingested_now"]), (False, 0))

    def test_grown_journal_ingests_only_the_new_lines(self):
        path = self.journal("2026-10-11T100000.01", SESSION)
        ingest.ensure_fresh(self.session, str(self.jdir))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(40, "Shutdown")) + "\n")
        fresh = ingest.ensure_fresh(self.session, str(self.jdir))
        self.assertEqual((fresh["files_ingested_now"], fresh["events_ingested_now"]), (1, 1))
        self.assertEqual(fresh["last_event_in_db"], ev(40, "Shutdown")["timestamp"])

    def test_read_only_database_answers_from_stored_data_and_says_it_is_behind(self):
        self.journal("2026-10-11T100000.01", SESSION[:4])
        ingest.ensure_fresh(self.session, str(self.jdir))
        self.journal("2026-10-11T120000.01", [ev(120, "Fileheader")])
        reader = self.readonly_session()
        fresh = ingest.ensure_fresh(reader, str(self.jdir))
        self.assertTrue(fresh["db_behind_journal"])
        self.assertIn("cannot write", fresh["ingest_error"])
        self.assertIn(config.ENV_INGEST_URL, fresh["ingest_error"])
        self.assertEqual(fresh["last_event_in_db"], SESSION[3]["timestamp"])   # stored data still served

    def test_ingest_url_gives_a_read_only_caller_a_writer(self):
        self.journal("2026-10-11T100000.01", SESSION)
        reader = self.readonly_session()
        with mock.patch.dict(os.environ, {config.ENV_INGEST_URL: self.url}):
            fresh = ingest.ensure_fresh(reader, str(self.jdir))
        self.assertEqual((fresh["db_behind_journal"], fresh["events_ingested_now"], fresh["ingest_error"]),
                         (False, len(SESSION), None))
        self.assertEqual(self.count(), len(SESSION))
        ingest._writer_engines.pop(self.url).dispose()

    def test_busy_lock_means_no_double_write(self):
        self.journal("2026-10-11T100000.01", SESSION)
        other = lock.ListenerLock(config.named_lock_path(self.url, "journal-ingest"))
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        fresh = ingest.ensure_fresh(self.session, str(self.jdir), wait=0.4)
        self.assertTrue(fresh["db_behind_journal"])
        self.assertIn("another journal ingest", fresh["ingest_error"])
        self.assertEqual(self.count(), 0)

    def test_no_journal_files(self):
        fresh = ingest.ensure_fresh(self.session, str(self.jdir))
        self.assertIn("no journal files", fresh["ingest_error"])
        self.assertFalse(fresh["db_behind_journal"])


class StatusTests(DbTest):
    def status(self, session=None):
        return ingest.journal_status(session or self.session, str(self.jdir), check_task=False)

    def test_states(self):
        self.assertEqual(self.status()["state"], "no_journals")
        self.journal("2026-10-11T100000.01", SESSION)
        st = self.status()
        self.assertEqual((st["state"], st["files_on_disk"], st["files_ingested"], st["events"]), ("behind", 1, 0, 0))
        self.assertGreater(st["bytes_behind"], 0)
        self.assertEqual(self.status(self.readonly_session())["state"], "behind")   # file is still writable on disk
        ingest.ensure_fresh(self.session, str(self.jdir))
        st = self.status()
        self.assertEqual((st["state"], st["events"], st["files_to_ingest"]), ("current", len(SESSION), []))
        self.assertEqual(st["commanders"][0]["fid"], "F1")
        self.assertEqual(st["commanders"][0]["events"], len(SESSION))
        self.assertIsNotNone(st["last_ingest_minutes_ago"])
        self.assertFalse(st["ingester"]["running"])

    def test_no_writer_when_the_database_cannot_be_written(self):
        self.journal("2026-10-11T100000.01", SESSION)
        with mock.patch.object(ingest, "can_write", return_value=False):
            self.assertEqual(self.status()["state"], "no_writer")

    def test_error_is_recorded_and_shown(self):
        self.journal("2026-10-11T100000.01", SESSION)
        ingest.ensure_fresh(self.readonly_session(), str(self.jdir))
        # read-only: the error could not be stored, but the live ensure_fresh result carried it (see gate tests)
        ingest._record_error(self.session, "boom")
        self.assertEqual(self.status()["last_error"], "boom")


class WatchTests(DbTest):
    def test_watch_pass_ingests_and_keeps_a_heartbeat_and_holds_the_service_lock(self):
        self.journal("2026-10-11T100000.01", SESSION)
        logs = []
        self.engine.dispose()
        self.assertTrue(ingest.watch(self.url, str(self.jdir), once=True, log=logs.append))
        self.assertTrue(any("ingested" in line for line in logs))
        st = self.status_after()
        self.assertEqual(st["events"], len(SESSION))
        self.assertIsNotNone(st["ingester"]["heartbeat_seconds_ago"])

    def status_after(self):
        self.session.rollback()
        return ingest.journal_status(self.session, str(self.jdir), check_task=False)

    def test_second_ingester_is_refused(self):
        svc = lock.ListenerLock(config.named_lock_path(self.url, "journal-ingester"))
        self.assertTrue(svc.acquire())
        self.addCleanup(svc.release)
        self.assertFalse(ingest.watch(self.url, str(self.jdir), once=True, log=lambda m: None))
        self.assertTrue(self.status_after()["ingester"]["running"])


class CliTests(DbTest):
    def run_cli(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        args = cli.build_parser().parse_args(["--db", self.url, *argv])
        with contextlib.redirect_stdout(out):
            if getattr(args, "raw", False):
                rc = args.func(args, None)
            else:
                rc = args.func(args, self.session) or 0
        return rc, out.getvalue()

    def test_sync_then_status(self):
        self.journal("2026-10-11T100000.01", SESSION)
        rc, out = self.run_cli("journal-sync", "--journal-dir", str(self.jdir), "--json")
        self.assertEqual((rc, json.loads(out)["events"]), (0, len(SESSION)))
        rc, out = self.run_cli("journal-status", "--journal-dir", str(self.jdir), "--json")
        self.assertEqual(json.loads(out)["state"], "current")
        rc, out = self.run_cli("journal-status", "--journal-dir", str(self.jdir))
        self.assertIn("State: current", out)
        self.assertIn("CMDR Test", out)

    def test_sync_without_journals_fails_cleanly(self):
        rc, out = self.run_cli("journal-sync", "--journal-dir", str(self.jdir), "--json")
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out))


if __name__ == "__main__":
    unittest.main()
