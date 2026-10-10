#requires -RunAsAdministrator
# Register the EDDN listener to start at boot (before login) under your account, no stored password (S4U).
# Run once from an elevated PowerShell:  .\scripts\install-task.ps1 [-LiveDir <path>] [-DbUrl <url>]
param(
    [string]$LiveDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'elite-helper-live'),
    [string]$TaskName = 'EliteHelperListener',
    [string]$DbUrl = ('sqlite:///' + ($env:LOCALAPPDATA -replace '\\', '/') + '/elite-helper/elite.sqlite'),
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA 'elite-helper\listener.log')
)
$ErrorActionPreference = 'Stop'
$py = Join-Path $LiveDir '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No venv at $py - create the live worktree and run 'uv sync' there first." }
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

$action = New-ScheduledTaskAction -Execute $py -WorkingDirectory $LiveDir `
    -Argument "-m eddn_helper --db `"$DbUrl`" listen --log-file `"$LogFile`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Collects EDDN market and faction data into the elite-helper database.' -Force | Out-Null
Write-Host "Registered task '$TaskName'. Start it now with: Start-ScheduledTask -TaskName $TaskName"
