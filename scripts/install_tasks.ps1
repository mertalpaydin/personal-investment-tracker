[CmdletBinding()]
param(
    [string]$TaskName = "PortfolioTrackerDailyUpdate",
    [string]$Time = "20:00",
    [string]$Mode = "SCHEDULED"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$vbsPath = "$repoRoot\scripts\run_minimized.vbs"
$batPath = "$repoRoot\scripts\daily_update.bat"

if (-not (Test-Path $vbsPath)) {
    throw "Launcher script not found: $vbsPath"
}
if (-not (Test-Path $batPath)) {
    throw "Batch script not found: $batPath"
}

Write-Host "Configuring Task Scheduler for Portfolio Tracker..." -ForegroundColor Cyan
Write-Host "  Repository Root : $repoRoot"
Write-Host "  Launcher Script : $vbsPath"
Write-Host "  Target Script   : $batPath"
Write-Host "  Mode            : $Mode"
Write-Host "  Scheduled Time  : $Time"

# Create action: wscript.exe """path\to\run_minimized.vbs"" ""path\to\script.bat"" <mode>"""
$actionArgs = "`"$vbsPath`" `"$batPath`" $Mode"
$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument $actionArgs `
    -WorkingDirectory $repoRoot

# Trigger: Daily at specified time
$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# Settings:
# - Window/Taskbar presence: Hidden is false so the minimized window shows up on taskbar
# - StartWhenAvailable: catch-up when laptop was asleep/off at trigger time
# - AllowStartIfOnBatteries, DontStopIfGoingOnBatteries
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Fetches missing prices and exchange rates for Portfolio Tracker, then recomputes (runs minimized to taskbar)." `
    -User $env:USERNAME `
    -Force | Out-Null

Write-Host "`nTask '$TaskName' registered successfully!" -ForegroundColor Green

# Display registered details
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, @{N='Execute';E={$_.Actions.Execute}}, @{N='Arguments';E={$_.Actions.Arguments}}, @{N='WorkingDirectory';E={$_.Actions.WorkingDirectory}} | Format-List
