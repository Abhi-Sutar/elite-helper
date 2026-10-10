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
- `journal/1` `FSDJump` / `Location` / `CarrierJump` → system info, faction influence, states, conflicts
- **Every accepted message is one row in an update log** (`market_update`, `system_update`: who, when, how many
  items, how many history rows it produced) = the exact **traffic** of each station and system.
- The **time series** (`price_history`, `faction_history`) stores a row only when something differs from the last
  logged state (see "History mode"), so nothing is lost but identical re-uploads are not stored 180 times.
  Commodity data older than 30 days (faction history 1 year, update log 1 year) is pruned by the listener or `prune`.

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
python -m eddn_helper price-history N0B-T4X gold --resample 60            # evenly spaced, last state carried forward
python -m eddn_helper traffic N0B-T4X --days 7 [--bucket 60] [--csv]      # how often a station is reported
python -m eddn_helper traffic Sol --systems                               # ... or a system's faction reports
python -m eddn_helper busiest [--days 1] [--limit 20] [--no-carriers] [--systems]   # who is reported most
python -m eddn_helper activity N0B-T4X                  # updates per day for a station (from the update log)
python -m eddn_helper commodity gold --sell --near Sol --ly 100 [--min-amount 500] [--no-carriers]
python -m eddn_helper commodity "liquid oxygen" --buy --limit 5
python -m eddn_helper trades Sol --ly 40 --cargo 256
python -m eddn_helper prune --days 30
```

Terms: `Buy` is what you pay the station (needs stock); `Sell` is what the station pays you (needs demand).

## Reading time series

| What | Command / MCP tool | Each point has |
|---|---|---|
| Price + amount of commodities at a station | `price-history <station> <commodity\|a,b\|all>` / `price_history` | `buy_price` (station sells; needs stock), `sell_price` (station buys; needs demand), `mean_price` (galactic average), `stock` (supply), `demand`, `stock_bracket`, `demand_bracket`, `listed` |
| How often a station / system is reported | `traffic <name>` / `station_traffic`, `system_traffic`; `busiest` / `busiest_stations`, `busiest_systems` | updates, distinct uploaders, price/faction rows written, per time bucket; leaderboards with share of all updates |
| A faction in a system, observation by observation | `factions <system> --history N` / `faction_history` | influence %, main state, happiness, active / pending / recovering states (pending/recovering with the game's trend), `present` |
| The same, as chart-ready intervals | `timeline <system> <faction>` / `faction_timeline` | influence series + intervals `{field, value, trend, start, last_seen, ended_at}` |
| Which systems a faction is in | `faction <name>` / `faction_presence` | influence, state, `history_rows`, `first_seen` per system |

Notes: a price of `0` means that side of the market is inactive (`--csv` leaves it empty so charts do not plot
zeros); stock and demand of `0` are real values; `listed: false` marks a point where the commodity was no longer on
sale (or `stale: true`, see below). In the default `changes` mode stored rows are **change points**: the value at any time is the latest row at
or before it (`state_at_start` gives the state when the window opens), `updates` is the true number of EDDN updates
(from the update log) and `rows` the number of stored change rows. `--resample MINUTES` gives evenly spaced samples
with the last state carried forward (and the updates in each step); series longer than `--max-points` are summarised
into time buckets with min/avg/max of prices, stock and demand; `--bucket MINUTES` picks the size and `--csv`
exports every point. Timeline intervals start at the stored row where a value appeared and end at the row where it
is gone (`ended_at`, empty while still present); `last_seen` comes from the update log; a `present=false` row means
the faction left the system.

Retention: market history 30 days; faction history 1 year; current snapshots always replaced in place. History starts
when the listener first saw a station or system (EDDN keeps nothing), and rows written before the 0003 migration
have no mean price, brackets, happiness or state lists.

## History mode: changes (default) / full / thresholded

`ELITE_HELPER_HISTORY_MODE` selects how `price_history` and `faction_history` are written. The update log
(`market_update`, `system_update`) is written in every mode.

- `changes` (**default**): a row only when the commodity/faction is new, a value differs from the last logged
  state (price, sell price, stock, demand, mean price, brackets; faction influence, state, happiness, any state
  or trend), a faction left (a `present=false` row), or an **anchor** is due (nothing logged for 24 h,
  `ELITE_HELPER_ANCHOR_HOURS`, 0 = off), so every window has a starting state even though old rows are pruned.
  Lossless: nothing about the state over time is missing, and every update time is in the update log. A message
  older than the current snapshot (a late upload) only adds to the update log.
  **Partial reports:** EDDN uploads are not always complete - the same station was seen reported with 368
  commodities by most uploaders and with 4 by another - so a commodity missing from one message is never treated
  as gone. The snapshot keeps it, each snapshot row has `seen_at` (last message containing it) and readers judge
  freshness per commodity; resampled series mark a state `stale` once it is older than 36 h (anchors refresh live
  items daily) instead of carrying it forward forever.
- `full`: a row for every reading. Roughly 40 million rows a day at current EDDN volume - PostgreSQL/Timescale
  only. A late upload is still logged but does not overwrite the snapshot.
- `thresholded` (legacy): only meaningful changes (5% price, 25% amount), at most hourly.

Why `changes`: in a 20-minute measurement 59% of price rows were exact repeats of the previous reading and only
about 1% of follow-up readings differed; a single busy station was re-reported every ~2 seconds.

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
