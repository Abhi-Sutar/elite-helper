# elite-helper

Elite Dangerous helper scripts.

- `ed_credit_rate.py` — credits/hour from your local journal files (stdlib only).
- `eddn_helper/` — Inara-style **commodity prices** and **system faction states** collected live from
  [EDDN](https://github.com/EDCD/EDDN), with a command-line tool and an MCP server so Claude can query it.

## How it works

EDDN is a live firehose with no history, so a background **listener** subscribes to
`tcp://eddn.edcd.io:9500` and stores what players' game clients upload into a database (SQLite today,
PostgreSQL/TimescaleDB later — the code is SQLAlchemy 2.0 + Alembic and switches by changing the DB URL):

- `commodity/3` messages → station markets (buy/sell price, stock, demand, age)
- `journal/1` `FSDJump` / `Location` / `CarrierJump` → system info, faction influence, states, conflicts,
  plus a change-only influence history
- Commodity prices also form a **time series** (`price_history`): a row is added when a price moves 5%+ or
  stock/demand 25%+, at most once an hour per commodity per station. Commodity data older than 30 days is
  pruned hourly by the listener (or `prune`).

Every query result carries the **age** of its data. `--live` (CLI) / `live_lookup` (MCP) fetches a system or
station from [EDSM](https://www.edsm.net) when local data is missing or stale.

## Setup (uv)

```
uv sync                                   # creates .venv from pyproject.toml / uv.lock
.venv\Scripts\python.exe -m eddn_helper listen
```

Database location: `--db <url-or-path>`, else `ELITE_HELPER_DATABASE_URL`, else
`%LOCALAPPDATA%\elite-helper\elite.sqlite`. Only one listener can run per database (OS file lock next to
the database); a second one exits with the first one's PID.

## CLI

All commands accept `--json`.

```
python -m eddn_helper status
python -m eddn_helper system Sol [--live]
python -m eddn_helper factions Sol --history 14 [--faction "Mother Gaia"] [--csv]
python -m eddn_helper timeline Sol "Mother Gaia" --days 30 [--csv]        # state/happiness intervals + influence
python -m eddn_helper faction "Mother Gaia"                              # every system it is in + how much history exists
python -m eddn_helper market Daedalus --system Sol [--live]
python -m eddn_helper price-history Daedalus gold --system Sol --days 14 [--bucket 60] [--csv]
python -m eddn_helper price-history N0B-T4X all --days 7 --csv > carrier.csv   # every commodity at a station
python -m eddn_helper activity N0B-T4X                  # how many updates are stored for a station, per day
python -m eddn_helper commodity gold --sell --near Sol --ly 100 [--min-amount 500] [--no-carriers]
python -m eddn_helper commodity "liquid oxygen" --buy --limit 5
python -m eddn_helper trades Sol --ly 40 --cargo 256
python -m eddn_helper prune --days 30
```

Terms: `Buy` is what you pay the station (needs stock); `Sell` is what the station pays you (needs demand).

## Reading time series

| What | Command / MCP tool | Each point has |
|---|---|---|
| Price + amount of commodities at a station | `price-history <station> <commodity\|a,b\|all>` / `price_history` | `buy_price` (station sells; needs stock), `sell_price` (station buys; needs demand), `mean_price` (galactic average), `stock` (supply), `demand`, `stock_bracket`, `demand_bracket` |
| A faction in a system, observation by observation | `factions <system> --history N` / `faction_history` | influence %, main state, happiness, active / pending / recovering states (pending/recovering with the game's trend), `present` |
| The same, as chart-ready intervals | `timeline <system> <faction>` / `faction_timeline` | influence series + intervals `{field, value, trend, start, last_seen, ended_at}` |
| Which systems a faction is in | `faction <name>` / `faction_presence` | influence, state, `history_rows`, `first_seen` per system |

Notes: a price of `0` means that side of the market is inactive (`--csv` leaves it empty so charts do not plot
zeros); stock and demand of `0` are real values. Series longer than `--max-points` come back summarised in time
buckets with min/avg/max of prices, stock and demand; `--bucket MINUTES` picks the size and `--csv` exports every
point. Timeline intervals start at the first observation in the window and end at the first observation where the
value is gone (`ended_at`, empty while still present); a `present=false` row means the faction left the system.

Retention: market history 30 days; faction history 1 year; current snapshots always replaced in place. History starts
when the listener first saw a station or system (EDDN keeps nothing), and rows written before the 0003 migration
have no mean price, brackets, happiness or state lists.

## History mode: thresholded vs full

`price_history` is written in one of two modes (`ELITE_HELPER_HISTORY_MODE`, default by database):

- `thresholded` (default on SQLite): a row only when a price moves 5%+ or stock/demand 25%+, at most once an
  hour per commodity per station (factions: when influence, state, happiness or a state list changes). Small, but
  not every update.
- `full` (default on PostgreSQL): one row per commodity for **every** accepted market update, kept 30 days.
  That is roughly 40 million rows a day at current EDDN volume, so it is meant for PostgreSQL + TimescaleDB
  (hypertable + compression + retention policy), not SQLite. A late upload that is older than the current
  snapshot still goes into the history but does not overwrite the snapshot.

Long series are summarised into time buckets (min/avg/max, last stock/demand) once they exceed `--max-points`
(default 500) so replies stay small; `--bucket MINUTES` picks the size.

## PostgreSQL + TimescaleDB setup (Windows)

Install PostgreSQL 17 and the TimescaleDB 2.30.x Windows add-on, then verify with
`psql -U postgres -f scripts\pg-smoke-test.sql`. After that:

```
psql -U postgres -f scripts\pg-setup.sql            # roles elite_writer / elite_reader, databases elite, elite_dev, elite_test
psql -U postgres -c "\password elite_writer"        # hidden prompts - passwords are never stored in the repo
psql -U postgres -c "\password elite_reader"
.\scripts\create-pgpass.ps1                         # writes %LOCALAPPDATA%\elite-helper\pgpass.conf (only you can read it)
```

Database URLs then carry no password (libpq reads it from the passfile):

```
postgresql+psycopg://elite_writer@localhost:5432/elite?passfile=C:/Users/<you>/AppData/Local/elite-helper/pgpass.conf
postgresql+psycopg://elite_reader@localhost:5432/elite?passfile=C:/Users/<you>/AppData/Local/elite-helper/pgpass.conf
```

Use the writer URL for the listener, `migrate` and `copy-data`, and the reader URL for the MCP server
(`ELITE_HELPER_DATABASE_URL`). Revision `0002` turns `price_history` into a compressed hypertable
(1-day chunks, compressed after 2 days, dropped after 30 days) when the `timescaledb` extension is present;
on SQLite it does nothing. Run the whole test suite on PostgreSQL with the disposable `elite_test` database
(the tests wipe it!):

```
$env:TEST_DATABASE_URL = "postgresql+psycopg://elite_writer@localhost:5432/elite_test?passfile=C:/Users/<you>/AppData/Local/elite-helper/pgpass.conf"
.venv\Scripts\python.exe -m unittest discover tests
```

### Choosing a compression layout

`price_history` and `faction_history` start with a layout chosen from a tiny sample. Once a day or two of full
data exists, compare the candidates on your real data (production tables are only read):

```
psql -h localhost -U elite_writer -d elite -v span="2 days" -v fspan="14 days" -f scripts\pg-compression-bench.sql
```

It prints size, compression ratio and query timings per layout. Apply the winner with a new Alembic revision
(`ALTER TABLE ... SET (timescaledb.compress_segmentby = ..., timescaledb.compress_orderby = ...)`).

## Moving the data to another database (SQLite -> PostgreSQL)

```
# 1. create an empty database and bring it to the current schema
python -m eddn_helper --db postgresql+psycopg://elite_writer@localhost/elite migrate upgrade
# 2. stop the listener that writes to the SQLite file, then copy everything across
python -m eddn_helper copy-data --from C:\path\to\elite.sqlite --to postgresql+psycopg://elite_writer@localhost/elite
```

`copy-data` copies parents before children in batches, can be re-run safely (existing rows are skipped), keeps
commodity ids and reports source vs destination row counts per table. History copied from SQLite stays
thresholded; full detail starts when the listener begins writing to PostgreSQL.

## Database schema and migrations (Alembic)

`eddn_helper/models.py` is the schema source of truth; revisions live in `eddn_helper/migrations/versions`.

```
python -m eddn_helper migrate status          # current vs head revision
python -m eddn_helper migrate upgrade         # apply pending revisions (SQLite is backed up first)
python -m eddn_helper migrate new "add foo"   # development: autogenerate a revision from the models
python -m eddn_helper backup C:\temp\copy.sqlite
```

- Only `listen` (at start-up) and `migrate upgrade` change the schema. CLI/MCP reads refuse to run against a
  database whose schema is behind or ahead of the code and tell you what to do.
- A migration runs in one transaction (rolls back fully on failure) and concurrent upgraders serialise.
- Large rebuilds lock the database for writes; stop the listener first for those.
- Import data from the old pre-SQLAlchemy file (`eddn.sqlite`): `python -m eddn_helper import-legacy [--from <file>]`.

## Developing while the listener runs

Python loads code once at start, so editing files never changes a running listener. Recommended setup:

```
git worktree add ..\elite-helper-live -b live main
cd ..\elite-helper-live ; uv sync            # the live listener runs from this folder
```

- `.\scripts\dev.ps1 snapshot` — consistent copy of the live DB into `%TEMP%\eddn_dev.sqlite` for experiments.
- `.\scripts\dev.ps1 listen` — second listener on the dev copy (EDDN allows many subscribers).
- `.\scripts\dev.ps1 test` — unit tests.
- `.\scripts\deploy.ps1` — tests → fast-forward the live worktree to `main` → `uv sync` → restart the task.

## Start at boot (Task Scheduler)

From an **elevated** PowerShell, once:

```
.\scripts\install-task.ps1          # task EliteHelperListener, at startup, runs as you without a stored password
Start-ScheduledTask -TaskName EliteHelperListener
```

Logs: `%LOCALAPPDATA%\elite-helper\listener.log` (rotating). `restart-task.ps1` and `uninstall-task.ps1` are
alongside. The listener retries the database for up to 5 minutes at start-up, restarts automatically if it
dies, and reconnects after ~90 s of silence (e.g. after sleep). Sleep/hibernate still pauses collection.

## MCP server (Claude Desktop / Cowork)

```json
{
  "mcpServers": {
    "elite-helper": {
      "command": "C:\\path\\to\\elite-helper-live\\.venv\\Scripts\\python.exe",
      "args": ["-m", "eddn_helper.mcp_server"],
      "cwd": "C:\\path\\to\\elite-helper-live",
      "env": { "ELITE_HELPER_DATABASE_URL": "sqlite:///C:/Users/<you>/AppData/Local/elite-helper/elite.sqlite" }
    }
  }
}
```

Tools: `data_status`, `system_info`, `faction_history`, `faction_presence`, `station_market`,
`price_history`, `find_commodity`, `best_trades`. The listener must be running separately.
For Claude Code, the plain CLI above works as-is through the shell.

## Limitations

- Only systems/stations that players visited while the listener ran are present (use `--live` to fill gaps).
- EDDN commodity data has no pad size, and distances need the system's coordinates to be known.
- Faction state names from EDDN have no spaces (`PublicHoliday`); EDSM's have (`Public Holiday`).

## Tests

```
.venv\Scripts\python.exe -m unittest discover tests
```
