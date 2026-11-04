# Register the daily morning scan as a Windows scheduled task. Run this yourself.
#
#   .\tools\register_morning_scan.ps1 -ProfileName <name> -At 07:30 -Weekdays
#
# The scan reads the feed and drafts comments. It never posts. It runs only while
# you are logged in, because Chrome needs your desktop session, and it does not
# wake the PC.
#
# -AllowProduction lets the scan run unattended against a declared production
# identity. Leave it off and the scan refuses that identity, which shows up as
# FAILED / production_access_refused in review_status.json.
param(
  [Parameter(Mandatory = $true)][string]$ProfileName,
  [string]$At = "07:30",
  [switch]$Weekdays,
  [switch]$AllowProduction,
  [string]$TaskName = "LinkedIn Morning Scan - $ProfileName"
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No Python found at $python" }

$arguments = "-m linkedin_automation.morning_scan --profile `"$ProfileName`""
if ($AllowProduction) { $arguments += " --allow-production" }

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
if ($Weekdays) {
  $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
} else {
  $trigger = New-ScheduledTaskTrigger -Daily -At $At
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered '$TaskName' at $At. Allow production: $AllowProduction"
