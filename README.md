# elite-helper

Elite Dangerous helper scripts.

- `ed_credit_rate.py` — credits/hour from your local journal files (stdlib only; a thin wrapper over
  `eddn_helper/journal/`, which the `where` / `credit-rate` commands and MCP tools below share).
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

## Economies and finding systems

```
python -m eddn_helper economy Sol [--live]                       # system economy / second economy + its stations
python -m eddn_helper find-systems --economy Extraction,Refinery,Industrial --state Expansion --near Sol --ly 40
python -m eddn_helper find-systems --state Expansion --state-in pending --economy Extraction --demand bertrandite,coltan
```

`find-systems` (MCP: `find_systems`) returns systems matching **all** criteria, nearest to `--near` first (else most
populated first); `--csv` / `--json` export them.

| Option | Meaning |
|---|---|
| `--economy A,B` | any of these; case/space-insensitive (`High Tech` = `HighTech`, `Agriculture` = `Agri`) |
| `--economy-scope system\|station\|any` | system economy (default, plus the second economy unless `--no-second`), any station's economy, or either |
| `--state X --state-in any\|state\|active\|pending\|recovering` | a faction has state X as its main state, in its active/pending/recovering list, or anywhere (default `any`); `--controlling-only` limits it to the controlling faction |
| `--near SYSTEM --ly N`, `--min-pop N`, `--max-age-hours N` | distance, population and data-age filters |
| `--demand a,b` | a station currently has demand for any of these commodities |
| `--exclude-economy A,B` `--exclude-scope any\|system\|station` | reject a system whose economy, second economy or (scope `any`/`station`) any station's economy is one of these - "not at all" is the default |
| `--exclude-state X,Y` `--exclude-state-in ...` | reject systems where **any** faction has one of these states (e.g. `War,CivilWar`) |
| `--exclude-system A,B`, `--max-pop N` | drop named systems; maximum population (`--min-pop` exists too) |
| `--allegiance`, `--government`, `--security` | any-of lists, e.g. `--government Democracy,Corporate --security low,anarchy` |
| `--conflict yes\|no`, `--conflict-type War,CivilWar,Election` | systems with / without an active conflict, and of which type |
| `--faction NAME`, `--min-influence N`, `--max-influence N`, `--happiness A,B`, `--trend N`, `--controlling-only` | criteria on the **same** faction as `--state` (influence in percent; `--trend` is the raw number stored for pending/recovering states) |
| `--player-faction yes\|no` | the matching faction is / is not a player faction. EDDN does not say, so this comes from EDSM (stored by `refresh-stations`, `--refresh-stations` or a `--live` lookup); factions EDSM has not been asked about are **kept** and shown as `[?]` (`[P]` = player), and the reply counts them in `faction_players_unknown` |
| `--state-since-hours N` | with `--state`: the faction's **current run** of that state began within N hours. Worked out from the stored faction history, not stored itself; each hit shows when it began, and "or earlier" when history starts inside the run |
| `--min-stations N`, `--station-type coriolis,outpost` | at least N stations with a stored market; a station type containing one of the names |
| `--sort distance\|population\|influence\|freshest\|name` | ordering (default distance; most populated first without `--near`) |

Example - agricultural or industrial systems near Sol with a faction **pending Expansion** and no extraction or
refinery economy anywhere (system, second economy or stations):

```
python -m eddn_helper find-systems --economy Agri,Industrial --state Expansion --state-in pending --exclude-economy Extraction,Refinery --near Sol --ly 60
```

Example - industrial systems of at least a million people whose controlling **player faction** entered an active
Expansion in the last two days (run `--refresh-stations` once so player status is known; it fetches both from EDSM):

```
python -m eddn_helper find-systems --economy Industrial --min-pop 1000000 --controlling-only --player-faction yes --state Expansion --state-in active --state-since-hours 48 --refresh-stations
```

