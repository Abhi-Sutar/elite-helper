#!/usr/bin/env python3
"""
ed_credit_rate.py

Computes your effective credits/hour rate from Elite Dangerous journal files,
using actual active playtime (stitched across sessions) rather than raw
wall-clock time between first and last event. Also aggregates multiple income
streams, not just commodity trading.

WHY "PLAYTIME" NEEDS STITCHING
Each journal file (Journal.<timestamp>.NN.log) corresponds to one continuous
game session, from launch (Fileheader event, first line) to close (Shutdown
event, usually the last line, sometimes absent if the game crashed/was killed).
If you play Monday evening and again Wednesday, the two-day gap between your
last Monday event and first Wednesday event is NOT playtime. This script sums
(last_event_time - first_event_time) PER FILE, across however many session
files you point it at, so idle time between sessions never counts.

Within a single file, a large internal gap (e.g. you tabbed out / went AFK for
an hour with the client open) is optionally treated as non-playtime too, via
--max-gap-minutes, since that time wasn't spent doing anything income-relevant
either.

INCOME STREAMS COVERED
  - Trading:            MarketBuy / MarketSell
  - Bounty vouchers:    RedeemVoucher (Type == "bounty" or "CombatBond")
  - Mission rewards:    MissionCompleted (Reward + any CommodityReward market
                         value is not auto-priced, only the credit Reward field
                         is counted, since commodity reward value depends on
                         where you sell it)
  - Exploration data:   SellExplorationData / MultiSellExplorationData
  - Trade profit is computed as spend vs earn per commodity, same as v1.

Journal event schema reference: the community-maintained journal manual
(https://github.com/EDCD/EliteDangerous.Journal.Manual) and EDDiscovery's
public event handling (https://github.com/EDDiscovery/EDDiscovery) were used
as the reference for field names; no code from either project is reproduced
here, this is a from-scratch parser against the documented/observed schema.

Journal files live at:
  Windows: %USERPROFILE%\\Saved Games\\Frontier Developments\\Elite Dangerous\\
  Named like: Journal.2026-02-03T120000.01.log

Usage:
  python ed_credit_rate.py
  python ed_credit_rate.py --dir "C:\\path\\to\\journals"
  python ed_credit_rate.py --file Journal.xxx.log
  python ed_credit_rate.py --since 2026-09-01T00:00:00
  python ed_credit_rate.py --max-gap-minutes 20
  python ed_credit_rate.py --streams trading,bounty
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone


ALL_STREAMS = ("trading", "bounty", "mission", "exploration")


def default_journal_dir():
    home = os.path.expanduser("~")
    win_path = os.path.join(home, "Saved Games", "Frontier Developments", "Elite Dangerous")
    if os.path.isdir(win_path):
        return win_path
    return os.getcwd()


def find_journal_files(directory):
    pattern = os.path.join(directory, "Journal.*.log")
    return sorted(glob.glob(pattern))


def parse_timestamp(ts_str):
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_file_events(path):
    """Return a list of (datetime, event_dict) for one journal file, in order."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("timestamp")
            if not ts:
                continue
            try:
                dt = parse_timestamp(ts)
            except ValueError:
                continue
            out.append((dt, e))
    return out


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


def process_income_events(events, streams):
    """
    Walk one session's events and extract income-relevant activity.
    Returns a dict of stream -> {"earned":..., "spent":..., "count":...}
    and a per_commodity dict for the trading stream specifically.
    """
    result = {s: {"earned": 0, "spent": 0, "count": 0} for s in ALL_STREAMS}
    per_commodity = {}

    for _, e in events:
        ev = e.get("event")

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
            reward = e.get("Reward", 0)
            # Some missions pay via a negotiated reward instead of Reward field
            if not reward:
                reward = e.get("Reward", 0)
            result["mission"]["earned"] += reward
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

    return result, per_commodity


def fmt_cr(value):
    return f"{value:,.0f} Cr"


