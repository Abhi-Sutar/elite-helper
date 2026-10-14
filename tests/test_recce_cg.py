"""The "cg" dashboard kind: config validation, the snapshot (large-pad filter, EDSM lookups, baselines), the legacy
import, and the shared server serving it."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from eddn_helper import parsers, store
from eddn_helper.recce import cg, kinds, server
from eddn_helper.recce import config as rcfg
from eddn_helper.recce.core import Core, CoreError
from recce_helpers import RecceDb, plain
from test_eddn_helper import commodity_msg
from test_recce_server import ServerCase, wait_for
from test_traffic import at

CFG = {"kind": "cg", "name": "cg", "target": {"name": "Hub Station", "system": "Hub"},
       "ship": {"name": "S", "laden_ly": 30, "unladen_ly": 40, "cargo": 100}, "refresh_seconds": 5,
       "tabs": [{"key": "gold", "label": "Gold", "commodity": "gold", "cg_price": 500, "max_jumps": 2}]}


def config(**over):
    return json.loads(json.dumps({**CFG, **over}))


class ConfigTests(unittest.TestCase):
    def test_defaults_are_filled_and_kind_is_kept(self):
        cfg = rcfg.validate(config())
        self.assertEqual((cfg["kind"], cfg["max_age_hours"], cfg["station_types"][0]), ("cg", 72, "Coriolis"))
        self.assertIs(kinds.for_cfg(cfg), cg.KIND)

    def test_recce_default_is_unchanged(self):
        self.assertNotIn("kind", rcfg.validate({"name": "x"}))
        self.assertNotIn("kind", rcfg.validate({"name": "x", "kind": "recce"}))

    def test_unknown_kind(self):
        with self.assertRaises(rcfg.ConfigError) as ctx:
            rcfg.validate({"kind": "nope"})
        self.assertIn("kind: must be one of recce, cg", ctx.exception.errors[0])

    def test_every_problem_is_listed(self):
        bad = config(tabs=[{"key": "A b", "commodity": ""}, {"key": "gold", "commodity": "x", "max_jumps": 0},
                           {"key": "gold", "commodity": "y", "oops": 1}])
        bad["ship"]["cargo"] = -1
        bad["target"]["system"] = ""
        with self.assertRaises(rcfg.ConfigError) as ctx:
            rcfg.validate(bad)
        text = " | ".join(ctx.exception.errors)
        for needle in ("tabs[0].key", "tabs[0].commodity", "tabs[1].max_jumps", "tabs[2].key", "tabs[2].oops",
                       "ship.cargo", "target.system"):
            self.assertIn(needle, text)

    def test_needs_tabs(self):
        with self.assertRaises(rcfg.ConfigError) as ctx:
            rcfg.validate({"kind": "cg"})
        self.assertIn("tabs", ctx.exception.errors[0])

    def test_recce_keys_are_rejected_on_a_cg_dashboard(self):
        with self.assertRaises(rcfg.ConfigError):
            rcfg.validate(config(radius_ly=50))


class StoredTests(RecceDb):
    def test_save_load_update_and_kind_cannot_change(self):
        rcfg.save(config())
        self.assertEqual(rcfg.kind_of("cg"), "cg")
        cfg = rcfg.update("cg", {"target": {"system": "Elsewhere"}})
        self.assertEqual((cfg["target"]["system"], cfg["target"]["name"]), ("Elsewhere", "Hub Station"))
        with self.assertRaises(rcfg.ConfigError):
            rcfg.update("cg", {"kind": "recce"})

    def test_save_new_does_not_pin_a_cg_target(self):
        cfg, note = rcfg.save_new(config(target={"name": "x", "system": "current"}),
                                  where=lambda: {"system": "Sol"})
        self.assertEqual((cfg["target"]["system"], note), ("current", None))

    def test_kind_of_defaults_to_recce(self):
        rcfg.ensure_default()
        self.assertEqual(rcfg.kind_of("recce"), "recce")

    def test_recentre_moves_the_cg_target(self):
        rcfg.save(config())
        res = Core(None, where=lambda: {"system": "Sol"}).recentre("cg")
        self.assertEqual((res["origin"], res["previous"]), ("Sol", "Hub"))
        self.assertEqual(rcfg.load("cg")["target"]["system"], "Sol")


class SnapshotTests(RecceDb):
    def setUp(self):
        super().setUp()
        self.put(1, "Hub", [(1, plain("A"))], pos=(0, 0, 0))
        self.put(2, "Near", [(1, plain("A"))], pos=(20, 0, 0))
        self.put(3, "Far", [(1, plain("A"))], pos=(500, 0, 0))
        self.feed("Alpha Port", 11, "Near", 1000, station_type="Orbis")
        self.feed("Beta Outpost", 12, "Near", 2000, station_type="Outpost")
        self.feed("Gamma Mystery", 13, "Near", 3000, station_type=None)
        self.feed("Delta Mystery", 14, "Hub", 4000, station_type=None)
        self.feed("Far Port", 15, "Far", 5000, station_type="Orbis")
        self.core = Core(lambda: self.factory)
        rcfg.save(config())
        self.cfg = rcfg.load("cg")

    def feed(self, station, market_id, system, stock, station_type):
        msg = commodity_msg(market_id, station, system, [("gold", 100, stock, 0, 0)], when=at(1))
        if station_type is None:
            msg["message"].pop("stationType")
        else:
            msg["message"]["stationType"] = station_type
        s = self.factory()
        try:
            store.apply_parsed(s, parsers.parse_message(msg))
            s.commit()
        finally:
            s.close()

    def snap(self, answers=None, cfg=None, **kw):
        asked = []

        def fetch(system):
            asked.append(system)
            if answers is None:
                raise OSError("offline")
            return {"stations": answers.get(system, [])}
        out = cg.snapshot(self.core, cfg or self.cfg, fetch=fetch, **kw)
        return out, asked

    def test_known_large_pads_listed_and_distance_limited(self):
        out, _ = self.snap({})
        rows = out["tabs"]["gold"]["rows"]
        self.assertIn("Alpha Port", rows)
        self.assertNotIn("Beta Outpost", rows)         # known small pad
        self.assertNotIn("Far Port", rows)             # 500 ly away
        self.assertEqual((rows["Alpha Port"]["ly"], rows["Alpha Port"]["stock"], rows["Alpha Port"]["price"]),
                         (20.0, 1000, 100))
        self.assertFalse(rows["Alpha Port"]["pad_unknown"])
        self.assertEqual((out["tabs"]["gold"]["max_ly"], out["tabs"]["gold"]["max_jumps"]), (60.0, 2))
        self.assertIn("newest_market_update", out["db"])

    def test_unknown_type_is_resolved_through_edsm(self):
        answers = {"Near": [{"name": "Gamma Mystery", "type": "Coriolis Starport", "distanceToArrival": 321.4}],
                   "Hub": [{"name": "Delta Mystery", "type": "Planetary Outpost", "distanceToArrival": 5}]}
        out, _ = self.snap(answers)
        rows = out["tabs"]["gold"]["rows"]
        self.assertEqual((rows["Gamma Mystery"]["ls"], rows["Gamma Mystery"]["pad_unknown"]), (321, False))
        self.assertNotIn("Delta Mystery", rows)        # EDSM says outpost: dropped

    def test_unknown_type_is_kept_but_flagged_when_edsm_does_not_answer(self):
        out, asked = self.snap(None)
        rows = out["tabs"]["gold"]["rows"]
        self.assertTrue(rows["Gamma Mystery"]["pad_unknown"])
        self.assertIsNone(rows["Gamma Mystery"]["ls"])
        self.assertTrue(rows["Delta Mystery"]["pad_unknown"])
        self.assertEqual(sorted(set(asked)), ["Hub", "Near"])
        out, asked = self.snap(None)                   # a failed lookup is not retried at once
        self.assertEqual(asked, [])

    def test_lookup_ls_off_never_asks_edsm(self):
        out, asked = self.snap({}, cfg={**self.cfg, "lookup_ls": False})
        self.assertEqual(asked, [])
        self.assertTrue(out["tabs"]["gold"]["rows"]["Gamma Mystery"]["pad_unknown"])

    def test_cg_price_hides_stations_priced_at_or_above_it(self):
        cfg = {**self.cfg, "tabs": [{**self.cfg["tabs"][0], "cg_price": 100}]}
        out, _ = self.snap({}, cfg=cfg)
        self.assertEqual(out["tabs"]["gold"]["rows"], {})

    def test_baseline_tracks_stock_and_resets_when_the_target_moves(self):
        out, _ = self.snap({})
        self.assertEqual(out["tabs"]["gold"]["rows"]["Alpha Port"]["base"], 1000)
        self.feed("Alpha Port", 11, "Near", 400, station_type="Orbis")
        out, _ = self.snap({})
        row = out["tabs"]["gold"]["rows"]["Alpha Port"]
        self.assertEqual((row["stock"], row["base"]), (400, 1000))     # Change = -600 on the page
        cg.reset_baselines("cg", "gold")
        out, _ = self.snap({})
        self.assertEqual(out["tabs"]["gold"]["rows"]["Alpha Port"]["base"], 400)
        self.feed("Alpha Port", 11, "Near", 250, station_type="Orbis")
        moved = {**self.cfg, "target": {"name": "Hub Station", "system": "Near"}}
        out, _ = self.snap({}, cfg=moved)
        self.assertEqual(out["tabs"]["gold"]["rows"]["Alpha Port"]["base"], 250)   # fresh baseline for the new target

    def test_current_target_follows_the_journal(self):
        core = Core(lambda: self.factory, where=lambda: {"system": "Near"})
        cfg = {**self.cfg, "target": {"name": "x", "system": "current"}}
        out = cg.snapshot(core, cfg, fetch=lambda s: {"stations": []})
        self.assertEqual(out["config"]["target"]["system"], "Near")
        self.assertTrue(cg.KIND.follows_position(cfg))

    def test_unknown_target_system_is_an_error_with_a_message(self):
        cfg = {**self.cfg, "target": {"name": "x", "system": "Nowhere"}}
        with self.assertRaises(CoreError) as ctx:
            cg.snapshot(self.core, cfg, fetch=lambda s: {"stations": []})
        self.assertIn("gold", str(ctx.exception))


class LegacyImportTests(RecceDb):
    def test_import_keeps_everything_and_backs_the_old_files_up(self):
        old = Path(self.tmp.name) / "old"
        old.mkdir()
        legacy = {"title": "T", "port": 8765, "helper_dir": "x", "database_url": "postgresql://u@h/d",
                  "refresh_seconds": 30, "target": CFG["target"], "ship": CFG["ship"], "tabs": CFG["tabs"]}
        (old / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        (old / "baseline.json").write_text(json.dumps({"gold": {"baselineAt": "2026-01-01T00:00:00Z",
                                                              "base": {"A": 5}}}), encoding="utf-8")
        (old / "ls_cache.json").write_text(json.dumps({"S|A": {"ls": 7}}), encoding="utf-8")
        saved = cg.import_legacy(old)
        self.assertEqual((saved["kind"], saved["title"], saved["target"]["system"]), ("cg", "T", "Hub"))
        for k in ("port", "helper_dir", "database_url"):
            self.assertNotIn(k, saved)
        self.assertEqual(cg.load_json(cg.baseline_path("cg"), {})["gold"]["base"], {"A": 5})
        self.assertEqual(cg.load_json(cg.baseline_path("cg"), {})[cg.STATE_KEY], "Hub")
        self.assertEqual(cg.load_json(cg.ls_cache_path(), {})["S|A"], {"ls": 7})
        self.assertTrue((cg.state_dir() / "legacy" / "config.json").is_file())
        with self.assertRaises(rcfg.ConfigError):
            cg.import_legacy(old)                       # already exists
        cg.import_legacy(old, force=True)

    def test_invalid_old_config_writes_nothing(self):
        old = Path(self.tmp.name) / "old"
        old.mkdir()
        (old / "config.json").write_text(json.dumps({"tabs": []}), encoding="utf-8")
        with self.assertRaises(rcfg.ConfigError):
            cg.import_legacy(old)
        self.assertFalse(rcfg.exists("cg"))
        self.assertFalse((cg.state_dir() / "legacy").exists())


class ServerTests(ServerCase):
    def setUp(self):
        super().setUp()
        rcfg.save(config())
        self.market_stamp = server.iso(server.now_utc())
        self.fake.stamps = lambda: {"newest_faction_update": self.fake.stamp, "newest_market_update": self.market_stamp}
        self.snap = {"dashboard": "cg", "generated_at": "2026-10-13T10:00:05Z", "updated": "2026-10-13T10:00:05Z",
                     "order": ["gold"], "db": {"newest_market_update": self.market_stamp, "newest_faction_update": "f1"},
                     "config": {"title": "T", "target": {"name": "x", "system": "Hub"}, "ship": CFG["ship"],
                                "station_types": ["Orbis"]},
                     "tabs": {"gold": {"label": "Gold", "rows": {}}}}
        p = mock.patch.object(cg, "snapshot", side_effect=lambda core, cfg: self.snap)
        p.start()
        self.addCleanup(p.stop)

    def test_page_config_and_data_of_a_cg_dashboard(self):
        code, headers, body = self.get("/?d=cg")
        self.assertEqual(code, 200)
        self.assertIn(b"CG Supply Board", body)
        code, cfg = self.json("/api/config?d=cg")
        self.assertEqual((code, cfg["kind"], cfg["labels"]), (200, "cg", {}))
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=cg")[1].get("tabs") is not None))
        _, data = self.json("/api/data?d=cg")
        self.assertEqual((data["status"]["state"], data["status"]["db_newest"], data["status"]["error"]), ("live", self.market_stamp, None))
        self.assertEqual(self.json("/api/data?d=recce")[0], 200)       # recce still works beside it
        code, _, body = self.get("/?d=recce")
        self.assertIn(b"EventSource", body)
        self.assertNotIn(b"CG Supply Board", body)

    def test_page_wires_the_copy_button(self):
        """The copy buttons are rebuilt on every update, so the page needs one delegated click handler for them
        (it was lost once in the port from the standalone board: the button showed and did nothing)."""
        _, _, body = self.get("/?d=cg")
        page = body.decode("utf-8")
        self.assertIn('data-sys="', page)                                   # buttons are rendered with the system name
        self.assertIn('getElementById("tiers").addEventListener("click"', page)
        self.assertIn('closest(".copy")', page)
        self.assertIn("copyText(sys)", page)

    def test_refreshes_when_market_data_moves_and_on_config_edit(self):
        self.json("/api/config?d=cg")
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=cg")[1].get("tabs") is not None))
        self.snap = {**self.snap, "updated": "later"}
        self.fake.stamp = "unrelated-faction-change"            # faction stamps do not matter to a cg dashboard
        rcfg.update("cg", {"target": {"system": "Elsewhere"}})   # a config edit forces a fresh snapshot at once
        self.assertTrue(wait_for(lambda: self.json("/api/data?d=cg")[1].get("updated") == "later"))

    def test_listing_shows_the_kind(self):
        from eddn_helper.recce import tools
        items = {d["name"]: d for d in tools.list_dashboards()["dashboards"]}
        self.assertEqual((items["cg"]["kind"], items["cg"]["origin"]), ("cg", "Hub"))
        self.assertEqual(items["recce"]["kind"], "recce")


if __name__ == "__main__":
    unittest.main()
