# Changelog

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
