# PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
# Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md). Commercial use requires written permission.
<#
.SYNOPSIS
  Installs, updates or removes PC TidyUp for the current user - no administrator rights needed.

.DESCRIPTION
  1. Checks for Python 3.9+ and offers to install Python 3.12 with winget if it is missing.
  2. Downloads PC TidyUp from GitHub (or uses the folder this script is in, if it contains PC TidyUp).
  3. Installs it to %LOCALAPPDATA%\Programs\PC-TidyUp, keeping your reports, rules and settings on updates.
  4. Unblocks the files and creates "PC TidyUp" shortcuts in the Start menu and on the desktop.
  5. Optionally schedules a weekly scan, then starts PC TidyUp.

  One-line install (PowerShell):
    irm https://raw.githubusercontent.com/MZade/productivity-tools/main/pc-tidy-up/install.ps1 | iex

  With options:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/MZade/productivity-tools/main/pc-tidy-up/install.ps1))) -Schedule -NoDesktopShortcut

  From a downloaded copy:
    powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Uninstall]

  PC TidyUp is used entirely at your own risk - see DISCLAIMER.md. The app asks you to accept it on first start.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$Branch = 'main',
    [switch]$NoStartMenuShortcut,
    [switch]$NoDesktopShortcut,
    [switch]$Schedule,
    [switch]$NoLaunch,
    [switch]$Online,          # download from GitHub even when run from a local copy
    [switch]$Uninstall,
    [switch]$KeepData,        # with -Uninstall: keep reports, your rules and settings
    [switch]$Yes              # answer "yes" to all questions (unattended)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'                # much faster downloads in Windows PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

if ($env:PCTIDYUP_NO_LAUNCH) { $NoLaunch = $true }       # for unattended setups / tests
if ($env:PCTIDYUP_YES) { $Yes = $true }
$Repo = 'MZade/productivity-tools'
$ToolFolder = 'pc-tidy-up'
$AppName = 'PC TidyUp'
$TaskName = 'PC TidyUp weekly storage review'
if (-not $InstallDir) { $InstallDir = if ($env:PCTIDYUP_INSTALL_DIR) { $env:PCTIDYUP_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\PC-TidyUp' } }
# shortcut locations (overridable for tests)
$StartMenuDir = if ($env:PCTIDYUP_SHORTCUT_ROOT) { Join-Path $env:PCTIDYUP_SHORTCUT_ROOT 'StartMenu' } else { Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs' }
$DesktopDir = if ($env:PCTIDYUP_SHORTCUT_ROOT) { Join-Path $env:PCTIDYUP_SHORTCUT_ROOT 'Desktop' } else { [Environment]::GetFolderPath('Desktop') }
$Preserve = @('reports', 'tidyup.rules.user.json', 'tidyup.config.json', 'tidyup.config.json.bak', 'tidyup.rules.user.json.bak')

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Step([string]$msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ask([string]$question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question [Y/n]"
    return ($a -eq '' -or $a -match '^(y|yes|j|ja)$')
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += , @($py.Source, '-3') }
    $store = @()
    foreach ($name in 'python.exe', 'python3.exe') {
        Get-Command $name -All -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.Source -match '\\WindowsApps\\') { $store += , @($_.Source) } else { $candidates += , @($_.Source) }
        }
    }
    Get-ChildItem "$env:ProgramFiles\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $candidates += , @($_.FullName) }
    $candidates += $store
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        try {
            $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2] if sys.version_info >= (3, 9) else 'old')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match '^\d+\.\d+$') { return @{ Exe = $exe; Pre = $pre; Version = $v } }
        } catch { }
    }
    return $null
}

function Update-PathFromRegistry {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Assert-SafeInstallDir([string]$dir) {
    $full = [IO.Path]::GetFullPath($dir).TrimEnd('\')
    $bad = @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:SystemRoot, $env:SystemDrive,
             (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') }
    if ($bad -contains $full -or $full.Length -le 3) { throw "Refusing to use '$full' as install folder - choose a dedicated folder, e.g. ...\PC-TidyUp" }
}

function Stop-RunningTidyUp([string]$dir) {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$dir*tidyup_app.py*" }
    if ($procs) {
        if (Ask "PC TidyUp is running from $dir. Stop it now?") {
            $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 800
        } else {
            Say 'Continuing - restart PC TidyUp afterwards to use the new files.' Yellow
        }
    }
}

function New-Shortcut([string]$path, [string]$target, [string]$workDir, [string]$icon) {
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $target
    $s.WorkingDirectory = $workDir
    $s.Description = 'PC TidyUp - find what is eating your disk and clean it up'
    if (Test-Path -LiteralPath $icon) { $s.IconLocation = "$icon,0" }
    $s.WindowStyle = 7                                   # start the console minimised
    $s.Save()
}

Write-Host ''
Write-Host '  PC TidyUp installer' -ForegroundColor White
Write-Host '  Free for personal use. Used entirely at your own risk - see DISCLAIMER.md (you accept it in the app).' -ForegroundColor DarkGray

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
    Assert-SafeInstallDir $InstallDir
    if (-not (Ask "Remove $AppName from $InstallDir$(if ($KeepData) { ' (keeping reports, rules and settings)' })?")) { Say 'Nothing changed.'; return }
    Stop-RunningTidyUp $InstallDir
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Say "Removed scheduled task '$TaskName'."
    }
    foreach ($lnk in (Join-Path $StartMenuDir "$AppName.lnk"), (Join-Path $DesktopDir "$AppName.lnk")) {
        if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Say "Removed shortcut $lnk" }
    }
    if (Test-Path -LiteralPath $InstallDir) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'tidyup.py'))) { throw "$InstallDir does not look like a PC TidyUp folder - not removed." }
        if ($KeepData) {
            Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object { $Preserve -notcontains $_.Name } | Remove-Item -Recurse -Force
            Say "Removed the program; kept your data in $InstallDir"
        } else {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force
            Say "Removed $InstallDir"
        }
    }
    Say "$AppName is uninstalled. Files you archived stay in your OneDrive 'TidyUp Archive' folder." Green
    return
}

