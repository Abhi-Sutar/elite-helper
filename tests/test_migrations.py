import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select, text

from eddn_helper import database, legacy, lock, migrate, queries, store
from eddn_helper.models import Base, Commodity, CommodityPrice, Market, PriceHistory, System

warnings.filterwarnings("ignore", message=".*expression-based index.*")

BAD_REVISION = '''
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scratch", sa.Column("id", sa.Integer(), primary_key=True))
    raise RuntimeError("boom")


def downgrade():
    pass
'''

GOOD_REVISION = BAD_REVISION.replace('    raise RuntimeError("boom")\n', "")


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engines = []
        self.addCleanup(lambda: [e.dispose() for e in self.engines])

    def engine(self, name="t.sqlite"):
        e = database.make_engine(f"sqlite:///{(self.tmp / name).as_posix()}")
        self.engines.append(e)
        return e

    def scratch_migrations(self, revision_src):
        loc = self.tmp / "migrations"
        shutil.copytree(migrate.SCRIPT_LOCATION, loc, ignore=shutil.ignore_patterns("__pycache__"))
        (loc / "versions" / "0002_scratch.py").write_text(revision_src)
        return loc


class MigrationTests(TmpDirTest):
    def test_schema_head_constant_matches_alembic(self):
        self.assertEqual(migrate.SCHEMA_HEAD, migrate.head_revision())

    def test_fresh_db_reaches_head_and_is_idempotent(self):
        e = self.engine()
        self.assertEqual(migrate.status(e)["state"], "empty")
        self.assertEqual(migrate.upgrade(e, backup=False)["state"], "ok")
        self.assertEqual(migrate.upgrade(e, backup=False)["state"], "ok")
        self.assertEqual(migrate.ensure_current(e)["current"], migrate.SCHEMA_HEAD)

    def test_models_match_migrations(self):
        e = self.engine()
        migrate.upgrade(e, backup=False)
        with e.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
            diff = compare_metadata(ctx, Base.metadata)
        self.assertEqual(diff, [])

    def test_reader_refuses_unmigrated_and_newer_db(self):
        e = self.engine()
        with self.assertRaises(migrate.SchemaError):
            migrate.ensure_current(e)
        migrate.upgrade(e, backup=False)
        loc = self.scratch_migrations(GOOD_REVISION)
        with mock.patch.object(migrate, "SCRIPT_LOCATION", loc):
            migrate.upgrade(e, backup=False)
        self.assertEqual(migrate.status(e)["state"], "ahead")  # real code only knows 0001
        with self.assertRaises(migrate.SchemaError):
            migrate.ensure_current(e)

    def test_failing_revision_rolls_back_completely(self):
        e = self.engine()
        migrate.upgrade(e, backup=False)
        loc = self.scratch_migrations(BAD_REVISION)
        with mock.patch.object(migrate, "SCRIPT_LOCATION", loc):
            with self.assertRaises(Exception):
                migrate.upgrade(e, backup=False)
        self.assertNotIn("scratch", inspect(e).get_table_names())
        self.assertEqual(migrate.current_revision(e), "0001")

    def test_upgrade_makes_backup_of_existing_db(self):
        e = self.engine()
        migrate.upgrade(e, backup=False)
        loc = self.scratch_migrations(GOOD_REVISION)
        with mock.patch.object(migrate, "SCRIPT_LOCATION", loc):
            migrate.upgrade(e)
        self.assertEqual(len(list(self.tmp.glob("t.sqlite.pre-0001-*.bak"))), 1)

    def test_concurrent_upgraders_apply_once(self):
        """Separate processes (as in real life: listener + `migrate upgrade`) race; the revision applies once."""
        loc = self.scratch_migrations(GOOD_REVISION)
        db_path = self.tmp / "race.sqlite"
        code = "\n".join([
            "import sys",
            "from pathlib import Path",
            "from eddn_helper import database, migrate",
            "migrate.SCRIPT_LOCATION = Path(sys.argv[2])",
            "e = database.make_engine('sqlite:///' + sys.argv[1])",
            "migrate.upgrade(e, backup=False)",
        ])
        root = str(Path(__file__).resolve().parent.parent)
        procs = [subprocess.Popen([sys.executable, "-c", code, db_path.as_posix(), str(loc)], cwd=root,
                                  stderr=subprocess.PIPE) for _ in range(4)]
        errs = [p.communicate()[1].decode() for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0, 0, 0], errs)
        e = self.engine("race.sqlite")
        with mock.patch.object(migrate, "SCRIPT_LOCATION", loc):
            self.assertEqual(migrate.current_revision(e), "0002")


