"""The refresh rule: refresh only when the database moved AND the minimum interval has passed (the slower of the two)."""

import unittest

from eddn_helper.recce.refresh import RefreshGate


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class GateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = RefreshGate(20)

    def refresh(self, token):
        self.assertTrue(self.gate.should_refresh(token, self.clock()))
        self.gate.done(token, self.clock())

    def test_first_check_always_loads(self):
        self.refresh("t1")

    def test_unchanged_database_never_refreshes_however_long_we_wait(self):
        self.refresh("t1")
        for _ in range(50):
            self.clock.t += 60
            self.assertFalse(self.gate.should_refresh("t1", self.clock()))

    def test_change_after_the_interval_refreshes_at_once(self):
        self.refresh("t1")
        self.clock.t += 45
        self.assertTrue(self.gate.should_refresh("t2", self.clock()))

    def test_change_before_the_interval_waits_then_fires_at_the_interval(self):
        self.refresh("t1")
        self.clock.t += 5
        self.assertFalse(self.gate.should_refresh("t2", self.clock()))        # moved, but too soon
        self.assertEqual(self.gate.seconds_until_due(self.clock()), 15)
        self.clock.t += 10
        self.assertFalse(self.gate.should_refresh("t2", self.clock()))        # still too soon, change is remembered
        self.clock.t += 5
        self.assertTrue(self.gate.should_refresh("t2", self.clock()))         # exactly the interval
        self.gate.done("t2", self.clock())
        self.clock.t += 100
        self.assertFalse(self.gate.should_refresh("t2", self.clock()))        # nothing pending any more

    def test_a_change_that_reverts_inside_the_interval_still_counts_once_seen(self):
        self.refresh("t1")
        self.clock.t += 5
        self.gate.should_refresh("t2", self.clock())
        self.clock.t += 20
        self.assertTrue(self.gate.should_refresh("t1", self.clock()))        # remembered pending change

    def test_interval_can_change_live(self):
        self.refresh("t1")
        self.gate.set_interval(60)
        self.clock.t += 30
        self.assertFalse(self.gate.should_refresh("t2", self.clock()))
        self.clock.t += 30
        self.assertTrue(self.gate.should_refresh("t2", self.clock()))

    def test_failed_refresh_is_retried_after_a_full_interval_not_every_poll(self):
        self.refresh("t1")
        self.clock.t += 30
        self.assertTrue(self.gate.should_refresh("t2", self.clock()))
        self.gate.failed(self.clock())
        self.clock.t += 5
        self.assertFalse(self.gate.should_refresh("t2", self.clock()))
        self.clock.t += 15
        self.assertTrue(self.gate.should_refresh("t2", self.clock()))

    def test_force_still_waits_for_the_interval(self):
        self.refresh("t1")
        self.clock.t += 3
        self.assertFalse(self.gate.should_refresh("t1", self.clock(), force=True))
        self.clock.t += 20
        self.assertTrue(self.gate.should_refresh("t1", self.clock(), force=True))


if __name__ == "__main__":
    unittest.main()
