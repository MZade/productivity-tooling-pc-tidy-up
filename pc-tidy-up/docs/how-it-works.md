# PC TidyUp: how it works

## Components

| File | Role |
|---|---|
| `tidyup.py` | The scanner and report generator. Usable on its own (`python tidyup.py --help`). |
| `tidyup_app.py` | The local web app: serves the report, runs scans and actions, shows live progress, edits settings |
| `report_template.html` | The single-page report: works offline as a saved file, and as the live app |
| `tidyup.config.json` | Settings: roots, thresholds, priorities, archive, protection, app |
| `tidyup.rules.json` / `tidyup.rules.user.json` | Built-in rules / your rules |
| `PC-TidyUp.cmd`, `Run-TidyUp.ps1`, `Register-TidyUpTask.ps1` | Launchers and the weekly scheduled scan |

Everything uses the Python standard library. There are no packages to install and no network access.

## Scanning

- **Fast walk.** The drive is read with `os.scandir`, which uses the size and dates Windows already returns while listing a folder. Long paths are supported (`\\?\`). Symbolic links and junctions are never followed.
- **Rules first.** For every folder, folder rules are checked; matching folders become clean-up candidates with their total size and newest change. Files are matched against file rules; everything else counts towards categories, age, largest files, compression, archive and duplicate analysis.
- **"Last used"** is the later of the last-access and last-modified times when Windows tracks last access (the default on Windows 10/11); otherwise last modified.
- **OneDrive and SharePoint.** Online-only files are recognised from their file attributes. They're counted separately and **never opened**, so a scan never downloads anything. Synced files stored locally are offload candidates.
- **Duplicates.** Files of the same size are compared by content hash: full content up to 512 MB, sampled above that. Installed packages and app data are excluded. Reading a file for hashing restores its last-access time.
- **Priorities.** Every finding gets a priority from its recommendation and size (thresholds in Settings). Disk health compares free space with the configured limits.
- **History.** Each scan saves a small snapshot, so the next report can show what grew or shrank.

## The local app and its security model

`PC-TidyUp.cmd` starts `tidyup_app.py`, a small HTTP server built on Python's `http.server`, and opens the report in your browser.

- **Local only:** it listens on `127.0.0.1` and refuses requests whose `Host` header isn't the local address, which blocks DNS-rebinding tricks.
- **Per-session token:** every API call needs a random token that only the served page knows. Other websites can't call the app.
- **Allow-list:** actions work only on items in the current report, never on arbitrary paths. Your profile root, `C:\Windows`, Program Files, ProgramData and the OneDrive root are refused outright, and file actions are never applied inside system folders or application data.
- **Disclaimer first:** scans and actions are refused until the disclaimer has been accepted. Only the version and a timestamp are stored, in `reports\disclaimer-acceptance.json`.
- **Re-check before acting:** ages and conditions are evaluated again right before each delete.
- **Apps must be closed:** for browser, WebView, Electron and Visual Studio caches, the app checks whether the owning program is running (its process name matching a folder in the path) and skips the item if so.
- **One task at a time:** scans and actions run in the background; progress is polled and shown live; tasks can be cancelled between items.
- **Open in Explorer** only *shows* folders and files (`explorer /select`). It never launches a file.
- **Logging:** every action goes to `reports\actions.log`; archive moves go to `reports\archive-manifest.json`.
- **Idle shutdown:** the app stops when no page has been open for 20 minutes.

## Actions in detail

| Action | Implementation |
|---|---|
| Delete permanently | Deletes files one by one (links removed, never followed), or runs the rule's tool command (`npm cache clean --force`, …) |
| Move to Recycle Bin | Windows Shell `SHFileOperation` with undo, the same as Explorer. Refused when the bin is off; items larger than the bin are skipped. |
| Compress (ZIP) | Streams into a ZIP, verifies the CRC and total size, keeps the original's dates, then removes the original |
| Compress (NTFS) | `compact /c`; the saving is measured from the allocated size before and after |
| Archive | A same-drive rename into the archive folder; `attrib +U -P` makes it online-only; an optional junction, symlink or `.lnk` is left at the old location |
| Make online-only | `attrib +U -P` on synced files |
| Restore | Copies back from OneDrive (downloading if needed), verifies, removes the archived copy and the link |

## Files written

All under `reports\` by default, and all excluded from the repository by `.gitignore`:

| Path | Content |
|---|---|
| `TidyUp-Report-*.html`, `.md`, `-latest.*` | Reports (the last 10 kept) |
| `TidyUp-Data-latest.json`, `history\` | Report data and snapshots |
| `cleanup\TidyUp-Cleanup-*.ps1` | Dry-run-by-default cleanup scripts |
| `actions.log`, `action-state.json` | Action audit log and per-item results |
| `archive-manifest.json` | Archived items, used for Restore |

Reports contain file paths and your PC name, so treat them as personal data.
