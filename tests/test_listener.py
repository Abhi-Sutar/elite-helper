import json
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


if __name__ == "__main__":
    unittest.main()
