"""Estimates whether a pending Expansion will go ahead, by running the rules in expansion_rules over stored data.

The rules (thresholds, weights, wording, order) live in expansion_rules.RULESET; this module holds the checks they
point at (CHECKS) and the code that combines the results. A check never guesses: missing data gives "unknown".
The percentage is a ranking heuristic built from community-derived mechanics, not a calibrated probability.
"""

from sqlalchemy import func, select

from .. import queries
from ..models import Faction, System
from .expansion_rules import RULESET

KINDS = ("gate", "modifier", "info")
RESULTS = ("pass", "fail", "unknown", "info")
CONFIDENCE = ("dev-stated", "community", "uncertain")
HAPPINESS_RANK = {"elated": 1, "happy": 2, "discontented": 3, "unhappy": 4, "despondent": 5}


def _res(result, detail):
    return {"result": result, "detail": detail}


# ---- per-refresh lookups, cached so rows sharing a system or faction do not repeat queries -----------------------

class Context:
    """Lookups the checks need. Make one per snapshot; it holds a database session and caches what it reads."""

    def __init__(self, session, params):
        self.s = session
        self.params = params
        self._system, self._factions, self._near, self._footprint = {}, {}, {}, {}

    def system(self, name):
        key = name.lower()
        if key not in self._system:
            self._system[key] = queries.find_system(self.s, name)
        return self._system[key]

    def faction(self, system_name, faction):
        """The stored Faction row for one faction in one system, or None."""
        sysrow = self.system(system_name)
        if sysrow is None:
            return None
        key = (sysrow.id64, faction.lower())
        if key not in self._factions:
            self._factions[key] = self.s.execute(select(Faction).where(
                Faction.system_id64 == sysrow.id64, func.lower(Faction.name) == faction.lower())).scalars().first()
        return self._factions[key]

    def neighbours(self, system_name, radius):
        """Populated systems within `radius` ly of the system, nearest first:
        [{"name", "distance_ly", "factions": count or None when the faction list was never stored}]."""
        key = (system_name.lower(), radius)
        if key in self._near:
            return self._near[key]
        origin = self.system(system_name)
        out = []
        if origin is not None and None not in (origin.x, origin.y, origin.z):
            box = self.s.execute(select(System).where(
                System.id64 != origin.id64, System.population > 0,
                System.x.between(origin.x - radius, origin.x + radius),
                System.y.between(origin.y - radius, origin.y + radius),
                System.z.between(origin.z - radius, origin.z + radius))).scalars().all()
            here = {"x": origin.x, "y": origin.y, "z": origin.z}
            near = []
            for r in box:
                d = queries.distance(here, {"x": r.x, "y": r.y, "z": r.z})
                if d is not None and d <= radius:
                    near.append((d, r))
            counts = {}
            ids = [r.id64 for _, r in near]
            for i in range(0, len(ids), 500):
                for sid, n in self.s.execute(select(Faction.system_id64, func.count()).where(
                        Faction.system_id64.in_(ids[i:i + 500])).group_by(Faction.system_id64)):
                    counts[sid] = n
            for d, r in sorted(near, key=lambda t: t[0]):
                known = r.factions_updated_at is not None or counts.get(r.id64)
                out.append({"name": r.name, "distance_ly": round(d, 1),
                            "factions": counts.get(r.id64, 0) if known else None})
        self._near[key] = out
        return out

    def footprint(self, faction):
        """Every stored system where this faction's influence is above the expansion threshold:
        [{"system", "influence_pct", "happiness"}]."""
        key = faction.lower()
        if key not in self._footprint:
            floor = self.params["influence_threshold_pct"] / 100
            rows = self.s.execute(select(Faction, System.name).join(System, System.id64 == Faction.system_id64).where(
                func.lower(Faction.name) == key, Faction.influence > floor)).all()
            self._footprint[key] = [{"system": n, "influence_pct": round(f.influence * 100, 1),
                                     "happiness": f.happiness} for f, n in rows]
        return self._footprint[key]


# ---- the checks --------------------------------------------------------------------------------------------------

def influence_above_threshold(ctx, row, params, rule):
    pct, need = row.get("influence_pct"), params["influence_threshold_pct"]
    if pct is None:
        return _res("unknown", "The faction's influence here is not stored.")
    if pct > need:
        return _res("pass", f"Influence is {pct:.1f}%, above the {need:g}% threshold.")
    return _res("fail", f"Influence is {pct:.1f}%, not above the {need:g}% threshold.")


