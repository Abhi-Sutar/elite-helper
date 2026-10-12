# OPTIONAL: run the journal ingester as a background task so the database follows your journals while you play.
# Nothing depends on it - journal queries ingest on demand when the database is behind (see README "Your journals").
# Registers 'EliteHelperJournal' to run `journal-sync --watch` at logon under your account (no admin needed).
# Run once:  .\scripts\install-journal-task.ps1 [-LiveDir <path>] [-DbUrl <writer url>]
param(
    [string]$LiveDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'elite-helper-live'),
    [string]$TaskName = 'EliteHelperJournal',
    [string]$DbUrl = ('sqlite:///' + ($env:LOCALAPPDATA -replace '\', '/') + '/elite-helper/elite.sqlite'),
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA 'elite-helper\journal-ingester.log'),
    [int]$IntervalSeconds = 30
)
$ErrorActionPreference = 'Stop'
$py = Join-Path $LiveDir '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No venv at $py - create the live worktree and run 'uv sync' there first." }
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

$action = New-ScheduledTaskAction -Execute $py -WorkingDirectory $LiveDir `
    -Argument "-m eddn_helper --db `"$DbUrl`" journal-sync --watch --interval $IntervalSeconds --log-file `"$LogFile`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Ingests Elite Dangerous journal files into the elite-helper database.' -Force | Out-Null
Write-Host "Registered task '$TaskName'. Start it now with: Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with: python -m eddn_helper journal-status   (log: $LogFile)"
