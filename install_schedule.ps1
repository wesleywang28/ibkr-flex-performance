<#
  Registers a Windows scheduled task that pulls your IBKR data once a day.
  If the computer is off or asleep at that time, the task runs as soon as possible afterwards.

  IBKR publishes the previous day's Flex data overnight US Eastern time, so pick a local time
  after roughly 06:00 ET (e.g. 07:00 in the US, 14:00 in Europe, 19:00-20:00 in Asia).

  Parameters:
    -At '07:00'  local time to run (default 07:00)
    -Remove      delete the scheduled task

  Examples:
    setup_schedule.bat
    setup_schedule.bat -At 19:30
    setup_schedule.bat -Remove
#>
param(
    [string]$At = '07:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$TaskName = 'IBKR Flex Daily Pull'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Green
    exit 0
}

$Root   = $PSScriptRoot
$Script = Join-Path $Root 'pull_ibkr.ps1'
if (-not (Test-Path (Join-Path $Root 'config\flex_credentials.xml'))) {
    Write-Host 'Run run_pull.bat first to complete the setup and a successful pull, then install the schedule.' -ForegroundColor Yellow
    exit 1
}

$arg = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Scheduled"
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Root
$trigger   = New-ScheduledTaskTrigger -Daily -At $At
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Daily pull of the IBKR Flex report into a local folder' -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName (daily at $At)" -ForegroundColor Green
Write-Host 'Run history: data\pull_log.txt'
