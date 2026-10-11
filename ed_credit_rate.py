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
  - Fleet carrier (your own transfers, exact):
                         CarrierBuy (purchase cost), CarrierDecommission (sale
                         refund), CarrierBankTransfer (Deposit/Withdraw between
                         your personal wallet and the carrier's own bank,
                         counted as spent/earned respectively since that's
                         money leaving/entering your wallet balance).
  - Fleet carrier market (approximate):
                         The journal has NO per-transaction event for other
                         commanders/NPCs buying from or selling to your
                         carrier's trade orders -- that activity is invisible
                         to a journal-only tool. The only proxy available is
                         the carrier's bank balance, reported as a snapshot in
                         CarrierStats/CarrierFinance each time you open
                         carrier management in-game. This stream diffs
                         consecutive balance snapshots per carrier, subtracts
                         out any CarrierBankTransfer Deposit/Withdraw that
                         happened in between (so your own transfers aren't
                         double-counted), and reports the remainder as
                         "carrier_market" earned/spent. This necessarily also
                         absorbs weekly carrier upkeep deductions and the
                         carrier's own buy-order spending, since the journal
                         doesn't separate those from sale revenue -- treat it
                         as "net carrier balance movement you didn't cause
                         directly", not pure sales income. Resolution is only
                         as fine as how often you open carrier management.
  - Trade profit is computed as spend vs earn per commodity, same as v1.

EXPENSE STREAMS (counted as "spent", so they reduce net profit / credits/hour)
  - Maintenance:        Repair, RepairAll, AFMURepairs (repair), RefuelAll,
                         RefuelPartial (refuel), BuyAmmo (rearm), BuyDrones
                         (limpets), RestockVehicle (SRV/fighter restock).
                         A per-kind breakdown (repair/refuel/rearm/...) is
                         printed after the stream table.
  - Rebuys:             Resurrected (the insurance rebuy Cost after your ship
                         is destroyed). Reported as its own stream so the
                         balance sheet shows what dying cost you.
  - Crew wages:         NpcCrewPaidWage (Amount). Use --crew-wages to also
                         print the total paid out per crew member.

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
  python ed_credit_rate.py --streams trading,bounty,carrier,carrier_market
  python ed_credit_rate.py --session          # only the most recent journal file
"""

import argparse
import sys

from eddn_helper.journal import files as jfiles
from eddn_helper.journal import income
from eddn_helper.journal.income import ALL_STREAMS


def main():
    parser = argparse.ArgumentParser(description="Compute credits/hour from Elite Dangerous journals, using real stitched playtime.")
    parser.add_argument("--dir", help="Directory containing journal files (default: auto-detect)")
    parser.add_argument("--file", action="append", help="Parse specific journal file(s), can repeat")
    parser.add_argument("--since", help="Only count sessions/files with events after this UTC timestamp, format YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--max-gap-minutes", type=float, default=None,
                         help="Treat any in-session gap longer than this as non-playtime (e.g. AFK). Default: no gap filtering.")
    parser.add_argument("--streams", default=",".join(ALL_STREAMS),
                         help="Comma-separated list of income streams to include: " + ",".join(ALL_STREAMS))
    parser.add_argument("--crew-wages", action="store_true",
                         help="Also print total wages paid out per NPC crew member")
    parser.add_argument("--session", action="store_true",
                         help="Only use the single most recent journal file (i.e. just your last play session), instead of all files found")
    args = parser.parse_args()

    if args.file:
        files = args.file
    else:
        directory = jfiles.journal_dir(args.dir)
        files = jfiles.find_journal_files(directory)
        if not files:
            print(f"No journal files found in: {directory}", file=sys.stderr)
            print("Pass --dir or --file explicitly if your journals live elsewhere.", file=sys.stderr)
            sys.exit(1)
        if args.session:
            files = [files[-1]]  # filenames sort chronologically, last one is most recent

    result = income.credit_rate(files, args.streams, args.since, args.max_gap_minutes, args.crew_wages)
    if "error" in result:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print("\n".join(income.format_report(result)))


if __name__ == "__main__":
    main()