def not_blocked_by_conflict(ctx, row, params, rule):
    blocking = {queries.norm_key(s) for s in params["blocking_states"]}
    fac = ctx.faction(row["system"], row["faction"])
    if fac is None:
        return _res("unknown", "The faction's states in this system are not stored.")
    states = [fac.state] + [s["state"] for s in queries._state_items(fac.active_states)]
    hit = sorted({s for s in states if s and queries.norm_key(s) in blocking})
    if hit:
        return _res("fail", f"The faction is in {', '.join(hit)} here, which holds up the countdown.")
    sysrow = ctx.system(row["system"])
    for c in (sysrow.conflicts if sysrow else None) or []:
        if isinstance(c, dict) and row["faction"].lower() in (str(c.get("faction1")).lower(), str(c.get("faction2")).lower()) \
                and queries.norm_key(c.get("type")) in blocking:
            return _res("fail", f"The faction is a party to a {c.get('type')} in this system.")
    return _res("pass", "No War, Civil War or Election involves the faction here.")


def target_in_range(ctx, row, params, rule):
    radius, cap = params["radius_ladder_ly"][0], params["max_factions"]
    if ctx.system(row["system"]) is None or ctx.system(row["system"]).x is None:
        return _res("unknown", "This system's position is not stored, so nearby systems cannot be found.")
    near = ctx.neighbours(row["system"], radius)
    open_ = [n for n in near if n["factions"] is not None and n["factions"] < cap]
    unknown = [n for n in near if n["factions"] is None]
    if open_:
        full = sum(1 for n in near if n["factions"] is not None and n["factions"] >= cap)
        t = open_[0]
        return _res("pass", f"Nearest system with room: {t['name']} ({t['distance_ly']:g} ly, {t['factions']} of {cap} factions). "
                            f"{len(open_)} of {len(near)} populated systems within {radius:g} ly have room; {full} are full.")
    if unknown:
        return _res("unknown", f"No known system with room within {radius:g} ly, but {len(unknown)} nearby "
                               f"system(s) have no stored faction list.")
    if not near:
        return _res("fail", f"No populated systems are stored within {radius:g} ly.")
    return _res("fail", f"All {len(near)} populated systems within {radius:g} ly already have {cap} factions.")


def origin_by_happiness(ctx, row, params, rule):
    others = [f for f in ctx.footprint(row["faction"]) if f["system"].lower() != row["system"].lower()]
    if not others:
        return _res("pass", "The faction is above the threshold only in this system, so it is the only origin.")
    fac = ctx.faction(row["system"], row["faction"])
    mine = HAPPINESS_RANK.get(str(fac.happiness).lower()) if fac else None
    theirs = [(f["system"], HAPPINESS_RANK.get(str(f["happiness"]).lower())) for f in others]
    names = ", ".join(f["system"] for f in others)
    if mine is None or any(r is None for _, r in theirs):
        return _res("unknown", f"The faction is also above the threshold in {names}, but happiness is missing "
                               f"for at least one of them.")
    best = min(r for _, r in theirs)
    if mine < best:
        return _res("pass", f"Also above the threshold in {names}; happiness here is the highest.")
    if mine == best:
        return _res("unknown", f"Also above the threshold in {names} with equal happiness; the tie-break is not known.")
    better = ", ".join(s for s, r in theirs if r < mine)
    return _res("fail", f"The faction is happier in {better}, which would become the origin instead.")


def influence_margin(ctx, row, params, rule):
    pct = row.get("influence_pct")
    if pct is None:
        return _res("unknown", "The faction's influence here is not stored.")
    margin = pct - params["influence_threshold_pct"]
    if margin <= 0:
        return _res("info", "Influence is not above the threshold, so the margin does not apply.")
    if margin < params["influence_margin_pct"]:
        return _res("fail", f"Only {margin:.1f} points above the threshold (under {params['influence_margin_pct']:g}).")
    return _res("pass", f"{margin:.1f} points above the threshold.")


def fallback_ladder(ctx, row, params, rule):
    cap, ladder = params["max_factions"], params["radius_ladder_ly"]
    if ctx.system(row["system"]) is None or ctx.system(row["system"]).x is None:
        return _res("info", "Position unknown; the wider-search outcome cannot be checked.")
    steps, counts = [], []
    for radius in ladder:
        near = ctx.neighbours(row["system"], radius)
        counts.append(sum(1 for n in near if n["factions"] is not None and n["factions"] < cap))
        steps.append(f"{radius:g} ly: {counts[-1]} with room")
    prefix = ("This attempt should find a target; " if counts[0]
              else "If every system in the first radius is full this becomes an Investment, and the search widens. ")
    return _res("info", prefix + "Systems with room by radius: " + "; ".join(steps) + ".")