**Fetching what is missing.** Player status and stations come from EDSM, one system at a time (1 request/second, two
requests per system). Ways to fill them in:

- `find-systems ... --refresh-players --refresh-limit N` fetches **only** the player status of the matching factions
  (one request per system; a faction controlling several result systems is looked up once). `--refresh-stations`
  fetches stations too (two requests per system) for result systems that lack them.
- MCP `find_systems(..., refresh_missing=true, refresh_limit=10)` fetches only what the search needs (player status
  with `player_faction`; stations only with station filters or `refresh_stations=true`) from chat (replies list `refreshed` and
  `refresh_pending`; call again to continue). The MCP connects as the read-only reader, so set
  `ELITE_HELPER_INGEST_DATABASE_URL` to a writer URL in its `env`; only the EDSM fetch uses it. `system_info` /
  `system_economy` with `live_lookup=true` use it too.
- `refresh-factions [--limit N] [--controlling-only] [--near SYSTEM --ly N] [--dry-run]` checks every faction that has
  no player status yet in bulk, asking the system that covers the most unchecked factions each time. Needs a writer
  URL (`--db`). `--dry-run` only counts. `--controlling-only` skips factions that control nothing.

Each result lists the matching factions with where the state was found and the **raw trend** of pending/recovering
states, the system's stations with their economies, and the age of the faction data. Notes: this is a **current
snapshot** (economy changes are not kept as history) and only covers systems the listener (or an earlier `--live`
lookup) has seen; `economy --live` refreshes a missing/stale system from EDSM and lists stations EDSM knows that we have
no market for (not stored). Searching the whole galaxy via Spansh is not implemented yet.

## Your journals: where you are, credits per hour, history

The game's own journal files (default `%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous`, override with
`--journal-dir` or `ELITE_JOURNAL_DIR`) are used in two ways.

**Straight from the files (no database):**

```
python -m eddn_helper where [--enrich [--live]]     # system, station, ship, credits; --enrich adds EDDN data for the system/station
python -m eddn_helper credit-rate [--session] [--since 2026-10-01T00:00:00] [--max-gap-minutes 20] [--streams trading,bounty] [--crew-wages] [--from-db]
```

MCP: `where_am_i(enrich, live_lookup)` and `credit_rate(since_hours, session_only, max_gap_minutes, streams, include_crew, source)`.
`where` is the **last known** position: `game_running` is false after a `Shutdown` event (or a long-silent journal
without one) and `last_event_age_seconds` says how stale it is. `credits` is the balance at login; `status.balance`
(from `Status.json`) is live while the game runs. Credit rate sums active playtime per journal file (time between
sessions never counts) over trading, bounties, missions, exploration, carrier, carrier market (approximate, from
balance snapshots), maintenance, rebuys and crew wages; `ed_credit_rate.py` prints the same report.

**History and current state, from the database (the ED Discovery way):** every journal line is stored as an event
(raw JSON plus the system/station you were at), and each view replays the stored events.

| Command | MCP tool | Shows |
|---|---|---|
| `history` | `journal_search` | search all events by `--event`, `--system`, `--station`, `--text`, `--days/--since/--until` |
| `travel` | `travel_log` | jumps with distance, fuel, minutes stayed; totals |
| `visited [--stations]` | `visited_systems`, `visited_stations` | what you visited / docked at, counts, first and last |
| `sessions` | `play_sessions` | play sessions (one per journal file) |
| `ledger` | `ledger` | every credit movement by category, running total (`--category`) |
| `credit-rate --from-db` | `credit_rate` (default source) | the same analysis from stored events, much faster over the whole history |
| `cargo`, `materials` | `cargo`, `materials` | last snapshot moved forward through later trades / collection / crafting |
| `ships` | `ships` | current ship with modules and engineering, stored fleet |
| `missions` | `missions` | active, expired-unresolved, recently finished |
| `ranks` | `reputation` | ranks with progress, superpower reputation |
| `stats` | `commander_stats` | the game's Statistics snapshot (`--section`) |
| `carrier` | `carrier_status` | your fleet carrier as last reported |

