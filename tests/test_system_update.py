"""upsert_system writes factions_updated_at in the same statement as the system fields (one UPDATE per message)."""
from sqlalchemy import event, select

from eddn_helper import store
from eddn_helper.models import System
from test_eddn_helper import jump_msg
from test_traffic import ChangesCase, at, fac, sys_msg


def no_factions(when):
    m = jump_msg("Sol", 1, [0, 0, 0], when=when)
    del m["message"]["Factions"]
    return m


class SystemUpdateTests(ChangesCase):
    def setUp(self):
        super().setUp()
        self.updates = []
        engine = self.s.get_bind()
        event.listen(engine, "before_cursor_execute", self.capture)
        self.addCleanup(event.remove, engine, "before_cursor_execute", self.capture)

    def capture(self, conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("UPDATE SYSTEMS"):
            self.updates.append(statement)

    def stamps(self):
        r = self.s.execute(select(System.updated_at, System.factions_updated_at)).one()
        return r.updated_at, r.factions_updated_at

    def test_new_system_needs_no_update_and_has_stamp(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(5)))
        self.assertEqual(self.updates, [])
        updated, fac_ts = self.stamps()
        self.assertEqual(updated, fac_ts)

    def test_existing_system_uses_a_single_update(self):
        self.feed(sys_msg([fac("A", 0.6), fac("B", 0.4)], at(5)))
        self.feed(sys_msg([fac("A", 0.5), fac("B", 0.5)], at(2)))
        self.assertEqual(len(self.updates), 1)
        updated, fac_ts = self.stamps()
        self.assertEqual(fac_ts, updated)

    def test_message_without_factions_leaves_stamp_alone(self):
        self.feed(sys_msg([fac("A", 0.6)], at(5)))
        _, before = self.stamps()
        self.feed(no_factions(at(2)))
        updated, after = self.stamps()
        self.assertEqual(after, before)
        self.assertGreater(updated, after)

    def test_factions_older_than_system_row_still_advance_stamp(self):
        self.feed(sys_msg([fac("A", 0.6)], at(10)))
        self.feed(no_factions(at(2)))  # system row newer, factions snapshot older
        self.updates.clear()
        self.feed(sys_msg([fac("A", 0.7)], at(5)))
        self.assertEqual(len(self.updates), 1)
        updated, fac_ts = self.stamps()
        self.assertGreater(updated, fac_ts)
        self.assertEqual(fac_ts.replace(tzinfo=None), store.to_dt(at(5)).replace(tzinfo=None))

    def test_late_faction_upload_does_not_move_stamp(self):
        self.feed(sys_msg([fac("A", 0.6)], at(2)))
        _, before = self.stamps()
        self.updates.clear()
        self.feed(sys_msg([fac("A", 0.9)], at(8)))
        self.assertEqual(self.updates, [])
        self.assertEqual(self.stamps()[1], before)