# ------------------------------------------------------------------ 1. Python
Step 'Checking Python (3.9 or newer)'
$python = Find-Python
if (-not $python) {
    Say 'Python 3.9+ was not found.' Yellow
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask 'Install Python 3.12 for your user account with winget now?')) {
        winget install --exact --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        Update-PathFromRegistry
        $python = Find-Python
    }
    if (-not $python) {
        Say 'Please install Python 3.9 or newer (https://www.python.org/downloads/ - tick "Add python.exe to PATH"), then run this installer again.' Red
        return
    }
}
Say "Found Python $($python.Version): $($python.Exe)" Green

# ------------------------------------------------------------------ 2. Get the files
Assert-SafeInstallDir $InstallDir
$local = $PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tidyup_app.py')) -and -not $Online
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("pc-tidyup-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    if ($local) {
        Step "Using the PC TidyUp files next to this script ($PSScriptRoot)"
        $src = $PSScriptRoot
    } else {
        Step "Downloading PC TidyUp from github.com/$Repo ($Branch)"
        $zip = Join-Path $tmp 'pc-tidyup.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        # the archive contains <repo>-<branch>\<tool folder>\...
        $src = Get-ChildItem -LiteralPath $tmp -Directory | ForEach-Object { Join-Path $_.FullName $ToolFolder } |
            Where-Object { Test-Path (Join-Path $_ 'tidyup_app.py') } | Select-Object -First 1
        if (-not $src) { throw 'The download does not contain PC TidyUp - please try again later.' }
    }

    # -------------------------------------------------------------- 3. Install / update
    $update = Test-Path -LiteralPath (Join-Path $InstallDir 'tidyup.py')
    Step $(if ($update) { "Updating $InstallDir (your reports, rules and settings are kept)" } else { "Installing to $InstallDir" })
    if ($update) { Stop-RunningTidyUp $InstallDir }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    # never copy personal or working files from the source; the app creates your rules file from the example
    $skip = @('.git', 'reports', '__pycache__', 'tidyup.rules.user.json', 'tidyup.config.json.bak', 'tidyup.rules.user.json.bak') +
            $(if ($update) { $Preserve } else { @() })
    Get-ChildItem -LiteralPath $src -Force | Where-Object { $skip -notcontains $_.Name } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
    }
    if ($update -and (Test-Path -LiteralPath (Join-Path $InstallDir 'tidyup.config.json'))) {
        # keep the user's settings; ship the new defaults next to them for reference
        Copy-Item -LiteralPath (Join-Path $src 'tidyup.config.json') -Destination (Join-Path $InstallDir 'tidyup.config.default.json') -Force
    }
    Get-ChildItem -LiteralPath $InstallDir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    Say "Files in place ($((Get-ChildItem -LiteralPath $InstallDir -Recurse -File).Count) files)." Green

    # -------------------------------------------------------------- 4. Shortcuts
    $target = Join-Path $InstallDir 'PC-TidyUp.cmd'
    $icon = Join-Path $InstallDir 'assets\pc-tidyup.ico'
    if (-not $NoStartMenuShortcut) {
        $p = Join-Path $StartMenuDir "$AppName.lnk"; New-Shortcut $p $target $InstallDir $icon; Say "Start menu: $AppName" Green
    }
    if (-not $NoDesktopShortcut) {
        $p = Join-Path $DesktopDir "$AppName.lnk"; New-Shortcut $p $target $InstallDir $icon; Say "Desktop shortcut: $p" Green
    }

    # -------------------------------------------------------------- 5. Optional weekly scan
    if ($Schedule) {
        Step 'Scheduling a weekly scan (Mondays 12:30; it never deletes anything)'
        & (Join-Path $InstallDir 'Register-TidyUpTask.ps1')
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Say "$AppName is ready. Start it any time from the Start menu or the desktop shortcut." Green
Say "To update later, run this installer again. To remove it: install.ps1 -Uninstall (in $InstallDir)." DarkGray
if (-not $NoLaunch) {
    Step "Starting $AppName (your browser opens; keep the minimised console window open while you use it)"
    Start-Process -FilePath $target -WorkingDirectory $InstallDir -WindowStyle Minimized
}