All accept `--commander`, `--journal-dir`, `--no-sync`, `--json`; `history`, `travel` and `ledger` also `--csv`.

**Staying up to date - no service required.** Each of these queries first compares the database with the journal
files (a few `stat` calls plus one small query) and ingests only the new lines before answering, so the answer
includes the session you just played. Every reply carries `journal_freshness` (`db_behind_journal`, what was
ingested just now, newest event stored vs newest journal write, `ingest_error`). Ingest needs a database connection
that can write; the MCP normally uses the read-only `elite_reader` role, so give it a writer for this with
`ELITE_HELPER_INGEST_DATABASE_URL` (same format as `ELITE_HELPER_DATABASE_URL`, e.g. the `elite_writer` URL). Without
it the tools still answer from what is stored and say `db_behind_journal: true` with the reason (and `credit_rate`
falls back to reading the files). Only one ingest runs at a time (a lock file next to the database).

```
python -m eddn_helper journal-status               # state: current | behind | no_writer | no_journals; files, events, last ingest/error, ingester service
python -m eddn_helper journal-sync [--rebuild]     # ingest now (about 20-35 s for ~200 sessions the first time, then seconds)
python -m eddn_helper journal-sync --watch         # keep ingesting every 30 s while you play (the optional service)
```

MCP: `journal_sync_status`. **Optional background ingester:** `.\scripts\install-journal-task.ps1` registers task
`EliteHelperJournal` (at logon, `journal-sync --watch`, log `%LOCALAPPDATA%\elite-helper\journal-ingester.log`;
`restart-journal-task.ps1`, `uninstall-journal-task.ps1`). Nothing depends on it; with it running the on-demand check
simply finds nothing to do, and `journal-status` shows its heartbeat and task state. `ELITE_HELPER_JOURNAL_IGNORE`
(comma list of event names) skips storing noisy events; by default everything is stored (about 100 MB for 190k events
including indexes).

Code layout, so more can be added: `journal/files.py` (locate/read), `journal/state.py` (commander state; add an
event with `@handles("EventName")`), `journal/status.py` (`Status.json`), `journal/income.py` (credit rate, ledger),
`journal/ingest.py` (files -> database, freshness check, status), `journal/queries.py` (history queries) and
`journal/views.py` (current-state views), `journal/context.py` (`where`). Tests build synthetic journals
(`tests/test_journal*.py`). Deploying the database part applies migration `0006` (three new tables): the listener
migrates at start-up (`deploy.ps1` restarts it because migrations changed), and until then the MCP reports a schema
mismatch like for any other migration.

## Time zones

Everything is stored and returned in **UTC** (`timestamptz` on PostgreSQL, UTC on SQLite; `--json` and the MCP tools
always give `YYYY-MM-DDTHH:MM:SSZ`). To *show* local times in CLI tables and `--csv`, set
`ELITE_HELPER_DISPLAY_TZ` to `local` or an IANA name (`Europe/Berlin`); times then print with their offset, e.g.
`2026-10-11T12:54:03+02:00`. The default is `UTC`. (psql shows times in its own session zone; that is display only.)

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
- `faction_info` (revision 0007) holds one row per faction name with `is_player` from EDSM. It is written only by the
  EDSM paths, never by the listener; a faction with no row has unknown status.
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
- `.\scripts\deploy.ps1` — tests → fast-forward the live worktree to `main` → `uv sync` → restart the task **only if
  listener code changed** (listener/store/parsers/models/database/lock/migrate/config, migrations, dependencies).
  CLI/MCP-only changes leave the listener running; the MCP server picks them up when Claude restarts or reconnects it.

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

