# Promote main to the live worktree; restart the scheduled listener only if listener code changed.
# One-time setup:  git worktree add ..\elite-helper-live -b live main ; cd ..\elite-helper-live ; uv sync
param(
    [string]$LiveDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'elite-helper-live'),
    [string]$TaskName = 'EliteHelperListener',
    [string]$DbUrl   # database to check; default: the URL the scheduled listener runs with, else $env:ELITE_HELPER_DATABASE_URL
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $LiveDir)) { throw "Live worktree not found at $LiveDir (see the header of this script)." }

# Which database the migrate-status lines refer to. Without --db the CLI falls back to the default SQLite file,
# which is not the live PostgreSQL database.
if (-not $DbUrl) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $taskArgs = if ($task) { ($task.Actions | Select-Object -First 1).Arguments } else { $null }
    if ($taskArgs -match '--db\s+"([^"]+)"') { $DbUrl = $Matches[1] } elseif ($taskArgs -match '--db\s+(\S+)') { $DbUrl = $Matches[1] }
}
if (-not $DbUrl) { $DbUrl = $env:ELITE_HELPER_DATABASE_URL }
if ($DbUrl) { $dbArgs = @('--db', $DbUrl) } else { $dbArgs = @(); Write-Warning 'No database URL found (task, -DbUrl, ELITE_HELPER_DATABASE_URL): checking the default SQLite file, not the live database.' }
$liveDbName = if ($DbUrl) { ($DbUrl -replace '\?.*$', '') } else { 'default SQLite file' }

Push-Location $repo
& $py -m unittest discover tests
if ($LASTEXITCODE) { Pop-Location; throw 'tests failed - not deploying' }
Pop-Location

$before = (git -C $LiveDir rev-parse HEAD).Trim()
git -C $LiveDir merge --ff-only main
if ($LASTEXITCODE) { throw 'live worktree could not fast-forward to main' }
$after = (git -C $LiveDir rev-parse HEAD).Trim()

# The listener only needs a restart when code it runs (or its dependencies) changed; CLI/MCP-only changes don't.
# If the changed files can't be determined, restart to be safe.
$listenerFiles = '^(eddn_helper/(listener|store|parsers|models|database|lock|migrate|config)\.py|eddn_helper/migrations/|pyproject\.toml|uv\.lock)'
$changed = @(git -C $LiveDir diff --name-only $before $after)
$restart = ($LASTEXITCODE -ne 0) -or [bool]($changed | Where-Object { $_ -match $listenerFiles })
Push-Location $LiveDir
uv sync --frozen
if ($LASTEXITCODE) { Pop-Location; throw 'uv sync failed' }
$livePy = Join-Path $LiveDir '.venv\Scripts\python.exe'
Write-Host "Database: $liveDbName"
Write-Host -NoNewline 'Before deploy: '
& $livePy -m eddn_helper @dbArgs migrate status
Pop-Location

if ($restart) {
    # The listener migrates the database itself on start-up (with a SQLite backup first).
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'Listener restarted (listener code or dependencies changed); waiting for it to migrate the database...'
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $status = (& $livePy -m eddn_helper @dbArgs migrate status) -join ' '
        if ($status -match 'state=ok') { break }
    }
    Write-Host "After restart: $status"
} else {
    Write-Host 'Listener restart not required (MCP/CLI-only change); it keeps running.'
}
Write-Host "Deployed. Check the live database with:"
if ($DbUrl) { Write-Host "  $livePy -m eddn_helper --db `"$DbUrl`" status" } else { Write-Host "  $livePy -m eddn_helper status" }
Write-Host 'MCP server changes load when Claude restarts or reconnects the elite-helper MCP server.'