def pending_timing(ctx, row, params, rule):
    run = row.get("run")
    days = params["pending_days"]
    tail = (f" Then active for {params['active_days'][0]} to {params['active_days'][1]} days "
            f"(about {params['expansion_tax_pct_per_day']:g}% influence tax per day), then about "
            f"{params['cooldown_days']} days of cooldown.")
    if not run:
        return _res("info", f"Pending lasts about {days} days; how long this one has been pending is unknown." + tail)
    return _res("info", f"Pending for {row.get('pending_text')} of about {days} days." + tail)


CHECKS = {f.__name__: f for f in (influence_above_threshold, not_blocked_by_conflict, target_in_range,
                                  origin_by_happiness, influence_margin, fallback_ladder, pending_timing)}


# ---- combining ---------------------------------------------------------------------------------------------------

def validate_ruleset(ruleset=RULESET, checks=CHECKS):
    """Every problem in a ruleset, as strings (empty list when it is usable). Run by the tests on RULESET."""
    errors = []
    if not isinstance(ruleset.get("params"), dict):
        return ["params: missing"]
    p = ruleset["params"]
    for k in ("influence_threshold_pct", "max_factions", "radius_ladder_ly", "blocking_states", "base_probability",
              "min_probability", "max_probability", "likely_at", "possible_at"):
        if k not in p:
            errors.append(f"params.{k}: missing")
    seen = set()
    for i, r in enumerate(ruleset.get("rules") or []):
        where = f"rules[{i}] ({r.get('id')})"
        if r.get("id") in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(r.get("id"))
        if r.get("check") not in checks:
            errors.append(f"{where}: unknown check {r.get('check')!r}")
        if r.get("kind") not in KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(KINDS)}")
        if r.get("confidence") not in CONFIDENCE:
            errors.append(f"{where}: confidence must be one of {', '.join(CONFIDENCE)}")
        needed = {"gate": ("cap", "unknown_cap"), "modifier": ("fail_factor", "unknown_factor")}.get(r.get("kind"), ())
        for k in needed + ("title", "explain", "source"):
            if k not in r:
                errors.append(f"{where}: missing {k}")
    return errors


def _label(p, ruleset, failed_gate, unknown_gate):
    if failed_gate:
        return "Unlikely"
    if unknown_gate:
        return "Uncertain"
    if p >= ruleset["params"]["likely_at"]:
        return "Likely"
    return "Possible" if p >= ruleset["params"]["possible_at"] else "Unlikely"


def evaluate(ctx, row, ruleset=RULESET, checks=CHECKS):
    """
    Run every rule for one pending-Expansion row. Returns
    {"probability": 0..1, "label", "rules": [{id, title, kind, result, detail, explain, confidence, source}],
     "ruleset_version", "ruleset_updated", "disclaimer"}.
    Start from base_probability, multiply in each modifier, then cap by the lowest failing/unknown gate and clamp.
    """
    p = ruleset["params"]
    prob, cap = p["base_probability"], 1.0
    failed_gate = unknown_gate = False
    rules = []
    for rule in ruleset["rules"]:
        out = checks[rule["check"]](ctx, row, p, rule)
        result = out["result"]
        if rule["kind"] == "gate":
            if result == "fail":
                cap, failed_gate = min(cap, rule["cap"]), True
            elif result == "unknown":
                cap, unknown_gate = min(cap, rule["unknown_cap"]), True
        elif rule["kind"] == "modifier":
            if result == "fail":
                prob *= rule["fail_factor"]
            elif result == "unknown":
                prob *= rule["unknown_factor"]
        rules.append({"id": rule["id"], "title": rule["title"], "kind": rule["kind"], "result": result,
                      "detail": out["detail"], "explain": rule["explain"], "confidence": rule["confidence"],
                      "source": rule["source"]})
    prob = max(p["min_probability"], min(p["max_probability"], min(prob, cap)))
    return {"probability": round(prob, 2), "label": _label(prob, ruleset, failed_gate, unknown_gate),
            "rules": rules, "ruleset_version": ruleset["version"], "ruleset_updated": ruleset["updated"],
            "disclaimer": ruleset["disclaimer"]}
