"""The Expansion rules the Recce dashboard uses to estimate whether a pending Expansion will go ahead.

This file is DATA. When the community's understanding of the Background Simulation changes, edit it here: change a
number in `params`, reword a rule, reorder rules, change how hard a rule counts, or add a rule that points at one of the
check functions in expansion.CHECKS. Nothing else needs touching unless a rule needs a brand-new kind of check.

The mechanics come from developer patch notes, forum comments and community tracking, not from an official manual, so
treat every rule as a best current guess. `confidence` says how firmly each one is established and is shown on the page.

Rule fields
  id           unique name (also the key the page and tests use)
  title        short heading shown in the explanation
  check        name of the function in expansion.CHECKS that decides this rule
  kind         "gate"     a failing gate caps the chance at `cap` (an unknown gate at `unknown_cap`)
               "modifier" multiplies the chance by `fail_factor` (fail) or `unknown_factor` (unknown)
               "info"     explanation only; never changes the chance
  confidence   "dev-stated" | "community" | "uncertain"
  source       where the rule comes from, shown on the page
  explain      one sentence on what the rule means, shown on the page
"""

RULESET = {
    "version": "2026-10-13",
    "updated": "2026-10-13",
    "sources": [
        "Frontier developer patch notes and forum comments (incl. Beyond - Chapter Four 3.3)",
        "Community 'BGS Guide Best Current Thinking' megathread on the Frontier forums",
    ],
    "disclaimer": ("An estimate from community-derived mechanics, not an official Frontier calculation. "
                   "The rules may be incomplete or out of date."),
    "params": {
        "influence_threshold_pct": 75.0,       # a faction expands once its influence exceeds this
        "influence_margin_pct": 3.0,           # closer than this to the threshold is "fragile"
        "pending_days": 5,
        "active_days": [5, 7],
        "cooldown_days": 3,
        "expansion_tax_pct_per_day": 3.0,      # shown for information only
        "max_factions": 7,                     # a system with this many factions is full
        "radius_ladder_ly": [20, 30, 60],      # search radius for the first attempt, then after each Investment
        "blocking_states": ["War", "Civil War", "Election"],
        "base_probability": 0.85,              # chance when every rule passes
        "min_probability": 0.03,
        "max_probability": 0.95,
        "likely_at": 0.65,                     # label thresholds
        "possible_at": 0.35,
    },
    "rules": [
        {"id": "influence_above_threshold", "title": "Influence above the expansion threshold",
         "check": "influence_above_threshold", "kind": "gate", "cap": 0.05, "unknown_cap": 0.4,
         "confidence": "dev-stated", "source": "Developer statements",
         "explain": "A minor faction expands when its influence exceeds 75%. It does not need to control the system."},
        {"id": "not_blocked_by_conflict", "title": "Not held up by a conflict",
         "check": "not_blocked_by_conflict", "kind": "gate", "cap": 0.15, "unknown_cap": 0.6,
         "confidence": "community", "source": "Community tracking",
         "explain": "A War, Civil War or Election takes priority and freezes the pending countdown until it ends."},
        {"id": "target_in_range", "title": "A system with room within the first search radius",
         "check": "target_in_range", "kind": "gate", "cap": 0.05, "unknown_cap": 0.5,
         "confidence": "dev-stated", "source": "Developer statements",
         "explain": ("The closest populated system within the search radius that has fewer than 7 factions is "
                     "targeted. If every system in range is full, the attempt fails and becomes an Investment.")},
        {"id": "origin_by_happiness", "title": "This system is the faction's expansion origin",
         "check": "origin_by_happiness", "kind": "modifier", "fail_factor": 0.15, "unknown_factor": 0.8,
         "confidence": "dev-stated", "source": "Beyond - Chapter Four (3.3) update notes",
         "explain": ("If the faction is above the threshold in several systems, the one where it is happiest "
                     "becomes the origin.")},
        {"id": "influence_margin", "title": "Influence safely above the threshold",
         "check": "influence_margin", "kind": "modifier", "fail_factor": 0.75, "unknown_factor": 0.9,
         "confidence": "uncertain", "source": "Inference from the 75% rule",
         "explain": "Influence only just above 75% can slip back below it before the expansion starts."},
        {"id": "fallback_ladder", "title": "If this attempt fails: Investment and wider search",
         "check": "fallback_ladder", "kind": "info",
         "confidence": "community", "source": "Community tracking",
         "explain": ("A failed attempt triggers Investment and the search radius grows for the next attempt "
                     "(20, then 30, then up to 60 ly).")},
        {"id": "pending_timing", "title": "Pending and active timing",
         "check": "pending_timing", "kind": "info",
         "confidence": "community", "source": "Community tracking",
         "explain": ("Pending lasts about 5 days, then Active for 5 to 7 days, then a cooldown of about 3 days. "
                     "While active the origin pays an influence tax of roughly 3% per day.")},
    ],
}
