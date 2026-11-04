"""
The coriolis-data catalogue: ships, modules and engineering blueprints, downloaded once and cached as one JSON file.

Source: EDCD/coriolis-data (github.com/EDCD/coriolis-data). Cache: <data dir>/coriolis-data/catalogue.json. A cache older than
MAX_AGE_DAYS is refreshed when possible; if the download fails the old copy is still used and the reply says how old it is.
"""

import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import config

URL = "https://codeload.github.com/EDCD/coriolis-data/zip/refs/heads/master"
USER_AGENT = "elite-helper/0.1 (personal ship build tool)"
MAX_AGE_DAYS = 30
# Fields worth keeping from the module files; the rest (descriptions, costs of weapons, ids) only costs space.
DROP = {"ukDiscript", "eddbID", "id", "edID", "integrity"}

# Journal ship key -> coriolis file stem, for the keys that differ after stripping punctuation.
ALIASES = {
    "type9": "type_9_heavy", "type7": "type_7_transport", "type6": "type_6_transporter", "type8": "type_8_transport",
    "type9_military": "type_10_defender", "typex": "alliance_chieftain", "typex_2": "alliance_crusader",
    "typex_3": "alliance_challenger", "cutter": "imperial_cutter", "empire_trader": "imperial_clipper",
    "empire_courier": "imperial_courier", "empire_eagle": "imperial_eagle", "ferdelance": "fer_de_lance",
    "federation_dropship": "federal_dropship", "federation_dropship_mkii": "federal_assault_ship",
    "federation_gunship": "federal_gunship", "federation_corvette": "federal_corvette",
    "independant_trader": "keelback", "belugaliner": "beluga", "diamondbackxl": "diamondback_explorer",
    "diamondback": "diamondback_scout", "krait_light": "krait_phantom", "lakonminer": "type_11_prospector",
    "viper_mkiv": "viper_mk_iv", "cobramkiii": "cobra_mk_iii", "cobramkiv": "cobra_mk_iv", "cobramkv": "cobra_mk_v",
    "panthermkii": "panther_clipper", "empire_corsair": "imperial_corsair", "corsair": "imperial_corsair",
    "kestrel_mkii": "kestrel", "asp": "asp", "asp_scout": "asp_scout",
}


class CatalogueError(Exception):
    pass


def cache_path():
    return config.data_dir() / "coriolis-data" / "catalogue.json"


def _norm(key):
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _slim(entry):
    return {k: v for k, v in entry.items() if k not in DROP and not k.startswith("uk")} | (
        {"name": entry["ukName"]} if "ukName" in entry and "name" not in entry else {})


def build_catalogue(zip_bytes):
    """Fold the repo zip into {ships: {stem: ship}, modules: {symbol_lower: module}, blueprints: {...}}."""
    ships, modules, blueprints = {}, {}, {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            parts = name.split("/", 1)[-1]          # drop the 'coriolis-data-master/' prefix
            if not parts.endswith(".json"):
                continue
            if parts.startswith("ships/"):
                stem = Path(parts).stem
                doc = json.loads(z.read(name))
                key, ship = next(iter(doc.items()))
                ships[stem] = {"stem": stem, "key": key, "properties": ship["properties"], "slots": ship["slots"],
                               "bulkheads": ship.get("bulkheads", [])}
            elif parts.startswith("modules/") and not parts.endswith("index.js"):
                for group, entries in json.loads(z.read(name)).items():
                    if not isinstance(entries, list):
                        continue
                    for e in entries:
                        if isinstance(e, dict) and e.get("symbol") and e.get("grp") != "mm" and e.get("rating") != "Z":
                            modules.setdefault(e["symbol"].lower(), _slim(e))
            elif parts == "modifications/blueprints.json":
                doc = json.loads(z.read(name))
                for bp, v in doc.items():
                    blueprints[bp] = {g: d.get("features", {}) for g, d in v.get("grades", {}).items()}
    if not ships or not modules:
        raise CatalogueError("the downloaded archive does not look like coriolis-data (no ships or modules)")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": URL,
            "ships": ships, "modules": modules, "blueprints": blueprints}


def download():
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise CatalogueError(f"could not download coriolis-data: {e}") from e


def refresh(fetch=download):
    cat = build_catalogue(fetch())
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return cat


class Catalogue:
    def __init__(self, raw):
        self.raw = raw
        self.ships = raw["ships"]
        self.modules = raw["modules"]
        self.blueprints = raw.get("blueprints", {})
        self._by_norm = {}
        for stem, s in self.ships.items():
            self._by_norm.setdefault(_norm(stem), stem)
            self._by_norm.setdefault(_norm(s["key"]), stem)

    @property
    def age_days(self):
        try:
            then = datetime.fromisoformat(self.raw["fetched_at"])
        except (KeyError, ValueError):
            return None
        return (datetime.now(timezone.utc) - then).total_seconds() / 86400

    def module(self, symbol):
        return self.modules.get(str(symbol or "").lower())

    def ship(self, key):
        """Ship by journal key ('panthermkii', 'type9'), coriolis stem or name; None if unknown."""
        k = str(key or "").strip().lower()
        stem = ALIASES.get(k) or ALIASES.get(_norm(k))
        if stem in self.ships:
            return self.ships[stem]
        stem = self._by_norm.get(_norm(k))
        if stem:
            return self.ships[stem]
        for s in self.ships.values():
            if _norm(s["properties"]["name"]) == _norm(k):
                return s
        return None

    def group(self, grp):
        return [m for m in self.modules.values() if m.get("grp") == grp]

    def status(self):
        return {"path": str(cache_path()), "fetched_at": self.raw.get("fetched_at"),
                "age_days": round(self.age_days, 1) if self.age_days is not None else None,
                "ships": len(self.ships), "modules": len(self.modules), "source": self.raw.get("source")}


_cache = {"cat": None}


def load(refresh_now=False, max_age_days=MAX_AGE_DAYS, fetch=download):
    """The catalogue, downloading it when missing, stale or `refresh_now`. A failed refresh falls back to the old copy."""
    path = cache_path()
    cat = None if refresh_now else _cache["cat"]
    if cat is None and not refresh_now and path.exists():
        try:
            cat = Catalogue(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            cat = None
    if cat is not None and not refresh_now and (cat.age_days is None or cat.age_days <= max_age_days):
        _cache["cat"] = cat
        return cat
    try:
        _cache["cat"] = Catalogue(refresh(fetch))
    except CatalogueError:
        if cat is None:
            raise
        _cache["cat"] = cat          # stale but usable
    return _cache["cat"]
