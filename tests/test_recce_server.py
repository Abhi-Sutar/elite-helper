"""The Recce HTTP server with a fake core: loopback only, endpoints, SSE, refresh timing, errors keep the last good
data, a slow query does not block the server, config edits reload live, and one server per machine."""

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from eddn_helper.recce import config as rcfg
from eddn_helper.recce import server
from eddn_helper.recce.core import CoreError


class FakeCore:
    def __init__(self):
        self.stamp = server.iso(server.now_utc())
        self.snapshots = 0
        self.position = "Sol"
        self.fail = None
        self.block = None

    def stamps(self):
        return {"newest_faction_update": self.stamp, "newest_market_update": "m1"}

    def resolve_origin(self, cfg):
        return "Hub", "config"

    def current_system(self):
        return self.position

    def recentre(self, name):
        if self.position is None:
            raise CoreError("cannot recentre: your current position is unknown (no journal)")
        previous = rcfg.load(name)["origin"]
        rcfg.update(name, {"origin": self.position})
        return {"name": name, "origin": self.position, "previous": previous}

    def snapshot(self, cfg):
        if self.block is not None:
            self.block.wait(20)
        if self.fail:
            raise CoreError(self.fail)
        self.snapshots += 1
        return {"dashboard": cfg["name"], "generated_at": "2026-10-13T10:00:05Z",
                "origin": {"name": "Hub", "source": "config"},
                "db": {"newest_faction_update": self.stamp, "newest_market_update": "m1"}, "count": 1,
                "hidden_unknown_player": 0, "truncated": False, "live_fetch": {"requests": 0},
                "rows": [{"key": "A|F", "system": "A", "faction": "F", "kind": "pending", "run": None,
                          "pending_text": "unknown", "last_update_at": "2026-10-13T09:00:00Z", "stale": False,
                          "highlight": False}]}


def wait_for(cond, timeout=6.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {rcfg.ENV_DIR: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch("eddn_helper.journal.context.where_am_i", return_value={"error": "no journal"}):
            rcfg.ensure_default()                       # unreadable journal: the default keeps following ("current")
        self.fake = FakeCore()
        self.srv = server.RecceServer(0, self.fake, poll_seconds=0.05)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.srv.port

    def stop(self):
        if self.fake.block is not None:
            self.fake.block.set()
        if self.thread.is_alive():
            self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)

    def get(self, path, host=None, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if host:
            h["Host"] = host
        conn.request(method, path, headers=h)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def json(self, path):
        code, _, body = self.get(path)
        return code, json.loads(body)

    def runner(self):
        return self.srv.manager.get("recce")

    def loaded(self):
        self.runner()
        return wait_for(lambda: self.json("/api/data?d=recce")[1].get("count") == 1)


class EndpointTests(ServerCase):
    def test_listens_on_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_pages_and_json(self):
        code, headers, _ = self.get("/")
        self.assertEqual((code, headers["Location"]), (302, "/?d=recce"))
        code, headers, body = self.get("/?d=recce")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"EventSource", body)
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))          # nothing fetched from outside
        self.assertEqual(self.json("/api/health")[1]["app"], "recce")
        code, cfg = self.json("/api/config?d=recce")
        self.assertEqual((code, cfg["config"]["name"], cfg["labels"]["pending_for"]), (200, "recce", "Expansion pending for"))
        self.assertTrue(self.loaded())
        _, data = self.json("/api/data?d=recce")
        self.assertEqual(data["rows"][0]["system"], "A")
        self.assertEqual(data["status"]["state"], "live")
        self.assertEqual(data["status"]["db_newest_faction_update"], self.fake.stamp)
        self.assertEqual(self.json("/api/dashboards")[1][0]["name"], "recce")

    def test_unknown_dashboard_unknown_path_and_foreign_host(self):
        self.assertEqual(self.get("/api/data?d=nope")[0], 404)
        self.assertEqual(self.get("/api/data")[0], 404)
        self.assertEqual(self.get("/nothing")[0], 404)
        self.assertEqual(self.get("/api/health", host="evil.example.com")[0], 403)          # DNS rebinding guard
        self.assertEqual(self.get("/api/health", host="localhost:1234")[0], 200)

    def test_shutdown_needs_the_header(self):
        self.assertEqual(self.get("/api/shutdown", method="POST")[0], 404)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.get("/api/shutdown", method="POST", headers={"X-Recce": "1"})[0], 200)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())


