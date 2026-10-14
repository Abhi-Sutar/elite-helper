# Config templates

Everything here is a **template**. The real, machine-specific files are not in git (see the "Local config" block of
`.gitignore`): they hold URLs/paths of your machine and are rewritten while the programs run (dashboard edits from
`elite-dash`, the MCP `dashboard_*` tools or the browser pages; baselines; server registration).

## Where the real files live

`%LOCALAPPDATA%\elite-helper\` (override the Recce part with `ELITE_HELPER_RECCE_DIR`):

| Template | Real file | Written by |
|---|---|---|
| `settings.json.example` | `recce\settings.json` | you (reader / writer database URLs for the dashboards) |
| `dashboards/recce.json.example` | `recce\dashboards\recce.json` | you, `elite-dash new/edit`, `dashboard_create/update`, the page's radius and player-only toggles |
| `dashboards/cg.json.example` | `recce\dashboards\cg.json` | you, `elite-dash edit cg`, `dashboard_update`, `cg_board_set_target` |
| `pgpass.conf.example` | `pgpass.conf` | `scripts\create-pgpass.ps1` |
| - | `recce\server.json`, `recce\server.log`, `recce\cg\*.baseline.json`, `recce\cg\edsm-cache.json` | the dashboard server (never edit by hand) |

Copy a template, drop the `.example` suffix, adjust it. Dashboards are validated when loaded: a wrong key is reported
with its field path and the last good config keeps running. `elite-dash new <name>` and `dashboard_create` create
them for you.

**Claude desktop app caveat:** files that Claude's own tools write under `%LOCALAPPDATA%` land in the app's private
copy (`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Local\...`) and are not visible in your terminal. To share one
folder between the app and your terminal, point `ELITE_HELPER_RECCE_DIR` at a folder outside AppData for both: set it as
a user environment variable for your terminal, and in the `env` block of the `elite-helper` server in
`%APPDATA%\Claude\claude_desktop_config.json` for the app's MCP servers (then quit and reopen Claude). For example
`C:\Users\<you>\elite-helper-data\recce`, or `<repo>\local\recce`, which is git-ignored. Without that, a dashboard changed
through Claude is written to the private copy, and a second dashboard server can be started that your terminal cannot see.

## Environment variables

| Variable | Meaning |
|---|---|
| `ELITE_HELPER_DATABASE_URL` | database for the CLI, listener and MCP reader (default: SQLite file in the data dir) |
| `ELITE_HELPER_INGEST_DATABASE_URL` | writer URL: journal ingest on demand and the limited EDSM fetches when the normal URL is read-only |
| `ELITE_HELPER_RECCE_DIR` | where dashboards, `settings.json`, `server.json` and the cg state live (default `%LOCALAPPDATA%\elite-helper\recce`) |
| `ELITE_HELPER_RECCE_POLL` | seconds between the dashboard server's cheap "did the database move?" checks (default 5) |
| `ELITE_HELPER_HISTORY_MODE` | `changes` (default), `full` or `thresholded` time-series storage |
| `ELITE_HELPER_ANCHOR_HOURS` | in `changes` mode, write an unchanged value again after this many hours |
| `ELITE_HELPER_DISPLAY_TZ` | timezone for CLI tables/CSV: `UTC` (default), `local` or an IANA name |
| `ELITE_HELPER_JOURNAL_IGNORE` | journal events the ingester skips |
| `ELITE_JOURNAL_DIR` | journal folder (default: Saved Games) |
| `TEST_DATABASE_URL` | run the store/query tests on PostgreSQL (the `elite_test` database) instead of in-memory SQLite |
