# PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
# Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md). Commercial use requires written permission.
<#
.SYNOPSIS
  Schedules PC TidyUp to run every week, so a fresh report (with "since last run" trends) is always waiting.

.EXAMPLE
  .\Register-TidyUpTask.ps1                          # Mondays 12:30, as you
.EXAMPLE
  .\Register-TidyUpTask.ps1 -DayOfWeek Friday -At 16:00 -Elevated   # run with highest privileges (needs admin)
.EXAMPLE
  .\Register-TidyUpTask.ps1 -Unregister
.NOTES
  Reports land in .\reports (TidyUp-Report-latest.html is always the newest).
  The task only scans and reports; it never deletes anything.
#>
[CmdletBinding()]
param(
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string]$DayOfWeek = 'Monday',
    [string]$At = '12:30',
    [string]$TaskName = 'PC TidyUp weekly storage review',
    [switch]$Elevated,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'Run-TidyUp.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Quiet" `
    -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive `
    -RunLevel $(if ($Elevated) { 'Highest' } else { 'Limited' })

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'PC TidyUp storage review: writes reports\TidyUp-Report-latest.html. Never deletes anything.' -Force | Out-Null
Write-Host "Scheduled '$TaskName' every $DayOfWeek at $At. Latest report: $here\reports\TidyUp-Report-latest.html"
Write-Host "Run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
