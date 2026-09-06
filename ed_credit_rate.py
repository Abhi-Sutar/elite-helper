#!/usr/bin/env python3
"""
ed_credit_rate.py

Computes your effective credits/hour rate from an Elite Dangerous journal file,
based on MarketBuy / MarketSell events (i.e. commodity trading).

Journal files are newline-delimited JSON, one event per line. Relevant events:

  MarketBuy:
    { "timestamp": "...", "event": "MarketBuy", "MarketID": ..., "Type": "gold",
      "Count": 1147, "BuyPrice": 45673, "TotalCost": 52387531 }

  MarketSell:
    { "timestamp": "...", "event": "MarketSell", "MarketID": ..., "Type": "gold",
      "Count": 1147, "SellPrice": 282678, "TotalSale": 324231066,
      "AvgPricePaid": 45673 }

Docs / schema reference: EDCD/EDDiscovery community journal manual
(https://github.com/EDCD/EliteDangerous.Journal.Manual) and EDDiscovery's own
event parsing (https://github.com/EDDiscovery/EDDiscovery), which this script
mirrors the relevant field names from without depending on either codebase.

Journal files live at:
  Windows: %USERPROFILE%\\Saved Games\\Frontier Developments\\Elite Dangerous\\
  Named like: Journal.2026-02-03T120000.01.log

Usage:
  python ed_credit_rate.py                       # auto-find latest journal(s) in default dir
  python ed_credit_rate.py --dir "C:\\path\\to\\journals"
  python ed_credit_rate.py --file Journal.xxx.log # parse a single specific file
  python ed_credit_rate.py --since "2026-09-06T18:00:00"  # only count events after this UTC time
  python ed_credit_rate.py --session               # only count the most recent contiguous session
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone


def default_journal_dir():
    """Best-guess default Elite Dangerous journal folder per-OS."""
    home = os.path.expanduser("~")
    win_path = os.path.join(home, "Saved Games", "Frontier Developments", "Elite Dangerous")
    if os.path.isdir(win_path):
        return win_path
    # Fallback: current directory
    return os.getcwd()


def find_journal_files(directory):
    pattern = os.path.join(directory, "Journal.*.log")
    files = sorted(glob.glob(pattern))
    return files


def parse_timestamp(ts_str):
    # Journal timestamps are ISO 8601 UTC, e.g. "2026-09-06T18:03:11Z"
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_events(files):
    """Yield parsed JSON events from a list of journal files, in file order."""
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def compute_trade_stats(events, since=None, session_only=False):
    """
    Walk MarketBuy / MarketSell events and compute:
      - total spent (credits)
      - total earned (credits)
      - net profit (credits)
      - elapsed real time between first and last relevant event (hours)
      - per-commodity breakdown

    session_only: if True, only use events from the last contiguous block of
    trading activity (gap of >30 min between trade events starts a new session,
    only the final session is kept). This approximates "this play session" without
    needing you to pass explicit timestamps.
    """
    trade_events = []
    for e in events:
        ev_type = e.get("event")
        if ev_type not in ("MarketBuy", "MarketSell"):
            continue
        ts = e.get("timestamp")
        if not ts:
            continue
        try:
            dt = parse_timestamp(ts)
        except ValueError:
            continue
        if since and dt < since:
            continue
        trade_events.append((dt, e))

    trade_events.sort(key=lambda x: x[0])

    if not trade_events:
        return None

    if session_only:
        # Split into sessions on >30 min gaps, keep only the last session
        SESSION_GAP_SECONDS = 30 * 60
        sessions = []
        current = [trade_events[0]]
        for prev, curr in zip(trade_events, trade_events[1:]):
            gap = (curr[0] - prev[0]).total_seconds()
            if gap > SESSION_GAP_SECONDS:
                sessions.append(current)
                current = []
            current.append(curr)
        sessions.append(current)
        trade_events = sessions[-1]

    total_spent = 0
    total_earned = 0
    per_commodity = {}

    first_ts = trade_events[0][0]
    last_ts = trade_events[-1][0]

    for dt, e in trade_events:
        commodity = e.get("Type_Localised", e.get("Type", "unknown"))
        count = e.get("Count", 0)
        if e["event"] == "MarketBuy":
            cost = e.get("TotalCost", 0)
            total_spent += cost
            bucket = per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            bucket["bought"] += count
            bucket["spent"] += cost
        elif e["event"] == "MarketSell":
            sale = e.get("TotalSale", 0)
            total_earned += sale
            bucket = per_commodity.setdefault(commodity, {"bought": 0, "spent": 0, "sold": 0, "earned": 0})
            bucket["sold"] += count
            bucket["earned"] += sale

    elapsed_hours = max((last_ts - first_ts).total_seconds() / 3600.0, 1e-9)
    net_profit = total_earned - total_spent

    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "elapsed_hours": elapsed_hours,
        "total_spent": total_spent,
        "total_earned": total_earned,
        "net_profit": net_profit,
        "credits_per_hour": net_profit / elapsed_hours,
        "per_commodity": per_commodity,
        "num_events": len(trade_events),
    }


def fmt_cr(value):
    return f"{value:,.0f} Cr"


def main():
    parser = argparse.ArgumentParser(description="Compute credits/hour from an Elite Dangerous journal.")
    parser.add_argument("--dir", help="Directory containing journal files (default: auto-detect)")
    parser.add_argument("--file", help="Parse a single specific journal file instead of a directory")
    parser.add_argument("--since", help="Only count events after this UTC timestamp, format YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--session", action="store_true",
                         help="Only use the most recent contiguous trading session (gap > 30 min starts a new one)")
    args = parser.parse_args()

    if args.file:
        files = [args.file]
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

    events = load_events(files)
    stats = compute_trade_stats(events, since=since_dt, session_only=args.session)

    if stats is None:
        print("No MarketBuy/MarketSell events found in the given journal(s)/time range.")
        sys.exit(0)

    print(f"Parsed {stats['num_events']} trade events")
    print(f"Window: {stats['first_ts'].isoformat()}  ->  {stats['last_ts'].isoformat()}")
    print(f"Elapsed: {stats['elapsed_hours']:.3f} hours")
    print()
    print(f"Total spent:   {fmt_cr(stats['total_spent'])}")
    print(f"Total earned:  {fmt_cr(stats['total_earned'])}")
    print(f"Net profit:    {fmt_cr(stats['net_profit'])}")
    print(f"Credits/hour:  {fmt_cr(stats['credits_per_hour'])}")
    print()
    print("Per-commodity breakdown:")
    for commodity, d in sorted(stats["per_commodity"].items(), key=lambda kv: kv[1]["earned"] - kv[1]["spent"], reverse=True):
        profit = d["earned"] - d["spent"]
        print(f"  {commodity:<20} bought={d['bought']:>7}  spent={fmt_cr(d['spent']):>18}  "
              f"sold={d['sold']:>7}  earned={fmt_cr(d['earned']):>18}  profit={fmt_cr(profit):>18}")


if __name__ == "__main__":
    main()