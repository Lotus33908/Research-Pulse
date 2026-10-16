# Registers a Windows Task Scheduler job that refreshes the publications every Monday at 08:00.
# Run once from PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here "run.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName "AKSOB Research Tracker - weekly refresh" `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled: 'AKSOB Research Tracker - weekly refresh' runs $runner every Monday at 08:00."
Write-Host "Run it now with:  Start-ScheduledTask -TaskName 'AKSOB Research Tracker - weekly refresh'"
