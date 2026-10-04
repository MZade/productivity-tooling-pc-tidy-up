@echo off
rem PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
rem Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md). Commercial use requires written permission.
rem Starts the PC TidyUp app and opens the report in your browser: run scans, clean up,
rem compress and archive to OneDrive from the page. Keep this window open while you use it.
rem Right-click > "Run as administrator" to include system folders (or use "Restart as admin" on the page).
title PC TidyUp
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-TidyUp.ps1" -App
