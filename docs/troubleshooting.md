# PC TidyUp: troubleshooting and FAQ

## Starting

**"Python 3.9+ was not found"**
Install Python (see [Getting started](getting-started.md#1-install-python-once)), then close and reopen the console and start `PC-TidyUp.cmd` again.

**Windows says "Windows protected your PC"**
That's SmartScreen, which flags any script downloaded from the internet. Click **More info → Run anyway**, but only for a copy from the official repository. To avoid it, unblock the ZIP *before* extracting it (right-click → Properties → *Unblock*).

**Nothing happens when I double-click `PC-TidyUp.cmd`**
Open PowerShell in the PC TidyUp folder and run `.\Run-TidyUp.ps1 -App`; the error message is shown there.

**The page says "PC TidyUp app not running"**
The console window was closed, or the app stopped after 20 minutes without an open page. Start `PC-TidyUp.cmd` again.

**Port 8765 is already in use**
PC TidyUp picks a free port by itself; use the address it prints in the console. You can set a fixed port in **Settings → App**.

**Can I open the saved report without the app?**
Yes, `reports\TidyUp-Report-latest.html` opens in any browser, but it's **read-only**. Scans and actions need the app (`PC-TidyUp.cmd`).

## Scanning

**Some folders couldn't be read**
Those are system folders and other users' profiles. Click **Restart as admin** and run a new scan to include them.

**My OneDrive files aren't counted**
Online-only OneDrive and SharePoint files use no local space, so they're counted separately and never opened; nothing gets downloaded. The **OneDrive** tab lists synced files that *do* take local space.

**A file I just downloaded is listed as "recently used"**
Antivirus software often reads new files, which refreshes their last-access time. That's expected.

**The scan is slow**
A full drive with millions of files takes a few minutes. You can turn off **Look for duplicate files** in Settings → Scan, or scan a single folder (**Run new scan** → change the folders).

## Cleaning up

**"skipped – chrome.exe is running" (or Code.exe, msedge.exe, …)**
On purpose: caches are never deleted from under a running app. Close the app completely, including from the system tray, and try again.

**"access denied – in use, or needs administrator"**
The file is locked by a running program, or it's in a system folder. Close the program, or use **Restart as admin**.

**I deleted items but free space didn't change**
- If you chose **Move to Recycle Bin**, the space only comes back once you empty the Recycle Bin.
- The numbers on the page update after **Run new scan**.

**Why is the Recycle Bin option refused?**
The Recycle Bin is switched off on that drive, so "recycling" would delete permanently. PC TidyUp refuses rather than surprise you. Turn it on in the Recycle Bin's properties, or choose *Delete permanently*.

**Is "Low risk" safe?**
It means caches and temp files that apps rebuild by themselves. That's low risk, but **not zero risk**: such items are still deleted for real. Keep backups, and prefer *Move to Recycle Bin* when in doubt.

**I deleted something by mistake**
If you used *Move to Recycle Bin*, restore it from the Recycle Bin. Permanent deletions can't be undone by PC TidyUp; use your backup.

## Archive and OneDrive

**An item can't be archived**
PC TidyUp refuses:
- application data and system folders
- items already in OneDrive (use *Make online-only* instead)
- folders with more than 20,000 files (compress them first)
- names OneDrive doesn't accept
- paths over 400 characters

The reason is shown next to the item.

**Archived files still use local space**
OneDrive first uploads them, then frees the space. The **Archive** tab shows *uploading / pending* until that's done. Make sure OneDrive is running and signed in.

**A shortcut (`.lnk`) was left instead of a link**
Symbolic links to files need Windows *Developer Mode* (Settings → System → For developers) or administrator rights. Without that, PC TidyUp leaves a shortcut. Folders always get a working link (a junction).

**How do I get an archived item back?**
Use **Archive** tab → *Archived items* → **Restore**. It downloads the item from OneDrive back to its original place.

## Settings and rules

**I broke something in the settings**
The previous version is kept as `tidyup.config.json.bak`; copy it back over `tidyup.config.json`. Or download a fresh `tidyup.config.json` from the repository.

**My rule doesn't match**
Use **Settings → Test a path**, or run `python tidyup.py --explain "C:\the\path"` and `python tidyup.py --check-rules`. See [Rules](rules.md).

## Still stuck?

Open an issue in the repository with the error text. Leave out file paths or names you don't want to share, and remember that reports contain your PC name.