Tools: `data_status`, `system_info`, `system_economy`, `find_systems`, `faction_stations`, `faction_history`,
`faction_timeline`, `faction_presence`, `station_market`, `price_history`, `station_traffic`, `system_traffic`,
`busiest_stations`, `busiest_systems`, `station_activity`, `find_commodity`, `best_trades`, plus the journal tools
`where_am_i`, `credit_rate`, `journal_search`, `travel_log`, `visited_systems`, `visited_stations`, `play_sessions`, `ledger`,
`cargo`, `materials`, `ships`, `missions`, `reputation`, `commander_stats`, `carrier_status`, `journal_sync_status`, and the
Recce dashboard tools `dashboard_create`, `dashboard_update`, `dashboard_list`, `dashboard_open`, `dashboard_delete`.
The listener must be running separately (the journal tools only need the database, and `where_am_i` not even that).
To let the MCP ingest new journal lines itself, add `ELITE_HELPER_INGEST_DATABASE_URL` (a writer URL) to its `env`.
For Claude Code, the plain CLI above works as-is through the shell.

## Recce dashboard (live web page)

A small local server reads the database directly and serves auto-refreshing dashboards on `127.0.0.1`, so a dashboard
works in your normal browser, offline, with no dependency on the Claude app. The page is one HTML file with no
build step and no CDN. It follows light/dark from the system and scrolls its table sideways on a phone-width window.

The default dashboard is named `recce`: Agriculture systems of over 1M population within 100 ly of your current
position (from the journal), controlled by a player faction with Expansion pending or active. Columns: System,
Player controlling faction, Economy, Expansion pending for, Current state, Time since last update.

### Install and run (Windows first)

Nothing to install beyond the normal `uv sync`. `scripts\elite-dash.cmd` calls the project virtualenv, so put the
`scripts` folder on your PATH or call it by full path. Database access uses the usual `ELITE_HELPER_DATABASE_URL`
(a read-only role is fine).

```
scripts\elite-dash open                     # start the server if needed, open the default dashboard in the browser
scripts\elite-dash open close-in            # open another dashboard
scripts\elite-dash serve --port 8780        # run the server in the foreground instead
scripts\elite-dash recentre                 # anchor the default dashboard on the system you are in now
scripts\elite-dash list                     # names, titles and URLs (server not started by this)
scripts\elite-dash show recce --data        # config, plus one query run printed as JSON
scripts\elite-dash new close-in --origin "Praea Euq YE-Y b1-3" --radius 40 --economy Agriculture --min-pop 1000000 --state-in pending --refresh 20
scripts\elite-dash edit close-in --set radius_ly=60 --set filters.min_population=500000
scripts\elite-dash delete close-in
scripts\elite-dash status                   # count running servers; --kill-strays force-stops any that are not the registered one
scripts\elite-dash stop                     # ask the background server to exit
```

A second `open` reuses the running server (a `server.json` file plus a health check on its port; a stale file is
ignored). Ctrl+C stops a foreground server cleanly. `python -m eddn_helper.recce ...` is the same entry point, and
`pyproject.toml` declares `elite-dash` as a console script for when the project is installed.

### Claude / MCP

The MCP server gets six tools. Each result carries the dashboard URL.

| Tool | What it does |
|---|---|
| `dashboard_create(config)` | create or replace; starts the server if needed, returns `name` and `url` |
| `dashboard_update(name, patch)` | merge changes; open browsers live-reload |
| `dashboard_list()` | names, URLs, whether the server runs (read-only, starts nothing) |
| `dashboard_recentre(name)` | anchor it on your current system (from the journal) |
| `dashboard_open(name)` | start the server if needed and open the browser |
| `dashboard_delete(name)` | remove the config |

Example: `dashboard_create({"name": "industry", "filters": {"economy": "Industrial", "state_in": "pending"}, "radius_ly": 60})`.

### Config

One JSON file per dashboard in `%LOCALAPPDATA%\elite-helper\recce\dashboards` (`ELITE_HELPER_RECCE_DIR` moves the
folder). Missing keys take the defaults below; errors name the field (`refresh_seconds: must be at least 5, got 4`).

