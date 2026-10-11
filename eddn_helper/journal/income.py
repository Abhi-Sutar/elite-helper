"""
Credit-rate analysis over journal files: effective credits/hour from stitched active playtime plus every income
and expense stream. Logic moved from the original ed_credit_rate.py; see that script's docstring for the
reasoning behind playtime stitching and the carrier_market approximation.
"""

from datetime import datetime, timezone

from . import files as jfiles

ALL_STREAMS = ("trading", "bounty", "mission", "exploration", "carrier", "carrier_market",
               "maintenance", "rebuy", "crew_wages")

# Journal event -> (maintenance kind, cost field)
MAINTENANCE_EVENTS = {
    "Repair": ("repair", "Cost"),
    "RepairAll": ("repair", "Cost"),
    "AFMURepairs": ("repair", "Cost"),
    "RefuelAll": ("refuel", "Cost"),
    "RefuelPartial": ("refuel", "Cost"),
    "BuyAmmo": ("rearm", "Cost"),
    "BuyDrones": ("limpets", "TotalCost"),
    "RestockVehicle": ("restock", "Cost"),
}


def session_playtime_seconds(events, max_gap_minutes=None):
    """
    Given a time-sorted list of (dt, event) for ONE session/file, return total
    active seconds. If max_gap_minutes is set, any internal gap between
    consecutive events longer than that is excluded (treated as AFK/paused),
    rather than counted as playtime.
    """
    if len(events) < 2:
        return 0.0
    total = 0.0
    max_gap = max_gap_minutes * 60 if max_gap_minutes else None
    for (t_prev, _), (t_curr, _) in zip(events, events[1:]):
        gap = (t_curr - t_prev).total_seconds()
        if gap <= 0:
            continue
        if max_gap is not None and gap > max_gap:
            continue  # drop this gap, treat as non-playtime
        total += gap
    return total


