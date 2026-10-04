# PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
# Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md). Commercial use requires written permission.
<#
.SYNOPSIS
  Runs a PC TidyUp storage review and writes the HTML/Markdown report, history snapshot and cleanup script.

.EXAMPLE
  .\Run-TidyUp.ps1 -Open                         # scan the roots from tidyup.config.json, open the report
.EXAMPLE
  .\Run-TidyUp.ps1 -Root $env:USERPROFILE -NoDuplicates
.EXAMPLE
  .\Run-TidyUp.ps1 -App                          # start the live app: report + scan/clean/compress/archive in the browser
.NOTES
  Run from an elevated PowerShell to include system folders (C:\Windows\Temp, other profiles, ...).
#>
[CmdletBinding()]
param(
    [string[]]$Root,
    [string]$OutDir,
    [switch]$NoDuplicates,
    [switch]$Open,
    [switch]$Quiet,
    [switch]$App
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += , @($py.Source, '-3') }
    foreach ($name in 'python.exe', 'python3.exe') {
        Get-Command $name -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notmatch '\\WindowsApps\\' } |   # skip the Microsoft Store stub
            ForEach-Object { $candidates += , @($_.Source) }
    }
    Get-ChildItem "$env:ProgramFiles\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $candidates += , @($_.FullName) }
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        try {
            $v = & $exe @pre -c "import sys; print(sys.version_info >= (3, 9))" 2>$null
            if ($v -eq 'True') { return , $c }
        } catch { }
    }
    throw 'Python 3.9+ was not found. Install it (winget install Python.Python.3.12) and run again.'
}

$python = Find-Python
$exe = $python[0]
if ($App) {
    & $exe @(@($python | Select-Object -Skip 1) + @("$here\tidyup_app.py"))
    exit $LASTEXITCODE
}
$pyArgs = @($python | Select-Object -Skip 1) + @("$here\tidyup.py")
foreach ($r in $Root) { $pyArgs += @('--root', $r) }
if ($OutDir) { $pyArgs += @('--out', $OutDir) }
if ($NoDuplicates) { $pyArgs += '--no-duplicates' }
if ($Open) { $pyArgs += '--open' }
if ($Quiet) { $pyArgs += '--quiet' }

& $exe @pyArgs
exit $LASTEXITCODE
