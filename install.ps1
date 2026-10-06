<#
ServerDeck installer. Run once from an elevated PowerShell in the ServerDeck folder:

    powershell -ExecutionPolicy Bypass -File .\install.ps1

  1. finds Python 3.11 or newer (offers to install it with winget)
  2. creates config.json if there is none (no servers yet - add them in the web page)
  3. registers the "ServerDeck" scheduled task: elevated, starts at boot without anyone
     logging on, restarts itself if it stops (5-minute watchdog) - and starts it
  4. puts a "ServerDeck" shortcut to http://127.0.0.1:8787 on your desktop

Remove again:  .\install.ps1 -Uninstall   (task + firewall rules; your servers and files stay)
#>
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an elevated PowerShell (right-click PowerShell > Run as administrator).' }

if ($Uninstall) {
    Stop-ScheduledTask -TaskName 'ServerDeck' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'ServerDeck' -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName 'ServerDeck - *' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    'ServerDeck task and firewall rules removed. Game servers, saves, backups and config.json are untouched.'
    return
}

Write-Host '== Python'
function Test-Python([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c 'import sys; print(sys.executable if sys.version_info >= (3, 11) else "")' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path ($out | Select-Object -Last 1).Trim())) { return ($out | Select-Object -Last 1).Trim() }
    } catch { }
    return $null
}
function Get-Python {
    $p = Test-Python 'py' @('-3')
    if (-not $p) { $p = Test-Python 'python' @() }
    return $p
}
$python = Get-Python
if (-not $python) {
    $answer = Read-Host 'Python 3.11+ was not found. Install Python 3.12 with winget now? (y/n)'
    if ($answer -notmatch '^y') { throw 'Install Python 3.11 or newer from https://www.python.org/downloads/ and run this again.' }
    winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $python = Get-Python
    if (-not $python) { throw 'Python still not found - open a new PowerShell window and run this again.' }
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
"  $python"

Write-Host '== config.json'
$config = Join-Path $here 'config.json'
if (Test-Path $config) { '  kept your existing config.json' }
else { Copy-Item (Join-Path $here 'config.example.json') $config; '  created from config.example.json (no servers yet)' }

Write-Host '== Scheduled task "ServerDeck"'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$(Join-Path $here 'serverdeck.py')`"" -WorkingDirectory $here
$atBoot = New-ScheduledTaskTrigger -AtStartup
$atBoot.Delay = 'PT30S'
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
# S4U: runs whether or not you are logged on, without storing your password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
# Priority 4 = normal. Task Scheduler's default (7) runs tasks with lower CPU, disk and memory
# priority, and the game servers ServerDeck starts would inherit it.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -Priority 4
Register-ScheduledTask -TaskName 'ServerDeck' -Description "Game server manager ($here). UI: http://127.0.0.1:8787" `
    -Action $action -Trigger @($atBoot, $watchdog) -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'ServerDeck'
"  registered and started"

Write-Host '== Desktop shortcut'
$desktop = [Environment]::GetFolderPath('Desktop')
Set-Content -Path (Join-Path $desktop 'ServerDeck.url') -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8787/" -Encoding ASCII
"  ServerDeck.url"

Start-Sleep 5
Start-Process 'http://127.0.0.1:8787/'
"`nDone. Open http://127.0.0.1:8787 and press 'Servers' to add your first server."