class SseTests(ServerCase):
    def events(self, until="data", timeout=6.0):
        """Open /events and return [(event name, payload)] up to and including the first `until` event."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", "/events?d=recce")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        seen, name = [], None
        end = time.monotonic() + timeout
        try:
            while time.monotonic() < end and not (seen and seen[-1][0] == until):
                line = resp.fp.readline().decode("utf-8").strip()
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:") and name:
                    seen.append((name, json.loads(line[5:])))
                    name = None
        finally:
            conn.close()
        return seen

    def test_config_then_data_events(self):
        got = self.events("data")
        self.assertEqual(got[0][0], "config")
        self.assertEqual(got[-1][0], "data")
        self.assertEqual(got[0][1]["config"]["name"], "recce")
        self.assertEqual(got[-1][1]["rows"][0]["system"], "A")

    def test_data_event_when_the_database_moves(self):
        self.assertTrue(self.loaded())
        self.runner().gate.set_interval(0.2)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        conn.request("GET", "/events?d=recce")
        resp = conn.getresponse()
        self.fake.stamp = server.iso(server.now_utc() + timedelta(seconds=30))
        found = None
        end = time.monotonic() + 6
        name = None
        while time.monotonic() < end and not found:
            line = resp.fp.readline().decode("utf-8").strip()
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:") and name == "data":
                payload = json.loads(line[5:])
                if payload["db"]["newest_faction_update"] == self.fake.stamp:
                    found = payload
        conn.close()
        self.assertIsNotNone(found)


class RefreshTimingTests(ServerCase):
    def test_a_change_waits_for_the_interval_and_no_change_never_refreshes(self):
        self.assertTrue(self.loaded())
        r = self.runner()
        first = r.gate.last_at
        r.gate.set_interval(1.5)
        time.sleep(0.5)
        self.assertEqual(self.fake.snapshots, 1)                       # unchanged database: nothing re-run
        self.fake.stamp = server.iso(server.now_utc() + timedelta(seconds=60))
        time.sleep(0.6)
        self.assertEqual(self.fake.snapshots, 1)                       # moved, but the interval has not passed
        self.assertTrue(wait_for(lambda: r.gate.last_at != first))
        self.assertEqual(self.fake.snapshots, 2)
        self.assertGreaterEqual(r.gate.last_at - first, 1.5 - 0.05)    # the slower of "changed" and "interval"
        r.gate.set_interval(0.2)
        time.sleep(1.0)
        self.assertEqual(self.fake.snapshots, 2)                       # settled again: no churn


class ResilienceTests(ServerCase):
    def trigger(self):
        """A config edit forces an immediate re-query (this is also how dashboard_update live-reloads)."""
        rcfg.update("recce", {"title": f"T{time.monotonic()}"})

    def test_config_edit_reloads_and_requeries_at_once(self):
        self.assertTrue(self.loaded())
        before = self.fake.snapshots
        rcfg.update("recce", {"title": "Renamed"})
        self.assertTrue(wait_for(lambda: self.json("/api/config?d=recce")[1]["config"]["title"] == "Renamed"))
        self.assertTrue(wait_for(lambda: self.fake.snapshots > before))

    def test_error_keeps_last_good_data_and_recovers(self):
        self.assertTrue(self.loaded())
        self.fake.fail = "database exploded"
        self.trigger()
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["state"] == "error"))
        _, data = self.json("/api/data?d=recce")
        self.assertEqual(data["count"], 1)                              # last good rows are still served
        self.assertIn("database exploded", data["status"]["error"])
        self.fake.fail = None
        self.runner().gate.set_interval(0.1)
        self.trigger()
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["state"] == "live"))

    def test_slow_query_does_not_block_the_server(self):
        self.assertTrue(self.loaded())
        self.fake.block = threading.Event()
        self.trigger()
        time.sleep(0.4)                                                 # the snapshot is now stuck in the worker
        t = time.monotonic()
        self.assertEqual(self.get("/api/health")[0], 200)
        _, data = self.json("/api/data?d=recce")
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(data["count"], 1)                              # still the last good data
        self.fake.block.set()

    def test_stamp_check_failure_shows_error_then_clears(self):
        self.assertTrue(self.loaded())
        with mock.patch.object(self.fake, "stamps", side_effect=RuntimeError("connection refused")):
            self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["state"] == "error"))
            self.assertIn("connection refused", self.json("/api/data?d=recce")[1]["status"]["error"])
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["state"] == "live"))

    def test_stale_when_the_database_is_old(self):
        self.fake.stamp = "2020-01-01T00:00:00Z"
        self.assertTrue(self.loaded())
        self.assertEqual(self.json("/api/data?d=recce")[1]["status"]["state"], "stale")

    def test_invalid_config_edit_keeps_running_and_reports(self):
        self.assertTrue(self.loaded())
        (rcfg.path_for("recce")).write_text('{"refresh_seconds": 1}', encoding="utf-8")
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["state"] == "error"))
        self.assertIn("refresh_seconds", self.json("/api/data?d=recce")[1]["status"]["error"])
        self.assertEqual(self.json("/api/data?d=recce")[1]["count"], 1)

    def test_deleted_dashboard_is_reported(self):
        self.assertTrue(self.loaded())
        rcfg.delete("recce")
        self.assertTrue(wait_for(lambda: "no longer exists" in (self.json("/api/data?d=recce")[1]["status"]["error"]
                                                                  if self.get("/api/data?d=recce")[0] == 200 else "")
                                 or self.get("/api/data?d=recce")[0] == 404))


class RecentreTests(ServerCase):
    def post(self, path, headers=None):
        return self.get(path, method="POST", headers=headers)

    def test_post_needs_the_header(self):
        self.assertEqual(self.post("/api/recentre?d=recce")[0], 404)
        self.assertEqual(rcfg.load("recce")["origin"], "current")

    def test_recentre_anchors_and_open_pages_get_the_new_config(self):
        self.assertTrue(self.loaded())
        self.assertEqual(self.json("/api/data?d=recce")[1]["status"]["current_system"], "Sol")
        code, _, body = self.post("/api/recentre?d=recce", {"X-Recce": "1"})
        self.assertEqual((code, json.loads(body)["origin"]), (200, "Sol"))
        self.assertEqual(rcfg.load("recce")["origin"], "Sol")
        self.assertTrue(wait_for(lambda: self.json("/api/config?d=recce")[1]["config"]["origin"] == "Sol"))

    def test_unknown_position_and_unknown_dashboard(self):
        self.fake.position = None
        code, _, body = self.post("/api/recentre?d=recce", {"X-Recce": "1"})
        self.assertEqual(code, 409)
        self.assertIn("unknown", json.loads(body)["error"])
        self.assertEqual(self.post("/api/recentre?d=nope", {"X-Recce": "1"})[0], 404)

    def test_anchored_dashboard_ignores_moves_but_the_button_label_learns_them(self):
        rcfg.update("recce", {"origin": "Hub"})
        self.assertTrue(self.loaded())
        self.runner().gate.set_interval(0.1)
        n = self.fake.snapshots
        self.fake.position = "Achenar"
        self.runner()._origin_checked = 0.0                              # force the next position check
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=recce")[1]["status"]["current_system"] == "Achenar"))
        time.sleep(0.5)
        self.assertEqual(self.fake.snapshots, n)                         # anchored: moving does not re-query

    def test_following_dashboard_requeries_when_you_move(self):
        self.assertTrue(self.loaded())                                   # default test config still follows
        self.runner().gate.set_interval(0.1)
        n = self.fake.snapshots
        self.fake.position = "Achenar"
        self.runner()._origin_checked = 0.0
        self.assertTrue(wait_for(lambda: self.fake.snapshots > n))


class RadiusTests(ServerCase):
    def post(self, q, headers=None):
        code, _, body = self.get("/api/radius?d=recce" + q, method="POST", headers=headers)
        return code, json.loads(body)

    def test_radius_is_set_validated_and_pushed(self):
        self.assertTrue(self.loaded())
        self.assertEqual(self.post("&ly=60")[0], 404)                                  # header required
        self.assertEqual(self.post("&ly=60", {"X-Recce": "1"}), (200, {"name": "recce", "radius_ly": 60}))
        self.assertEqual(rcfg.load("recce")["radius_ly"], 60)
        self.assertTrue(wait_for(lambda: self.json("/api/config?d=recce")[1]["config"]["radius_ly"] == 60))
        self.assertEqual(self.post("&ly=37.5", {"X-Recce": "1"})[1]["radius_ly"], 37.5)
        code, body = self.post("&ly=0", {"X-Recce": "1"})
        self.assertEqual(code, 400)
        self.assertIn("radius_ly: must be at least 1", body["error"])
        self.assertEqual(self.post("&ly=abc", {"X-Recce": "1"})[0], 400)
        self.assertEqual(rcfg.load("recce")["radius_ly"], 37.5)                        # bad values changed nothing


class PlayerOnlyTests(ServerCase):
    def post(self, q, name="recce", headers=None):
        code, _, body = self.get(f"/api/player-only?d={name}{q}", method="POST", headers=headers)
        return code, json.loads(body)

    def test_toggle_writes_pushes_and_validates(self):
        self.assertTrue(self.loaded())
        self.assertEqual(self.post("&on=0")[0], 404)                                   # header required
        self.assertEqual(self.post("&on=0", headers={"X-Recce": "1"}), (200, {"name": "recce", "player_faction_only": False}))
        self.assertFalse(rcfg.load("recce")["filters"]["player_faction_only"])
        self.assertTrue(wait_for(lambda: self.json("/api/config?d=recce")[1]["config"]["filters"]["player_faction_only"] is False))
        self.assertEqual(self.json("/api/config?d=recce")[1]["labels"]["faction"], "Faction")
        self.assertEqual(self.post("&on=1", headers={"X-Recce": "1"})[1]["player_faction_only"], True)
        self.assertEqual(self.json("/api/config?d=recce")[1]["labels"]["faction"] if wait_for(
            lambda: self.json("/api/config?d=recce")[1]["labels"]["faction"].startswith("Player")) else "", "Player controlling faction")
        self.assertEqual(self.post("&on=maybe", headers={"X-Recce": "1"})[0], 400)
        self.assertEqual(self.post("", headers={"X-Recce": "1"})[0], 400)
        self.assertEqual(self.post("&on=0", name="nope", headers={"X-Recce": "1"})[0], 404)
        self.assertTrue(rcfg.load("recce")["filters"]["player_faction_only"])         # bad calls changed nothing


class StatusTests(unittest.TestCase):
    CMD = r"C:\x\.venv\Scripts\python.exe -m eddn_helper.recce serve --quiet"

    def table(self):
        return [{"pid": 10, "parent": 1, "started": "t1", "cmd": self.CMD},                # server A launcher
                {"pid": 11, "parent": 10, "started": "t1", "cmd": self.CMD},               # server A interpreter
                {"pid": 20, "parent": 2, "started": "t2", "cmd": self.CMD},                # server B launcher
                {"pid": 21, "parent": 20, "started": "t2", "cmd": self.CMD},               # server B interpreter
                {"pid": 30, "parent": 1, "started": "t3", "cmd": "python -m eddn_helper.recce list"},
                {"pid": 31, "parent": 1, "started": "t3", "cmd": "notepad.exe"}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {rcfg.ENV_DIR: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def register(self, pid):
        server.info_path().write_text(json.dumps({"pid": pid, "port": 1, "started": "x"}), encoding="utf-8")

    def test_pairs_count_as_one_server_and_only_serve_commands_count(self):
        groups = server.find_servers(self.table())
        self.assertEqual([(g["root"], g["pids"]) for g in groups], [(10, [10, 11]), (20, [20, 21])])

    def test_registered_server_is_not_a_stray(self):
        self.register(21)                                      # server.json holds the interpreter pid
        self.assertEqual([(g["root"], g["registered"]) for g in server.find_servers(self.table())],
                         [(10, False), (20, True)])

    def test_no_server_file_makes_every_server_a_stray(self):
        self.assertTrue(all(not g["registered"] for g in server.find_servers(self.table())))

    def test_cli_status_reports_and_kills_only_strays(self):
        from eddn_helper.recce import cli
        self.register(11)
        table = self.table()
        out = io.StringIO()
        running = {"base_url": "http://127.0.0.1:1", "pid": 11, "port": 1}
        with mock.patch.object(server, "_process_table", side_effect=lambda: table), \
                mock.patch.object(server, "running_server", return_value=running), \
                mock.patch.object(server, "kill_server") as kill, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 1)                       # strays found, nothing killed
            kill.assert_not_called()
            self.assertIn("STRAY", out.getvalue())
            self.assertIn("server processes found: 2", out.getvalue())
            real_find = server.find_servers
            after = [g for g in real_find(table) if g["registered"]]
            with mock.patch.object(server, "find_servers", side_effect=[real_find(table), after]):
                self.assertEqual(cli.main(["status", "--kill-strays"]), 0)
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0]["root"], 20)              # the registered one (10) is left alone

    def test_real_process_table_is_readable(self):
        rows = server._process_table()
        self.assertTrue(any(r["pid"] == os.getpid() for r in rows))


class SingletonTests(ServerCase):
    def test_second_launch_reuses_the_first(self):
        self.assertIsNone(server.running_server())                      # server.json not written yet
        server._write_info(self.srv)
        found = server.running_server()
        self.assertEqual(found["port"], self.port)
        with mock.patch.object(server.subprocess, "Popen") as popen:
            info = server.ensure_server()
        popen.assert_not_called()
        self.assertEqual((info["port"], info["started"]), (self.port, False))
        lines = []
        self.assertEqual(server.serve(out=lines.append), 0)
        self.assertIn("already running", lines[0])
        self.assertEqual(server.dashboard_url(info["base_url"], "recce"), f"http://127.0.0.1:{self.port}/?d=recce")

    def test_stale_server_file_is_ignored(self):
        server.info_path().write_text(json.dumps({"pid": 1, "port": 9, "started": "x"}), encoding="utf-8")
        self.assertIsNone(server.running_server())

    def test_stop_server_shuts_it_down_and_removes_the_file(self):
        server._write_info(self.srv)
        self.assertTrue(server.stop_server())
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(server.info_path().exists())
        self.assertFalse(server.stop_server())


if __name__ == "__main__":
    unittest.main()
