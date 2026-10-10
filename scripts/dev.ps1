# Development helpers that never touch the live database file directly.
#   .\scripts\dev.ps1 snapshot   copy the live DB (SQLite backup API, safe while the listener writes) to a dev DB
#   .\scripts\dev.ps1 listen     run a second listener against the dev DB (own lock file, EDDN allows many subscribers)
#   .\scripts\dev.ps1 test       run the unit tests
param(
    [Parameter(Mandatory = $true)][ValidateSet('snapshot', 'listen', 'test')][string]$Command,
    [string]$DevDb = (Join-Path $env:TEMP 'eddn_dev.sqlite')
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No venv at $py - run 'uv sync' in $repo first." }
Push-Location $repo
try {
    switch ($Command) {
        'snapshot' { & $py -m eddn_helper backup $DevDb; if ($LASTEXITCODE) { throw 'backup failed' } }
        'listen'   { & $py -m eddn_helper --db $DevDb listen }
        'test'     { & $py -m unittest discover tests; if ($LASTEXITCODE) { throw 'tests failed' } }
    }
} finally { Pop-Location }
