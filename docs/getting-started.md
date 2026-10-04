# PC TidyUp: getting started

A step-by-step guide for first-time users. It takes about 10 minutes, plus a few minutes for the first scan.

> ⚠️ PC TidyUp can delete, move and change files. You use it entirely at your own risk; read the [Disclaimer](../DISCLAIMER.md) and **make a backup** of important data first.

## Quick install (recommended)

Open **PowerShell** (Start menu → type *PowerShell*) and run:

```powershell
irm https://raw.githubusercontent.com/MZade/productivity-tooling-pc-tidy-up/main/install.ps1 | iex
```

The installer:

1. checks for Python 3.9+. If it's missing, it offers to install Python 3.12 with winget, for your user only.
2. downloads PC TidyUp and installs it to `%LOCALAPPDATA%\Programs\PC-TidyUp`. That's outside OneDrive, and no administrator rights are needed.
3. unblocks the files, so Windows doesn't warn about each one.
4. adds **PC TidyUp** to the **Start menu** and the **desktop**.
5. starts PC TidyUp. Continue with [4. First run](#4-first-run).

**Options:**

```powershell
$installer = [scriptblock]::Create((irm https://raw.githubusercontent.com/MZade/productivity-tooling-pc-tidy-up/main/install.ps1))
& $installer -Schedule              # also schedule a weekly scan (Mondays 12:30)
& $installer -NoDesktopShortcut     # Start menu entry only
& $installer -InstallDir D:\Tools\PC-TidyUp
& $installer -Uninstall             # remove program, shortcuts and scheduled task
& $installer -Uninstall -KeepData   # ... but keep reports, your rules and settings
```

**Update:** run the one-line install command again. Your reports, your rules and your settings are kept; the new default settings are saved next to yours as `tidyup.config.default.json`.

Prefer to read a script before you run it? Download [`install.ps1`](../install.ps1), inspect it, and run `powershell -ExecutionPolicy Bypass -File .\install.ps1`.

The rest of this guide describes the **manual** installation.

## 1. Install Python (once)

PC TidyUp needs **Python 3.9 or newer**. It uses nothing else; no packages to install.

Check whether you already have it. Open **PowerShell** (Start menu → type *PowerShell*) and run:

```powershell
py --version
```

If it shows `Python 3.9` or higher, go to step 2. Otherwise, install Python in **one** of these ways:

- **winget** (Windows 10/11): `winget install Python.Python.3.12`
- **Microsoft Store**: search for *Python 3.12* and click *Get*
- **python.org**: download the Windows installer from https://www.python.org/downloads/. In the first installer screen, tick **"Add python.exe to PATH"**.

Close and reopen PowerShell afterwards.

## 2. Download PC TidyUp

**Without git (easiest):**

1. Open https://github.com/MZade/productivity-tooling-pc-tidy-up
2. Click the green **Code** button → **Download ZIP**.
3. In Explorer, **right-click the ZIP → Properties → tick "Unblock" → OK**. This stops Windows from warning about every file inside.
4. Right-click the ZIP → **Extract All…** and pick a folder, for example `C:\Tools\PC-TidyUp`.

   A folder **outside** OneDrive is best, so your scan reports aren't synced to the cloud.

**With git:**

```powershell
git clone https://github.com/MZade/productivity-tooling-pc-tidy-up.git C:\Tools\PC-TidyUp
```

## 3. Start it

Double-click **`PC-TidyUp.cmd`** in the folder. Explorer may show it as *PC-TidyUp* with the type *Windows Command Script*.

- If Windows shows **"Windows protected your PC"** (SmartScreen), click **More info → Run anyway**. Only do this for a copy you downloaded from the repository above.
- A console window opens. **Keep it open** while you use PC TidyUp.
- Your browser opens the page at `http://127.0.0.1:8765`.

## 4. First run

1. **Read and accept the disclaimer.** Until you do, the page is read-only.
2. Click **Run the first scan**. Scanning a full drive takes a few minutes; you can watch the progress.
3. When it's done, the **Overview** shows your disk's health and the **priority guide**:
   - **P1 Do now**: big and low risk. Caches and temp files that apps rebuild by themselves.
   - **P2 Quick check**: probably obsolete. Glance at the list, then act.
   - **P3 Decide**: only you can judge (backups, models, installer caches).
4. **Close your browser windows, Teams and code editors** you aren't using, so their caches aren't locked. Caches of running apps are skipped automatically.
5. In the **P1** lane, click **Clean up all …**, check the list, choose **Move to Recycle Bin** (can be undone) or **Delete permanently** (frees the space right away), and confirm.
6. Click **Run new scan** to see the new numbers.

Tip: to include system folders such as `C:\Windows\Temp`, click **Restart as admin** in the page header.

## 5. Optional

- **Weekly report:** run `.\Register-TidyUpTask.ps1` in PowerShell, in the PC TidyUp folder. It scans every Monday at 12:30 and never deletes anything.
- **Your own rules and thresholds:** use the **⚙ Settings** tab. See [Rules](rules.md).
- **Everything else:** see the [User guide](user-guide.md).

## Updating

If you used the **quick install**, just run the install command again. For a manual installation:

1. Click **Stop PC TidyUp** on the page.
2. Download the new version (step 2) and extract it **over** your existing folder, or run `git pull` if you used git.

Your **reports** (`reports\`) and **your own rules** (`tidyup.rules.user.json`) aren't part of the download, so they're kept.

If you changed **Settings**, `tidyup.config.json` is replaced by the new default. Copy it somewhere safe before updating and put it back afterwards. The previous version is also kept as `tidyup.config.json.bak` whenever you save settings.

## Uninstalling

If you used the **quick install**, run `install.ps1 -Uninstall` from the install folder (`%LOCALAPPDATA%\Programs\PC-TidyUp`), or use the `-Uninstall` option shown above. It removes the program, the shortcuts and the scheduled task. For a manual installation:

1. Click **Stop PC TidyUp**.
2. If you scheduled the weekly scan, run `.\Register-TidyUpTask.ps1 -Unregister`.
3. Delete the PC TidyUp folder.

Files you **archived** to OneDrive stay in `OneDrive\TidyUp Archive`. Restore them first in the **Archive** tab if you want them back in their original places.
