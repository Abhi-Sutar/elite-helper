import inspect
import json
import threading
import unittest
import zlib
from unittest import mock

from eddn_helper import listener, store
from eddn_helper.models import Market
from helpers import new_session
from test_eddn_helper import commodity_msg


def frame(market_id, system="Sol"):
    return zlib.compress(json.dumps(commodity_msg(market_id, "Hub", system, [("gold", 9000, 50, 0, 0)])).encode())


class ListenerBatchTests(unittest.TestCase):
    def setUp(self):
        listener._last_logged.clear()
        self.s = new_session()
        self.addCleanup(self.s.close)
        self.stats = listener.new_stats()

    def market_ids(self):
        self.s.expire_all()
        return sorted(m.market_id for m in self.s.query(Market))

    def test_a_bad_message_does_not_lose_the_rest_of_the_batch(self):
        frames = [frame(1), frame(2), frame(3)]
        real = store.apply_parsed

        def fail_on_two(session, parsed):
            if parsed["market"]["market_id"] == 2:
                raise RuntimeError("boom")
            return real(session, parsed)

        with mock.patch.object(store, "apply_parsed", fail_on_two), self.assertLogs("eddn_helper", "ERROR"):
            kept = [f for f in frames if listener.apply_one(self.s, f, self.stats)]
        self.assertTrue(listener.commit_batch(self.s, kept, self.stats, 0))
        self.assertEqual(self.market_ids(), [1, 3])
        self.assertEqual(self.stats["errors"], 1)

    def test_failed_commit_is_replayed_once(self):
        frames = [frame(1), frame(2)]
        for f in frames:
            listener.apply_one(self.s, f, self.stats)
        real = self.s.commit
        calls = []

        def fail_first(*a):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("connection lost")
            return real()

        with mock.patch.object(self.s, "commit", fail_first), self.assertLogs("eddn_helper", "ERROR"):
            self.assertTrue(listener.commit_batch(self.s, frames, self.stats, 0))
        self.assertEqual(self.market_ids(), [1, 2])
        self.assertEqual((self.stats["rollbacks"], self.stats["lost"]), (1, 0))

    def test_batch_is_lost_when_the_replay_fails_too(self):
        frames = [frame(1), frame(2)]
        for f in frames:
            listener.apply_one(self.s, f, self.stats)
        with mock.patch.object(self.s, "commit", side_effect=RuntimeError("down")), \
                self.assertLogs("eddn_helper", "ERROR"):
            self.assertFalse(listener.commit_batch(self.s, frames, self.stats, 0))
        self.assertEqual(self.market_ids(), [])
        self.assertEqual((self.stats["rollbacks"], self.stats["lost"]), (2, 2))


class LogFailureTests(unittest.TestCase):
    def setUp(self):
        listener._last_logged.clear()

    def log_one(self, exc):
        try:
            raise exc
        except Exception:
            listener.log_failure("failed")

    def test_one_traceback_per_exception_type_per_window(self):
        with self.assertLogs("eddn_helper", "ERROR") as logs:
            self.log_one(ValueError())
            self.log_one(ValueError())
            self.log_one(KeyError())
        self.assertEqual(len(logs.records), 2)

    def test_logs_again_after_the_window(self):
        with self.assertLogs("eddn_helper", "ERROR") as logs, \
                mock.patch.object(listener.time, "monotonic", side_effect=[1000, 1000 + listener.LOG_FAILURE_EVERY_S]):
            self.log_one(ValueError())
            self.log_one(ValueError())
        self.assertEqual(len(logs.records), 2)