def process_income_events(events, streams, carrier_state):
    """
    Walk one session's events and extract income-relevant activity.
    Returns a dict of stream -> {"earned":..., "spent":..., "count":...}
    a per_commodity dict for the trading stream, and a maintenance dict
    (kind -> {"spent","count"}) breaking down the maintenance stream, and a
    crew dict (crew name -> {"spent","count"}) of wages paid per crew member.

    carrier_state is a dict of CarrierID -> {"balance": last seen
    CarrierBalance or None, "transfer_since_last": net Deposit-Withdraw since
    that snapshot}, mutated in place and expected to be threaded across
    successive calls (one per journal file, in chronological order) so the
    carrier_market stream can diff balance snapshots across sessions.
    """
    result = {s: {"earned": 0, "spent": 0, "count": 0} for s in ALL_STREAMS}
    per_commodity = {}
    maintenance = {}
    crew = {}

    for _, e in events:
        ev = e.get("event")

        if ev in MAINTENANCE_EVENTS and "maintenance" in streams:
            kind, field = MAINTENANCE_EVENTS[ev]
            cost = e.get(field, 0) or 0
            result["maintenance"]["spent"] += cost
            result["maintenance"]["count"] += 1
            m = maintenance.setdefault(kind, {"spent": 0, "count": 0})
            m["spent"] += cost
            m["count"] += 1
            continue

        if ev == "NpcCrewPaidWage" and "crew_wages" in streams:
            amount = e.get("Amount", 0) or 0
            name = e.get("NpcCrewName") or str(e.get("NpcCrewId", "unknown"))
            result["crew_wages"]["spent"] += amount
            result["crew_wages"]["count"] += 1
            c = crew.setdefault(name, {"spent": 0, "count": 0})
            c["spent"] += amount
            c["count"] += 1
            continue

        if ev == "Resurrected" and "rebuy" in streams:
            # Cost is 0 when you chose to rebuy while bankrupt / recovered via other options.
            result["rebuy"]["spent"] += e.get("Cost", 0) or 0
            result["rebuy"]["count"] += 1
            continue

        if ev == "MarketBuy" and "trading" in streams:
            cost = e.get("TotalCost", 0)
            commodity = e.get("Type_Localised", e.get("Type", "unknown"))
            result["trading"]["spent"] += cost
            result["trading"]["count"] += 1
            b = per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            b["bought"] += e.get("Count", 0)
            b["spent"] += cost

        elif ev == "MarketSell" and "trading" in streams:
            sale = e.get("TotalSale", 0)
            commodity = e.get("Type_Localised", e.get("Type", "unknown"))
            result["trading"]["earned"] += sale
            result["trading"]["count"] += 1
            b = per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            b["sold"] += e.get("Count", 0)
            b["earned"] += sale

        elif ev == "RedeemVoucher" and "bounty" in streams:
            # Type is "bounty", "CombatBond", "scannable" (settlement data), etc.
            # We count bounty + CombatBond as combat income specifically.
            if e.get("Type") in ("bounty", "CombatBond"):
                amount = e.get("Amount", 0)
                result["bounty"]["earned"] += amount
                result["bounty"]["count"] += 1

        elif ev == "MissionCompleted" and "mission" in streams:
            # Only the credit Reward counts: commodity rewards depend on where they are sold.
            result["mission"]["earned"] += e.get("Reward", 0)
            result["mission"]["count"] += 1

        elif ev in ("SellExplorationData", "MultiSellExplorationData") and "exploration" in streams:
            # SellExplorationData (old): TotalEarnings
            # MultiSellExplorationData (current): TotalEarningsBonus + Discovered[].BaseValue sums,
            # but the event itself carries a top-level "BaseValue"+"Bonus" aggregate on some
            # versions; prefer explicit total fields when present, else sum discovered entries.
            total = e.get("TotalEarningsBonus")
            if total is None:
                total = e.get("TotalEarnings")
            if total is None:
                discovered = e.get("Discovered", [])
                total = sum(d.get("BaseValue", 0) + d.get("Bonus", 0) for d in discovered)
            result["exploration"]["earned"] += total or 0
            result["exploration"]["count"] += 1

        elif ev == "CarrierBuy" and "carrier" in streams:
            # Field is "Price" in the real journal (older docs say "Cost").
            result["carrier"]["spent"] += e.get("Price", e.get("Cost", 0))
            result["carrier"]["count"] += 1

        elif ev == "CarrierDecommission" and "carrier" in streams:
            # Field naming for the refund varies across journal manual versions.
            result["carrier"]["earned"] += e.get("Refund", e.get("ScrapRefund", 0))
            result["carrier"]["count"] += 1

        elif ev == "CarrierBankTransfer" and ("carrier" in streams or "carrier_market" in streams):
            # Deposit/Withdraw are the amount fields directly (no separate Direction field).
            deposit = e.get("Deposit", 0)
            withdraw = e.get("Withdraw", 0)
            if "carrier" in streams:
                result["carrier"]["spent"] += deposit
                result["carrier"]["earned"] += withdraw
                result["carrier"]["count"] += 1
            if "carrier_market" in streams:
                cid = e.get("CarrierID")
                if cid is not None:
                    st = carrier_state.setdefault(cid, {"balance": None, "transfer_since_last": 0})
                    st["transfer_since_last"] += deposit - withdraw

        elif ev in ("CarrierStats", "CarrierFinance") and "carrier_market" in streams:
            cid = e.get("CarrierID")
            if ev == "CarrierStats":
                balance = e.get("Finance", {}).get("CarrierBalance")
            else:
                balance = e.get("CarrierBalance")
            if cid is not None and balance is not None:
                st = carrier_state.setdefault(cid, {"balance": None, "transfer_since_last": 0})
                if st["balance"] is not None:
                    # Net change not explained by your own deposits/withdrawals since the
                    # last snapshot -- the only journal-visible proxy for carrier market
                    # activity (also absorbs weekly upkeep, see ed_credit_rate.py docstring).
                    market_delta = (balance - st["balance"]) - st["transfer_since_last"]
                    if market_delta > 0:
                        result["carrier_market"]["earned"] += market_delta
                    else:
                        result["carrier_market"]["spent"] += -market_delta
                    result["carrier_market"]["count"] += 1
                st["balance"] = balance
                st["transfer_since_last"] = 0

    return result, per_commodity, maintenance, crew


