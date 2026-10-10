# Promote main to the live worktree and restart the scheduled listener.
# One-time setup:  git worktree add ..\elite-helper-live -b live main ; cd ..\elite-helper-live ; uv sync
param(
    [string]$LiveDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'elite-helper-live'),
    [string]$TaskName = 'EliteHelperListener'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $LiveDir)) { throw "Live worktree not found at $LiveDir (see the header of this script)." }

Push-Location $repo
& $py -m unittest discover tests
if ($LASTEXITCODE) { Pop-Location; throw 'tests failed - not deploying' }
Pop-Location

git -C $LiveDir merge --ff-only main
if ($LASTEXITCODE) { throw 'live worktree could not fast-forward to main' }
Push-Location $LiveDir
uv sync --frozen
if ($LASTEXITCODE) { Pop-Location; throw 'uv sync failed' }
& (Join-Path $LiveDir '.venv\Scripts\python.exe') -m eddn_helper migrate status
Pop-Location

# The listener migrates the database itself on start-up (with a SQLite backup first).
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Write-Host "Deployed. Check: $LiveDir\.venv\Scripts\python.exe -m eddn_helper status"