class MaintenanceTests(unittest.TestCase):
    """The maintenance worker with stub tasks and a fake clock (no real concurrency: in-memory SQLite shares one connection)."""

    def setUp(self):
        listener._last_logged.clear()
        self.s = new_session()
        self.addCleanup(self.s.close)
        self.now = [1_000_000.0]
        self.calls = []
        self.results = {}
        tasks = {name: (every, self.stub(name)) for name, (every, _) in listener.MAINT_TASKS.items()}
        patcher = mock.patch.dict(listener.MAINT_TASKS, tasks, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub(self, name):
        def step(session, deadline):
            self.calls.append(name)
            result = self.results.get(name, ({}, True))
            if isinstance(result, Exception):
                raise result
            return result
        return step

    def worker(self):
        return listener.Maintenance(lambda: self.s, clock=lambda: self.now[0])

    def test_nothing_runs_at_start_up(self):
        self.worker().run_due()
        self.assertEqual(self.calls, [])

    def test_tasks_run_when_their_interval_has_passed(self):
        m = self.worker()
        self.now[0] += listener.LINK_EVERY_S + 1
        m.run_due()
        self.assertEqual(self.calls, ["link", "prune"])
        self.now[0] += listener.PRUNE_PRICES_EVERY_S - listener.LINK_EVERY_S
        m.run_due()
        self.assertEqual(self.calls, ["link", "prune", "link", "prune", "prune_prices"])

    def test_last_run_is_remembered_across_restarts(self):
        first, restarted = self.worker(), self.worker()   # both started an hour before the first run
        self.now[0] += listener.LINK_EVERY_S + 1
        first.run_due()
        self.calls.clear()
        restarted.run_due()                 # without the stored last run it would be due now
        self.assertEqual(self.calls, [])
        self.assertEqual(int(store.get_meta(self.s, "maint_last_link")), int(self.now[0]))

    def test_a_daily_task_still_fires_after_a_restart_when_it_is_overdue(self):
        store.set_meta(self.s, "maint_last_prune_prices", int(self.now[0]) - listener.PRUNE_PRICES_EVERY_S - 1)
        self.s.commit()
        self.worker().run_due()
        self.assertEqual(self.calls, ["prune_prices"])

    def test_a_failing_task_is_logged_and_retried_next_interval_without_stopping_the_others(self):
        self.results["link"] = RuntimeError("boom")
        m = self.worker()
        self.now[0] += listener.LINK_EVERY_S + 1
        with self.assertLogs("eddn_helper", "ERROR"):
            m.run_due()
        self.assertEqual(self.calls, ["link", "prune"])
        self.assertEqual(m.take_stats()["errors"], 1)
        self.assertEqual(m.take_stats()["errors"], 0)
        self.now[0] += 100
        m.run_due()
        self.assertEqual(self.calls, ["link", "prune"])
        self.now[0] += listener.LINK_EVERY_S
        self.results["link"] = ({"linked": 3}, True)
        m.run_due()
        self.assertEqual(self.calls.count("link"), 2)
        self.assertEqual(m.take_stats()["linked"], 3)

    def test_a_task_that_ran_out_of_budget_is_resumed_soon_and_not_marked_done(self):
        self.results["prune"] = ({"market_update": 5}, False)
        m = self.worker()
        self.now[0] += listener.PRUNE_EVERY_S + 1
        m.run_due()
        self.assertEqual(self.calls.count("prune"), 1)
        self.assertIsNone(store.get_meta(self.s, "maint_last_prune"))
        self.now[0] += listener.MAINT_RETRY_S - 1
        m.run_due()
        self.assertEqual(self.calls.count("prune"), 1)
        self.now[0] += 2
        self.results["prune"] = ({}, True)
        m.run_due()
        self.assertEqual(self.calls.count("prune"), 2)
        self.assertIsNotNone(store.get_meta(self.s, "maint_last_prune"))
        self.assertEqual(m.take_stats()["removed"], 5)

    def test_the_worker_thread_survives_an_error_and_stops_promptly(self):
        m = self.worker()
        seen, two_calls = [], threading.Event()

        def tick():
            seen.append(threading.current_thread().name)
            if len(seen) == 1:
                raise RuntimeError("boom")
            two_calls.set()

        with mock.patch.object(listener, "MAINT_TICK_S", 0.01), mock.patch.object(m, "run_due", tick), \
                self.assertLogs("eddn_helper", "ERROR"):
            m.start()
            self.assertTrue(two_calls.wait(5))
            m.stop()
        self.assertFalse(m._thread.is_alive())
        self.assertEqual(set(seen), {"maintenance"})

    def test_the_receive_loop_no_longer_prunes_or_links(self):
        src = inspect.getsource(listener.run)
        for needle in ("store.prune", "link_markets_to_systems", "last_prune"):
            self.assertNotIn(needle, src)
        self.assertIn("maintenance.start()", src)


if __name__ == "__main__":
    unittest.main()