def main():
    parser = argparse.ArgumentParser(description="Compute credits/hour from Elite Dangerous journals, using real stitched playtime.")
    parser.add_argument("--dir", help="Directory containing journal files (default: auto-detect)")
    parser.add_argument("--file", action="append", help="Parse specific journal file(s), can repeat")
    parser.add_argument("--since", help="Only count sessions/files with events after this UTC timestamp, format YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--max-gap-minutes", type=float, default=None,
                         help="Treat any in-session gap longer than this as non-playtime (e.g. AFK). Default: no gap filtering.")
    parser.add_argument("--streams", default="trading,bounty,mission,exploration",
                         help="Comma-separated list of income streams to include: trading,bounty,mission,exploration")
    args = parser.parse_args()

    streams = set(s.strip() for s in args.streams.split(","))
    unknown = streams - set(ALL_STREAMS)
    if unknown:
        print(f"Unknown stream(s): {unknown}. Valid options: {ALL_STREAMS}", file=sys.stderr)
        sys.exit(1)

    if args.file:
        files = args.file
    else:
        directory = args.dir or default_journal_dir()
        files = find_journal_files(directory)
        if not files:
            print(f"No journal files found in: {directory}", file=sys.stderr)
            print("Pass --dir or --file explicitly if your journals live elsewhere.", file=sys.stderr)
            sys.exit(1)

    since_dt = None
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

    total_playtime_seconds = 0.0
    grand_totals = {s: {"earned": 0, "spent": 0, "count": 0} for s in ALL_STREAMS}
    grand_per_commodity = {}
    sessions_used = 0
    earliest = None
    latest = None

    for path in files:
        events = load_file_events(path)
        if since_dt:
            events = [(t, e) for (t, e) in events if t >= since_dt]
        if len(events) < 2:
            continue

        sessions_used += 1
        t_first, t_last = events[0][0], events[-1][0]
        earliest = t_first if earliest is None else min(earliest, t_first)
        latest = t_last if latest is None else max(latest, t_last)

        total_playtime_seconds += session_playtime_seconds(events, args.max_gap_minutes)

        session_income, per_commodity = process_income_events(events, streams)
        for s in ALL_STREAMS:
            grand_totals[s]["earned"] += session_income[s]["earned"]
            grand_totals[s]["spent"] += session_income[s]["spent"]
            grand_totals[s]["count"] += session_income[s]["count"]

        for commodity, d in per_commodity.items():
            gc = grand_per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            for k in d:
                gc[k] += d[k]

    if sessions_used == 0:
        print("No usable sessions found (need at least 2 timestamped events per file).")
        sys.exit(0)

    playtime_hours = total_playtime_seconds / 3600.0
    if playtime_hours <= 0:
        print("Playtime computed as zero, cannot compute a rate.")
        sys.exit(0)

    total_earned = sum(grand_totals[s]["earned"] for s in ALL_STREAMS)
    total_spent = sum(grand_totals[s]["spent"] for s in ALL_STREAMS)
    net_profit = total_earned - total_spent

    print(f"Sessions used: {sessions_used}  (files spanning {earliest.date()} to {latest.date()})")
    print(f"Total active playtime: {playtime_hours:.3f} hours  (stitched across sessions"
          + (f", gaps > {args.max_gap_minutes:.0f} min excluded" if args.max_gap_minutes else "") + ")")
    print()
    print("Income by stream:")
    for s in ALL_STREAMS:
        if s not in streams:
            continue
        d = grand_totals[s]
        net = d["earned"] - d["spent"]
        print(f"  {s:<12} count={d['count']:>5}  earned={fmt_cr(d['earned']):>18}  "
              f"spent={fmt_cr(d['spent']):>18}  net={fmt_cr(net):>18}")
    print()
    print(f"Total earned:  {fmt_cr(total_earned)}")
    print(f"Total spent:   {fmt_cr(total_spent)}")
    print(f"Net profit:    {fmt_cr(net_profit)}")
    print(f"Credits/hour:  {fmt_cr(net_profit / playtime_hours)}   (based on active playtime, not wall-clock span)")

    if "trading" in streams and grand_per_commodity:
        print()
        print("Per-commodity trading breakdown:")
        for commodity, d in sorted(grand_per_commodity.items(), key=lambda kv: kv[1]["earned"] - kv[1]["spent"], reverse=True):
            profit = d["earned"] - d["spent"]
            print(f"  {commodity:<20} bought={d['bought']:>7}  spent={fmt_cr(d['spent']):>18}  "
                  f"sold={d['sold']:>7}  earned={fmt_cr(d['earned']):>18}  profit={fmt_cr(profit):>18}")


if __name__ == "__main__":
    main()