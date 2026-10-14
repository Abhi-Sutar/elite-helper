"""The templates in config-templates/ stay valid: dashboards pass the real validator, settings use known keys, and
every environment variable the code reads is documented there."""
import json
import re
import unittest
from pathlib import Path

from eddn_helper.recce import config as rcfg

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "config-templates"


def load(rel):
    return json.loads((TEMPLATES / rel).read_text(encoding="utf-8"))


class TemplateTests(unittest.TestCase):
    def test_dashboard_examples_validate(self):
        for rel, kind in (("dashboards/recce.json.example", "recce"), ("dashboards/cg.json.example", "cg")):
            raw = load(rel)
            cfg = rcfg.validate(raw)
            self.assertEqual(cfg.get("kind", "recce"), kind, rel)
            self.assertEqual(cfg["name"], rel.split("/")[1].split(".")[0])

    def test_settings_example_uses_known_keys(self):
        self.assertEqual(set(load("settings.json.example")), {"database_url", "ingest_database_url"})

    def test_pgpass_example_has_the_libpq_format(self):
        lines = [l for l in (TEMPLATES / "pgpass.conf.example").read_text(encoding="utf-8").splitlines()
                 if l and not l.startswith("#")]
        self.assertTrue(lines)
        for l in lines:
            self.assertEqual(len(l.split(":")), 5, l)

    def test_every_environment_variable_in_the_code_is_documented(self):
        used = set()
        for p in (ROOT / "eddn_helper").rglob("*.py"):
            used |= set(re.findall(r"\b(ELITE_(?:HELPER_)?[A-Z_]+)\b", p.read_text(encoding="utf-8")))
        used = {u for u in used if not u.endswith("_")}
        docs = (TEMPLATES / "README.md").read_text(encoding="utf-8")
        missing = sorted(u for u in used if u not in docs)
        self.assertEqual(missing, [], "document these in config-templates/README.md")

    def test_templates_are_not_gitignored(self):
        rules = [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        self.assertFalse([r for r in rules if "config-templates" in r])
        for p in TEMPLATES.rglob("*"):
            if p.is_file():
                self.assertNotRegex(p.name, r"\.json$", "templates must end in .example so *.json patterns skip them")


if __name__ == "__main__":
    unittest.main()