```json
{
  "name": "recce", "title": "Recce", "refresh_seconds": 20,
  "origin": "current", "radius_ly": 100,
  "filters": {"economy": "Agriculture", "min_population": 1000000, "controlling_only": true,
              "player_faction_only": true, "faction_state": "Expansion", "state_in": "any",
              "kinds": ["pending", "active"]},
  "columns": ["system", "faction", "economy", "pending_for", "state", "expansion_chance", "last_update"],
  "expansion_estimate": true,
  "sort": {"by": "state", "order": "asc"},
  "highlight_rules": [{"when": "active_for_under_hours", "hours": 48}],
  "live_fetch": {"enabled": true, "max_requests": 10},
  "stale_after_minutes": 60
}
```

- `origin`: a system name (the dashboard is **anchored** there) or `"current"` (it **follows** your position via
  `where_am_i`, last known position if the journal is quiet). A new dashboard created with origin `"current"` (the
  default, `elite-dash new` without `--origin`, `dashboard_create`) is anchored on the system you are in at that
  moment, so it stays put as you jump around. The page shows a "Recentre on <system>" button to move the anchor to
  where you are now (also `elite-dash recentre [name]` and the `dashboard_recentre` tool). To follow your position
  live instead, set the origin to `current` with `edit --set origin=current` or `dashboard_update`.
- The page header has a Radius box, a "Player factions only" switch and the Recentre button; each writes the dashboard's
  config (`radius_ly`, `filters.player_faction_only`, `origin`), so the CLI and MCP see the same values. With the switch off,
  NPC and unchecked factions are listed with a PLAYER / NPC / ? tag and no EDSM lookups are made.
- `state_in`: `any`, `pending`, `active` or `recovering`. With `any`, `kinds` picks which of `pending`, `active`,
  `recovering`, `other` are shown.
- `columns` (ordered): `system`, `faction`, `economy`, `pending_for`, `state`, `last_update`, `distance`,
  `population`, `influence`, `expansion_chance`. `sort.by`: `state`, `pending_for`, `system`, `faction`, `distance`, `population`,
  `influence`, `last_update`.
- `expansion_estimate` (default `true`) and the `expansion_chance` column: for each **pending** Expansion row the page shows a
  Likely / Possible / Unlikely / Uncertain chip with a percentage. Click the row to see which rules passed, failed or
  could not be checked, with a confidence tag on each. It is a heuristic built from developer comments and community
  tracking (75% influence threshold, 7-faction cap, 20/30/60 ly search ladder, War/Civil War/Election holding the
  countdown, happiness tie-break between origins), not an official calculation. The rules are data in
  `eddn_helper/recce/expansion_rules.py`: edit the numbers, wording or weights there when the mechanics change, and
  add a function to `expansion.CHECKS` only for a new kind of rule.
- `highlight_rules`: `active_for_under_hours` or `pending_for_over_hours`, re-checked in the browser every second.

### How it behaves

- **Refresh rule.** The server checks `newest_faction_update` (and reads `newest_market_update` for display) with
  one cheap query every 5 s. It re-runs the full query only when that timestamp, or your current system, moved
  **and** at least `refresh_seconds` (default 20) have passed since the last refresh: the slower of the two. Market
  uploads alone do not trigger a refresh because no column uses market data. Editing a config reloads at once.
- **Pending for.** From `faction_history`: the start of the current unbroken run of the state. "at least 5.2 h" when
  stored history starts inside the run, "5.2 h to 9 h" when the previous reading without the state is known,
  "unknown" when there is no history in the last 14 days. Cached per system and faction until the faction row
  changes. Active rows read "active for ...". Rows with data older than 24 h are marked stale.
- **Time since last update** counts up in the browser every second from the data age the database reports.
- **Status pill.** Live, Stale (the database itself has not changed for `stale_after_minutes`) or Error (the last
  attempt failed; the last good data stays on screen), with the last refresh time and the database timestamp.
  Queries run in worker threads, so a slow one never blocks the server.