class LegacyImportTests(TmpDirTest):
    def make_old_db(self):
        path = self.tmp / "eddn.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
        CREATE TABLE systems (id64 INTEGER PRIMARY KEY, name TEXT, x REAL, y REAL, z REAL, population INTEGER,
            allegiance TEXT, government TEXT, security TEXT, economy TEXT, second_economy TEXT,
            controlling_faction TEXT, conflicts TEXT, updated_at TEXT, factions_updated_at TEXT, source TEXT);
        CREATE TABLE factions (system_id64 INTEGER, name TEXT, government TEXT, allegiance TEXT, influence REAL,
            state TEXT, happiness TEXT, active_states TEXT, pending_states TEXT, recovering_states TEXT,
            is_controlling INTEGER, updated_at TEXT, source TEXT);
        CREATE TABLE faction_history (system_id64 INTEGER, faction TEXT, ts TEXT, influence REAL, state TEXT);
        CREATE TABLE markets (market_id INTEGER PRIMARY KEY, station TEXT, system_name TEXT, station_type TEXT,
            is_carrier INTEGER, economies TEXT, prohibited TEXT, updated_at TEXT, source TEXT);
        CREATE TABLE commodity_prices (market_id INTEGER, commodity TEXT, buy_price INTEGER, sell_price INTEGER,
            mean_price INTEGER, stock INTEGER, demand INTEGER, stock_bracket INTEGER, demand_bracket INTEGER);
        CREATE TABLE price_history (market_id INTEGER, commodity TEXT, ts TEXT, buy_price INTEGER,
            sell_price INTEGER, stock INTEGER, demand INTEGER);
        INSERT INTO systems VALUES (1,'Sol',0,0,0,100,'Federation','Democracy','high','Refinery','Service',
            'Alpha','[]','2026-10-10T10:00:00Z','2026-10-10T10:00:00Z','eddn');
        INSERT INTO factions VALUES (1,'Alpha','Democracy','Federation',0.6,'Boom','Happy','["Boom"]','[]','[]',
            1,'2026-10-10T10:00:00Z','eddn');
        INSERT INTO faction_history VALUES (1,'Alpha','2026-10-10T10:00:00Z',0.6,'Boom');
        INSERT INTO markets VALUES (9,'Hub','Sol','Orbis',0,'["Refinery"]','[]','2026-10-10T10:00:00Z','eddn');
        INSERT INTO commodity_prices VALUES (9,'gold',9000,0,8000,50,0,2,0);
        INSERT INTO price_history VALUES (9,'gold','2026-10-10T10:00:00Z',9000,0,50,0);
        INSERT INTO price_history VALUES (9,'gold','2026-10-10T12:00:00Z',9500,0,40,0);
        """)
        c.commit()
        c.close()
        return path

    def test_import_preserves_rows_and_converts_types(self):
        old = self.make_old_db()
        e = self.engine("new.sqlite")
        migrate.upgrade(e, backup=False)
        with database.session_scope(database.make_session_factory(e)) as s:
            counts = legacy.import_legacy(s, old)
        self.assertEqual(counts, {"systems": 1, "factions": 1, "faction_history": 1, "markets": 1,
                                  "commodity_prices": 1, "price_history": 2})
        with database.session_scope(database.make_session_factory(e)) as s:
            info = queries.system_info(s, "sol")
            self.assertEqual(info["factions"][0]["influence_pct"], 60.0)
            self.assertEqual(info["factions"][0]["active_states"], ["Boom"])
            m = queries.station_market(s, "hub")
            self.assertEqual(m["commodities"][0]["commodity"], "gold")
            self.assertEqual(s.get(Market, 9).system_id64, 1)  # linked after import
            h = queries.price_history(s, "hub", "gold", days=36500)
            self.assertEqual([r["buy_price"] for r in h["series"]], [9000, 9500])
            self.assertEqual(s.execute(select(System.updated_at)).scalar_one().tzinfo is not None, True)


    def test_orphan_child_rows_are_skipped_not_fatal(self):
        old = self.make_old_db()
        c = sqlite3.connect(old)
        c.execute("INSERT INTO price_history VALUES (99,'gold','2026-10-10T11:00:00Z',1,1,1,1)")
        c.execute("INSERT INTO factions VALUES (77,'Ghost','Democracy','Federation',0.1,'None','Happy','[]','[]','[]',"
                  "0,'2026-10-10T10:00:00Z','eddn')")
        c.commit()
        c.close()
        e = self.engine("new2.sqlite")
        migrate.upgrade(e, backup=False)
        with database.session_scope(database.make_session_factory(e)) as s:
            counts = legacy.import_legacy(s, old)
        self.assertEqual(counts["price_history"], 2)
        self.assertEqual(counts["skipped_orphans"], {"price_history": 1, "factions": 1})


class LockTests(TmpDirTest):
    def test_second_acquire_fails_and_release_frees(self):
        path = self.tmp / "listener.lock"
        a, b = lock.ListenerLock(path), lock.ListenerLock(path)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        running, pid = lock.holder(path)
        self.assertTrue(running)
        self.assertEqual(pid, __import__("os").getpid())
        a.release()
        self.assertEqual(lock.holder(path), (False, None))
        self.assertTrue(b.acquire())
        b.release()


if __name__ == "__main__":
    unittest.main()
