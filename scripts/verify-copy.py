"""Compare row counts between the frozen SQLite source and the live PostgreSQL database after copy-data.

Usage:  .venv\\Scripts\\python.exe scripts\\verify-copy.py <sqlite-file> <postgresql-url>
Tables the listener keeps writing (everything) are compared on the rows that existed when the source was frozen:
history tables by `ts <= max(source ts)`, snapshot tables by primary key presence (count only, so PG >= source).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from eddn_helper import database  # noqa: E402

src_path, pg_url = sys.argv[1], sys.argv[2]
src = database.make_engine("sqlite:///" + src_path.replace("\\", "/"))
dst = database.make_engine(pg_url)

SNAPSHOT = ["commodities", "systems", "factions", "markets", "commodity_prices"]
HISTORY = ["price_history", "faction_history"]

with src.connect() as s, dst.connect() as d:
    print(f"{'table':18} {'sqlite':>10} {'postgres':>10}  result")
    for t in SNAPSHOT:
        a = s.execute(text(f"SELECT count(*) FROM {t}")).scalar()
        b = d.execute(text(f"SELECT count(*) FROM {t}")).scalar()
        print(f"{t:18} {a:>10} {b:>10}  {'OK (pg >= sqlite)' if b >= a else 'MISSING ROWS'}")
    for t in HISTORY:
        a = s.execute(text(f"SELECT count(*), max(ts) FROM {t}")).one()
        # SQLite stores ts as text; compare through the same python datetime value
        newest = s.execute(text(f"SELECT ts FROM {t} ORDER BY ts DESC LIMIT 1")).scalar()
        # the stored value is naive UTC: tell PostgreSQL so, otherwise it assumes the session's local zone
        b = d.execute(text(f"SELECT count(*) FROM {t} WHERE ts <= (CAST(:m AS timestamp) AT TIME ZONE 'UTC')"),
                      {"m": str(newest)}).scalar()
        print(f"{t:18} {a[0]:>10} {b:>10}  {'OK (equal up to ' + str(newest) + ')' if b == a[0] else 'DIFFERENT'}")