- **Database URLs.** `%LOCALAPPDATA%\elite-helper\recce\settings.json` can hold `"database_url"` (the reader; the
  default SQLite file is used without it) and `"ingest_database_url"` (the writer for the EDSM fetch), e.g.
  `{"database_url": "postgresql+psycopg://elite_reader@localhost:5432/elite?passfile=...", "ingest_database_url": "postgresql+psycopg://elite_writer@localhost:5432/elite?passfile=..."}`.
  `ELITE_HELPER_DATABASE_URL` and `ELITE_HELPER_INGEST_DATABASE_URL`, if set, take precedence. Restart the server
  (`elite-dash stop`, then `open`) after adding it.
- **Read-only.** Normal queries and polling use only the reader connection. The one exception is the limited EDSM
  fetch: factions whose player status is unknown are hidden, and when a writer URL is configured (see above) the
  server asks EDSM for their status, at most `live_fetch.max_requests` (hard cap 10) requests per refresh, only for
  factions that lack it, then waits 10 minutes if any remain unknown. Results are stored by the existing
  `refresh_missing` code. Without that variable nothing is fetched or written.

## CG dashboard (community-goal supply board)

The same server also shows dashboards of kind `cg`: for each commodity tab, the large-pad stations within a few laden
jumps of the delivery system that sell it, with stock, loads, price, margin, distance from the star (Ls) and data age,
ranked by a score (loads available, close to the star, fresh data). Open it like any dashboard: `elite-dash open cg`
(or the `dashboard_open` tool). The page updates live, copies a system name with one click, and the server is shared
with the Recce dashboards, so there is one port and one process.

- **Config** (`dashboards\cg.json`, edited like any dashboard: `elite-dash edit cg --set target.system="HIP 69861"`, or
  `dashboard_update`): `target {name, system}` (system name, or `"current"` to follow your journal position), `ship
  {laden_ly, unladen_ly, cargo}`, `station_types` (default: the large pads), `min_stock`, `max_age_hours`, `lookup_ls`
  and `tabs [{key, commodity, label, cg_price, max_jumps | max_ly, min_stock, station_types, max_age_hours}]`.
  Changes apply within one refresh, no restart.
- **Moving the target**: `dashboard_update` with a `target` patch, or `cg_board_set_target(system, station?)`, which also
  checks that the system is in the database with coordinates. `cg_board_target()` reads it. Moving the target restarts
  the Change baselines, since they only mean something around one target.
- **Change baselines**: stock now minus stock when a station first appeared on the tab. `elite-dash reset cg [tab]`
  restarts them. State lives in `recce\cg\` (baselines per dashboard, one shared EDSM cache).
- **Station types**: market data carries no station type, so for stations the database has no type for, the server asks
  EDSM (once per system, cached, 6 systems per refresh) and drops the ones that are not large pads. Until EDSM answers a
  station is kept and flagged `PAD ?`. The same lookup gives Ls. `lookup_ls: false` turns EDSM off.
- **From the old standalone script**: `elite-dash import-cg` imports the old `cg_board/` folder's config, baselines and
  EDSM cache into a `cg` dashboard (the old files are copied to `recce\cg\legacy`).

## Local config and templates

Machine-specific files (dashboard configs, `settings.json` with database URLs, baselines, server registration, the
pgpass file) are not in git; the patterns are in `.gitignore`. `config-templates/` has a template for each plus the table of
environment variables and where every real file lives. A test keeps the templates valid and the variable table complete.

## Limitations

- Only systems/stations that players visited while the listener ran are present (use `--live` to fill gaps).
- EDDN commodity data has no pad size, and distances need the system's coordinates to be known.
- Faction state names from EDDN have no spaces (`PublicHoliday`); EDSM's have (`Public Holiday`).

## Tests

```
.venv\Scripts\python.exe -m unittest discover tests
```
