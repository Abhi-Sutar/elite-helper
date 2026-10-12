param([string]$TaskName = 'EliteHelperJournal')
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object LastRunTime, LastTaskResult
