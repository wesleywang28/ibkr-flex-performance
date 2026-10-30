<#
  Registers a Windows scheduled task that pulls the previous trading day's IBKR data
  every Tuesday-Saturday at 07:00. If the computer is off or asleep at that time,
  the task runs as soon as possible afterwards.

  Parameters:
    -Remove  delete the scheduled task
#>
param([switch]$Remove)

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
$trigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At '07:00'
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Daily pull of the IBKR Flex report into a local folder' -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName (Tue-Sat 07:00)" -ForegroundColor Green
Write-Host 'Run history: data\pull_log.txt'
