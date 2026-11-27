#Requires -Version 5.1
<#
.SYNOPSIS
  Install a Windows Scheduled Task that starts Miner Hearth (source version) at boot,
  whether or not you are logged in.

.NOTES
  v1.8: the task name stays "MinerAutotune" so an existing task keeps working.
  If you use MinerHearth.exe (the app window), you do NOT need this
  task. Double-click enable-autostart.bat instead; it starts the app window
  when you sign in. This scheduled task is for the headless source version
  (autotune.py + browser dashboard) that runs even when nobody is logged in.
  Do not run both at the same time: they would poll and tune the same miners.

  Run from an elevated PowerShell prompt (right-click PowerShell ->
  Run as administrator), in this folder:

    powershell -ExecutionPolicy Bypass -File .\install-task.ps1

  You will be asked for your Windows password so the task can run
  when nobody is logged in. The password is used only to register the
  task; it is not saved in this folder.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "MinerAutotune",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($exe) {
            $exe = [string]$exe
            if ($exe -match "\S") {
                $exe = $exe.Trim()
                if (Test-Path -LiteralPath $exe) { return $exe }
            }
        }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) {
            return $cmd.Source
        }
    }
    throw "Python 3 was not found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}

$python = Find-Python
$config = Join-Path $ScriptDir "config.json"
$autotune = Join-Path $ScriptDir "autotune.py"
$logDir = Join-Path $ScriptDir "logs"

if (-not (Test-Path -LiteralPath $autotune)) { throw "Missing $autotune" }
if (-not (Test-Path -LiteralPath $config)) { throw "Missing $config" }

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

# Quote paths the way CreateProcess expects.
$arguments = "`"$autotune`" --config `"$config`""
$stdout = Join-Path $logDir "task-stdout.log"
$stderr = Join-Path $logDir "task-stderr.log"

Write-Host "Python : $python"
Write-Host "Script : $autotune"
Write-Host "Config : $config"
Write-Host "Task   : $TaskName"
Write-Host ""
Write-Host "This task starts at boot and runs whether or not you are logged in."
Write-Host "Enter the Windows password for $env:USERDOMAIN\$env:USERNAME"
Write-Host ""

$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Enter your Windows password so Miner Hearth can start at boot even if you are not logged in."
if (-not $cred) { throw "Cancelled — no credentials provided." }

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Hidden + Windows 10 compatibility (Win8 is the Win10-era setting).
$settings.Hidden = $true
$settings.Compatibility = "Win8"
$settings.Priority = 7
$settings.DisallowStartOnRemoteAppSession = $false

# Capture leftover console output if Python writes before the script logger starts.
try {
    $settings.ExecutionTimeLimit = [TimeSpan]::Zero
} catch { }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Replacing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Auto-tunes Bitaxe Gamma and NerdOCTAxe frequency from ASIC and VR temperatures. Dashboard: http://localhost:8080  Logs go in $logDir" `
    -User $cred.UserName `
    -Password $cred.GetNetworkCredential().Password `
    -RunLevel Limited `
    -Force | Out-Null

# schtasks cannot set stdout redirection on a direct python action easily;
# autotune.py always writes logs\autotune.log and logs\autotune.csv itself,
# using paths next to the script, so a missing console is fine.

Write-Host ""
Write-Host "Scheduled task '$TaskName' is installed."
Write-Host "  Trigger : At startup"
Write-Host "  Logon   : Run whether the user is logged on or not"
Write-Host "  Logs     : $logDir\autotune.log  and  $logDir\autotune.csv"
Write-Host "  Dashboard: http://localhost:8080   (open this in your web browser on this PC)"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start now :  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Status    :  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Remove    :  powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1"
Write-Host ""
Write-Host "After the task is running, open a web browser on this PC and go to:"
Write-Host "    http://localhost:8080"
Write-Host "You will see each miner and can start or stop autotune for each one."

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
