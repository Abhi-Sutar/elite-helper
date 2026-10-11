"""Display timezone: tables and CSV can show local times, --json and stored values stay UTC."""
import contextlib
import io
import json
import os
import unittest
from argparse import Namespace
from unittest import mock

from eddn_helper import cli, config

STAMP = "2026-10-11T10:54:03Z"
SUMMER = "2026-07-01T12:00:00Z"
WINTER = "2026-01-01T12:00:00Z"


def with_tz(value):
    env = {} if value is None else {config.ENV_TZ: value}
    return mock.patch.dict(os.environ, env, clear=False) if value else mock.patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != config.ENV_TZ}, clear=True)


class DisplayTimezoneTests(unittest.TestCase):
    def test_default_is_utc_and_unchanged(self):
        with with_tz(None):
            self.assertIsNone(config.display_tz())
            self.assertEqual(cli.show_time(STAMP), STAMP)
        with with_tz("UTC"):
            self.assertEqual(cli.show_time(STAMP), STAMP)

    def test_iana_zone_follows_daylight_saving(self):
        with with_tz("Europe/Berlin"):
            self.assertEqual(cli.show_time(STAMP), "2026-10-11T12:54:03+02:00")
            self.assertEqual(cli.show_time(SUMMER), "2026-07-01T14:00:00+02:00")
            self.assertEqual(cli.show_time(WINTER), "2026-01-01T13:00:00+01:00")

    def test_local_uses_the_system_zone(self):
        with with_tz("local"):
            out = cli.show_time(STAMP)
        self.assertRegex(out, r"^2026-10-1\dT\d\d:\d\d:03[+-]\d\d:\d\d$")

    def test_only_utc_stamps_are_converted(self):
        with with_tz("Europe/Berlin"):
            for v in (None, 5, "gold", "2026-10-11 10:54:03", "", "x" + STAMP):
                self.assertEqual(cli.show_time(v), v)

    def test_table_and_csv_convert_but_json_does_not(self):
        data = {"series": [{"ts": STAMP, "n": 1}]}
        with with_tz("Europe/Berlin"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.table(data["series"], [("Time", "ts"), ("N", "n")])
            self.assertIn("2026-10-11T12:54:03+02:00", buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.emit(Namespace(csv=True, json=False), data, lambda d: None,
                         csv_rows=lambda d: (["ts", "n"], d["series"]))
            self.assertIn("2026-10-11T12:54:03+02:00,1", buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.emit(Namespace(csv=False, json=True), data, lambda d: None)
            self.assertEqual(json.loads(buf.getvalue()), data)   # still UTC, machine readable


if __name__ == "__main__":
    unittest.main()
