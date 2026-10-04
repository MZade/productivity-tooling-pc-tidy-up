# Changelog

## 1.1.0 (2026-10)

- One-command installer `install.ps1` (`irm … | iex`): checks for or installs Python, installs per user without admin, unblocks the files, adds Start menu and desktop shortcuts with the PC TidyUp icon, optional weekly scan; updates keep reports, rules and settings; `-Uninstall`
- New app icon (`assets/`)

## 1.0.1 (2026-10)

- Fix: runs on Python 3.9-3.11 as documented (junction detection used a Python 3.12-only function); tested on 3.9, 3.12 and 3.14
- Fix: Python installed from the Microsoft Store is now found by the launcher
- Docs: step-by-step [getting started](docs/getting-started.md) and [troubleshooting & FAQ](docs/troubleshooting.md)

## 1.0.0 (2026-10)

First public release.

- Drive scan with 59 built-in rules for caches, temp, logs, dumps, installers, build output, AI models and Windows-managed space
- Priority guide (P1 Do now / P2 Quick check / P3 Decide) and disk health verdict
- Local web app with live progress, plus actions: delete (Recycle Bin or permanent), compress (ZIP or NTFS), archive to OneDrive with optional link, make online-only, restore
- Settings tab: configuration, rule editor, built-in rule overrides, "Test a path"
- "Open in File Explorer" for every path
- Duplicates, largest files, folder tree, compression and archive candidates, "Since last run" trends
- Headless scans, weekly scheduled task, dry-run-by-default PowerShell cleanup script
- `--check-rules` and `--explain` command-line helpers
- Disclaimer and limitation of liability (`DISCLAIMER.md`), accepted once in the app before the first scan or action
- "Low risk" (not "safe") labelling for rebuildable caches; caches of running apps (browsers, VS Code, Visual Studio, Teams, …) are skipped
