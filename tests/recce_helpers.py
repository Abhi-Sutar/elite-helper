"""Shared setup for the Recce tests: a file-backed SQLite database (separate connections, like production) and a
builder for systems with player factions."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from eddn_helper import database, migrate, parsers, store
from eddn_helper.recce import config as rcfg
from test_traffic import at, fac, sys_msg

NOW = datetime.now(timezone.utc).replace(microsecond=0)


class RecceDb(unittest.TestCase):
    """Temp database + temp Recce config dir. self.factory is a sessionmaker; self.put() feeds a system."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {rcfg.ENV_DIR: os.path.join(self.tmp.name, "recce")})
        env.start()
        self.addCleanup(env.stop)
        self.engine = database.make_engine("sqlite:///" + os.path.join(self.tmp.name, "t.sqlite"))
        self.addCleanup(self.engine.dispose)
        migrate.upgrade(self.engine, backup=False)
        self.factory = database.make_session_factory(self.engine)

    def put(self, id64, name, observations, pos=(0, 0, 0), economy="Agri", population=5_000_000, conflicts=None,
            others=("Filler",)):
        """observations: [(hours_ago, controlling faction dict)], oldest first. The first faction controls."""
        s = self.factory()
        try:
            for hours_ago, f in observations:
                msg = sys_msg([f] + [fac(o, 0.2) for o in others], at(hours_ago), id64=id64, name=name)
                m = msg["message"]
                m["StarPos"] = list(pos)
                m["Population"] = population
                m["SystemEconomy"] = f"$economy_{economy};"
                if conflicts:
                    m["Conflicts"] = conflicts
                store.apply_parsed(s, parsers.parse_message(msg))
            s.commit()
        finally:
            s.close()

    def players(self, flags):
        s = self.factory()
        try:
            store.upsert_faction_info(s, flags)
            s.commit()
        finally:
            s.close()


def pending(name, infl=0.6):
    return fac(name, infl, pending=[("Expansion", 0)])


def expanding(name, infl=0.6):
    return fac(name, infl, state="Expansion", active=["Expansion"])


def plain(name, infl=0.6):
    return fac(name, infl)
