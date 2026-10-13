"""The refresh rule: re-run the full query only when the database has newer data AND the minimum interval has passed.

That makes the effective refresh time the slower of "the database changed" and "N seconds since the last refresh".
The gate is a plain object driven by the caller's clock, so it is easy to test.
"""


class RefreshGate:
    def __init__(self, min_interval):
        self.min_interval = float(min_interval)
        self.last_token = None      # the change token seen at the last refresh
        self.last_at = None         # clock value of the last refresh
        self.pending = False        # a change was seen but the interval had not passed yet

    def set_interval(self, seconds):
        self.min_interval = float(seconds)

    def should_refresh(self, token, now, force=False):
        """token: anything comparable that moves when the data changes (e.g. the newest-update timestamps).
        True means "run the full query now"; the caller must then call done(token, now)."""
        if self.last_at is None:
            return True                                  # nothing loaded yet
        changed = token != self.last_token
        if changed:
            self.pending = True
        if not (self.pending or force):
            return False
        return now - self.last_at >= self.min_interval

    def done(self, token, now):
        self.last_token, self.last_at, self.pending = token, now, False

    def failed(self, now):
        """A refresh attempt failed: keep the old token so it is retried, but not before another full interval."""
        self.last_at, self.pending = now, True

    def seconds_until_due(self, now):
        """How long until a pending change may refresh (0 when nothing is waiting or it is already due)."""
        if self.last_at is None or not self.pending:
            return 0.0
        return max(0.0, self.min_interval - (now - self.last_at))