def parse_streams(streams):
    """None -> every stream; a comma string or iterable -> a validated set. Raises ValueError on unknown names."""
    if streams is None:
        return set(ALL_STREAMS)
    if isinstance(streams, str):
        streams = streams.split(",")
    chosen = {s.strip() for s in streams if s.strip()}
    unknown = chosen - set(ALL_STREAMS)
    if unknown:
        raise ValueError(f"Unknown stream(s): {sorted(unknown)}. Valid options: {list(ALL_STREAMS)}")
    return chosen


def parse_since(since):
    """datetime | 'YYYY-MM-DDTHH:MM:SS' (UTC) | None -> aware datetime | None."""
    if since is None or isinstance(since, datetime):
        return since
    return datetime.strptime(since, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def credit_rate(paths, streams=None, since=None, max_gap_minutes=None, crew_wages=False):
    """
    Credits/hour over the given journal files (oldest first). Returns a JSON-serialisable dict:
    sessions, playtime_hours, streams{earned, spent, net, count}, maintenance, per_commodity, totals,
    credits_per_hour and per-session rows. {"error": ...} for bad arguments, and a "note" instead of a rate when
    there is nothing usable (fewer than 2 timestamped events per file, or zero playtime).
    """
    try:
        streams = parse_streams(streams)
        since_dt = parse_since(since)
    except ValueError as e:
        return {"error": str(e)}

    grand = {s: {"earned": 0, "spent": 0, "count": 0} for s in ALL_STREAMS}
    per_commodity, maintenance, crew = {}, {}, {}
    session_rows = []
    playtime = 0.0
    earliest = latest = None
    # Carrier balance persists across sessions (unlike playtime), so this state is threaded across the whole
    # file loop below, in chronological order.
    carrier_state = {}

    for path in paths:
        events = jfiles.read_events(path)
        if since_dt:
            events = [(t, e) for (t, e) in events if t >= since_dt]
        if len(events) < 2:
            continue
        t_first, t_last = events[0][0], events[-1][0]
        earliest = t_first if earliest is None else min(earliest, t_first)
        latest = t_last if latest is None else max(latest, t_last)
        seconds = session_playtime_seconds(events, max_gap_minutes)
        playtime += seconds

        income, commodities, maint, crews = process_income_events(events, streams, carrier_state)
        for s in ALL_STREAMS:
            for k in ("earned", "spent", "count"):
                grand[s][k] += income[s][k]
        for src, dst in ((maint, maintenance), (crews, crew)):
            for kind, d in src.items():
                g = dst.setdefault(kind, {"spent": 0, "count": 0})
                g["spent"] += d["spent"]
                g["count"] += d["count"]
        for commodity, d in commodities.items():
            gc = per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            for k in d:
                gc[k] += d[k]
        earned = sum(income[s]["earned"] for s in ALL_STREAMS)
        spent = sum(income[s]["spent"] for s in ALL_STREAMS)
        session_rows.append({"file": str(path).replace("\\", "/").rsplit("/", 1)[-1], "start": jfiles.iso(t_first),
                             "end": jfiles.iso(t_last), "playtime_hours": round(seconds / 3600, 3),
                             "earned": earned, "spent": spent, "net": earned - spent})

    out = {"sessions_used": len(session_rows), "since": jfiles.iso(since_dt),
           "first_event": jfiles.iso(earliest), "last_event": jfiles.iso(latest),
           "max_gap_minutes": max_gap_minutes, "streams_included": [s for s in ALL_STREAMS if s in streams]}
    if not session_rows:
        out["note"] = "No usable sessions found (need at least 2 timestamped events per file)."
        return out
    hours = playtime / 3600.0
    out["playtime_hours"] = round(hours, 3)
    out["streams"] = {s: dict(grand[s], net=grand[s]["earned"] - grand[s]["spent"])
                      for s in ALL_STREAMS if s in streams}
    out["maintenance"] = maintenance
    out["per_commodity"] = {c: dict(d, profit=d["earned"] - d["spent"]) for c, d in per_commodity.items()}
    if crew_wages:
        out["crew_wages"] = crew
    total_earned = sum(grand[s]["earned"] for s in ALL_STREAMS)
    total_spent = sum(grand[s]["spent"] for s in ALL_STREAMS)
    out["totals"] = {"earned": total_earned, "spent": total_spent, "net": total_earned - total_spent}
    out["credits_per_hour"] = round((total_earned - total_spent) / hours) if hours > 0 else None
    if hours <= 0:
        out["note"] = "Playtime computed as zero, cannot compute a rate."
    out["sessions"] = session_rows
    return out


def fmt_cr(value):
    return f"{value:,.0f} Cr"


def format_report(d):
    """Human-readable text for a credit_rate() result (same layout as the original script). Returns a list of lines."""
    if "error" in d:
        return [d["error"]]
    if "playtime_hours" not in d or d.get("credits_per_hour") is None:
        return [d.get("note", "Nothing to report.")]
    lines = [f"Sessions used: {d['sessions_used']}  (files spanning {d['first_event'][:10]} to {d['last_event'][:10]})"]
    gap = d.get("max_gap_minutes")
    lines.append(f"Total active playtime: {d['playtime_hours']:.3f} hours  (stitched across sessions"
                 + (f", gaps > {gap:.0f} min excluded" if gap else "") + ")")
    lines += ["", "Income by stream:"]
    for s, v in d["streams"].items():
        lines.append(f"  {s:<12} count={v['count']:>5}  earned={fmt_cr(v['earned']):>18}  "
                     f"spent={fmt_cr(v['spent']):>18}  net={fmt_cr(v['net']):>18}")
    if d["maintenance"]:
        lines += ["", "Maintenance breakdown:"]
        for kind, v in sorted(d["maintenance"].items(), key=lambda kv: kv[1]["spent"], reverse=True):
            lines.append(f"  {kind:<12} count={v['count']:>5}  spent={fmt_cr(v['spent']):>18}")
    if "crew_wages" in d:
        lines += ["", "Crew wages paid per crew member:"]
        if d["crew_wages"]:
            for name, v in sorted(d["crew_wages"].items(), key=lambda kv: kv[1]["spent"], reverse=True):
                lines.append(f"  {name:<20} payments={v['count']:>5}  paid={fmt_cr(v['spent']):>18}")
        else:
            lines.append("  (no NpcCrewPaidWage events found)")
    t = d["totals"]
    lines += ["", f"Total earned:  {fmt_cr(t['earned'])}", f"Total spent:   {fmt_cr(t['spent'])}",
              f"Net profit:    {fmt_cr(t['net'])}",
              f"Credits/hour:  {fmt_cr(d['credits_per_hour'])}   (based on active playtime, not wall-clock span)"]
    if "trading" in d["streams"] and d["per_commodity"]:
        lines += ["", "Per-commodity trading breakdown:"]
        for commodity, v in sorted(d["per_commodity"].items(), key=lambda kv: kv[1]["profit"], reverse=True):
            lines.append(f"  {commodity:<20} bought={v['bought']:>7}  spent={fmt_cr(v['spent']):>18}  "
                         f"sold={v['sold']:>7}  earned={fmt_cr(v['earned']):>18}  profit={fmt_cr(v['profit']):>18}")
    return lines
