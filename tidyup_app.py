#!/usr/bin/env python3
# PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
# Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md).
# Free for personal / noncommercial use; any commercial use requires the author's written permission.
"""
PC TidyUp app - the PC TidyUp report as a local web app, with actions.

    python tidyup_app.py            # or double-click PC-TidyUp.cmd

Serves the latest report on http://127.0.0.1:<port>/ and lets you, from that page:
  * run a new scan with live progress,
  * delete clean-up candidates (re-checking their age first),
  * compress files/folders (ZIP + verify, or transparent NTFS compression),
  * archive files/folders to one common OneDrive folder, optionally leaving a link
    (junction for folders, symbolic link or shortcut for files), and make them online-only,
  * make stale OneDrive files online-only, and restore archived items.

Safety: binds to 127.0.0.1 only, checks the Host header and a per-run token, and only
acts on items that are part of the current report. Every action is logged to
reports/actions.log; archive moves are recorded in reports/archive-manifest.json.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import fnmatch
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tidyup as T

HERE = T.HERE
CONFIG = HERE / "tidyup.config.json"
RULES = HERE / "tidyup.rules.json"
NO_WINDOW = 0x08000000
DISCLAIMER_VERSION = "2026-10"     # bump when DISCLAIMER.md changes -> users accept again

# Never touched by file/folder actions, regardless of the (editable) 'protected' setting.
HARD_PROTECTED = ["{windir}/**", "{programfiles}/**", "{programfilesx86}/**", "{programdata}/**",
                  "{systemdrive}/system volume information/**", "{systemdrive}/recovery/**", "**/.git/**"]
INVALID_ONEDRIVE_NAMES ={".lock", "con", "prn", "aux", "nul", "desktop.ini", ".ds_store"} | \
    {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}


class Cancelled(Exception):
    pass


class ActionError(Exception):
    """Expected refusal/failure with a message for the user."""


# --------------------------------------------------------------------------- small helpers
def lp(p: str) -> str:
    return T.longpath(p)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, **kw)


def under(path: str, root: str) -> bool:
    if not root:
        return False
    a, b = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(root))
    return a == b or a.startswith(b.rstrip("\\") + "\\")


def is_link(path: str) -> bool:
    try:
        return os.path.islink(lp(path)) or os.path.isjunction(lp(path))
    except OSError:
        return False


def file_attrs(path: str) -> int:
    try:
        return os.stat(lp(path), follow_symlinks=False).st_file_attributes
    except OSError:
        return 0


def walk_files(path: str, job=None):
    """Yields (full_path, stat) for every file below path, never following links."""
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(lp(d)) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if job is not None and job.cancel.is_set():
                raise Cancelled()
            full = T.join(d, e.name)
            try:
                if e.is_symlink() or e.is_junction():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(full)
                else:
                    yield full, e.stat(follow_symlinks=False)
            except OSError:
                continue


def tree_info(path: str, job=None, limit: int | None = None):
    """(bytes, files, newest_mtime) of a file or folder; stops counting after `limit` files."""
    st = os.stat(lp(path), follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1, st.st_mtime
    size = n = 0
    newest = st.st_mtime
    for _, fst in walk_files(path, job):
        size += fst.st_size
        n += 1
        newest = max(newest, fst.st_mtime)
        if limit and n > limit:
            break
    return size, n, newest


def local_size(st) -> int:
    """Bytes a file really occupies locally (online-only cloud files: 0)."""
    return 0 if getattr(st, "st_file_attributes", 0) & T.CLOUD_ONLY else st.st_size


def remove_file(path: str) -> None:
    try:
        os.remove(lp(path))
    except PermissionError:
        os.chmod(lp(path), stat.S_IWRITE)
        os.remove(lp(path))


def remove_link(path: str) -> None:
    if os.path.isjunction(lp(path)) or os.path.isdir(lp(path)):
        os.rmdir(lp(path))       # removes a junction / directory symlink, never its target
    else:
        os.unlink(lp(path))


def delete_tree(path: str, job, keep_root: bool = False) -> tuple[int, int]:
    """Deletes a file or folder (links are removed, never followed). Returns (bytes freed, failures)."""
    freed = failed = 0
    if is_link(path):
        remove_link(path)
        return 0, 0
    st = os.stat(lp(path), follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        remove_file(path)
        job.bytes_done += st.st_size
        return local_size(st), 0

    def rec(d: str):
        nonlocal freed, failed
        try:
            with os.scandir(lp(d)) as it:
                entries = list(it)
        except OSError:
            failed += 1
            return
        for e in entries:
            if job.cancel.is_set():
                raise Cancelled()
            full = T.join(d, e.name)
            try:
                if e.is_symlink() or e.is_junction():
                    remove_link(full)
                elif e.is_dir(follow_symlinks=False):
                    rec(full)
                    os.rmdir(lp(full))
                else:
                    fst = e.stat(follow_symlinks=False)
                    remove_file(full)
                    freed += local_size(fst)
                    job.bytes_done += fst.st_size
            except OSError:
                failed += 1

    rec(path)
    if not keep_root:
        try:
            os.rmdir(lp(path))
        except OSError:
            failed += 1
    return freed, failed


def alloc_size(path: str) -> int:
    k = ctypes.windll.kernel32
    k.GetCompressedFileSizeW.restype = ctypes.c_ulong
    high = ctypes.c_ulong(0)
    low = k.GetCompressedFileSizeW(ctypes.c_wchar_p(lp(path)), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.GetLastError():
        return os.path.getsize(lp(path))
    return (high.value << 32) + low


def unique_path(p: str) -> str:
    if not os.path.lexists(lp(p)):
        return p
    root, ext = os.path.splitext(p)
    i = 2
    while os.path.lexists(lp(f"{root} ({i}){ext}")):
        i += 1
    return f"{root} ({i}){ext}"


def make_shortcut(link: str, target: str) -> None:
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TU_LINK);"
          "$s.TargetPath=$env:TU_TARGET;$s.Description='Archived to OneDrive by PC TidyUp';$s.Save()")
    env = dict(os.environ, TU_LINK=link, TU_TARGET=target)
    r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, timeout=60)
    if r.returncode != 0 or not os.path.exists(link):
        raise ActionError("could not create shortcut: " + (r.stderr.strip() or "unknown error"))


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_info(path: str) -> dict:
    """Is the Recycle Bin on for this drive, how big may it get, how full is it."""
    import winreg
    drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    total = shutil.disk_usage(drive).total
    info = dict(drive=drive, enabled=True, max_bytes=int(total * 0.05), used_bytes=0, items=0, estimated=True)
    buf = ctypes.create_unicode_buffer(64)
    if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, buf, 64):
        guid = buf.value.rstrip("\\").rsplit("\\", 1)[-1].replace("Volume", "")   # {xxxxxxxx-...}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}") as k:
                try:
                    info["max_bytes"] = int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
                    info["estimated"] = False
                except OSError:
                    pass
                try:
                    info["enabled"] = int(winreg.QueryValueEx(k, "NukeOnDelete")[0]) == 0
                except OSError:
                    pass
        except OSError:
            pass
    q = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    if ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(q)) == 0:
        info["used_bytes"], info["items"] = q.i64Size, q.i64NumItems
    return info


def recycle(paths: list[str]) -> bool:
    """Moves paths to the Recycle Bin (Explorer semantics). If Windows cannot recycle something it asks
    before deleting it permanently (FOF_WANTNUKEWARNING) instead of doing so silently."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
    joined = "\0".join(paths) + "\0\0"
    arr = (ctypes.c_wchar * len(joined))(*joined)
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(arr, ctypes.c_void_p), None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING, 0, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def symlink_supported() -> bool:
    d = os.path.join(os.environ.get("TEMP", str(HERE)), f"tidyup-symlink-test-{os.getpid()}")
    try:
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, "t.txt")
        open(target, "w").close()
        os.symlink(target, os.path.join(d, "l.txt"))
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, app, kind: str, title: str, op: str | None = None):
        self.app = app
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.title, self.op = kind, title, op
        self.state = "running"
        self.started, self.ended = time.time(), None
        self.items_total = self.items_done = 0
        self.bytes_total = self.bytes_done = 0
        self.freed = 0
        self.recycled = 0
        self.current = ""
        self.log: list[list] = []
        self.results: dict[str, dict] = {}
        self.progress: dict = {}
        self.cancel = threading.Event()
        self.error = None

    def say(self, msg: str, level: str = "info"):
        self.log.append([time.strftime("%H:%M:%S"), level, msg])
        self.app.audit(f"[{self.kind}:{self.op or ''}] {level.upper()} {msg}")

    def to_json(self, log_from: int = 0) -> dict:
        return dict(id=self.id, kind=self.kind, op=self.op, title=self.title, state=self.state,
                    started=self.started, ended=self.ended, elapsed=round((self.ended or time.time()) - self.started),
                    items_total=self.items_total, items_done=self.items_done, bytes_total=self.bytes_total,
                    bytes_done=self.bytes_done, freed=self.freed, recycled=self.recycled, current=self.current, progress=self.progress,
                    error=self.error, log=self.log[log_from:], log_next=len(self.log), results=self.results)


# --------------------------------------------------------------------------- the app
class App:
    def __init__(self, port: int):
        self.cfg = T.load_json(CONFIG)
        self.out = T.report_dir(self.cfg)
        self.out.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.last_ping = time.time()
        self.elevated = T.is_admin()
        self.symlink_ok = symlink_supported()
        self.state_file = self.out / "action-state.json"
        self.manifest_file = self.out / "archive-manifest.json"
        self.audit_file = self.out / "actions.log"
        self.disclaimer_file = self.out / "disclaimer-acceptance.json"
        self.apply_config()
        self.data = None
        self.load_data()

    def disclaimer_accepted(self) -> bool:
        return self._read_json(self.disclaimer_file, {}).get("version") == DISCLAIMER_VERSION

    def accept_disclaimer(self):
        self._write_json(self.disclaimer_file, {"version": DISCLAIMER_VERSION,
                                                "accepted_at": dt.datetime.now().isoformat(timespec="seconds")})
        self.audit(f"disclaimer version {DISCLAIMER_VERSION} accepted")

    def check_app_closed(self, cand: dict):
        """Never delete an app's cache from under the running app."""
        if cand.get("check_running"):
            exe = running_app_for(cand["path"])
            if exe:
                raise ActionError(f"skipped - {exe} is running; close it completely and retry")

    def require_disclaimer(self):
        if not self.disclaimer_accepted():
            raise ActionError("please read and accept the disclaimer first - PC TidyUp is used entirely at your own risk")

    def apply_config(self):
        """(Re)derive everything that depends on tidyup.config.json - called at start and after Settings are saved."""
        self.archive_cfg = self.cfg.get("archive") or {}
        self.archive_root = T.archive_root(self.cfg)
        self.onedrive = T.onedrive_root()
        variables = T.build_vars()
        # HARD_PROTECTED always applies, whatever the editable 'protected' list says
        pats = HARD_PROTECTED + self.cfg.get("protected", []) + self.cfg.get("advice_exclude", [])
        self.no_touch = [T.glob_re(T.expand(p, variables)) for p in pats]
        self.forbidden = {os.path.normcase(os.path.normpath(p)) for p in [
            os.environ.get("SystemDrive", "C:") + "\\", os.environ.get("USERPROFILE", ""), os.environ.get("SystemRoot", ""),
            os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramData", ""),
            os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""), self.onedrive, self.archive_root,
            os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users")] + [
            os.path.join(os.environ.get("USERPROFILE", ""), n) for n in
            ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "source", "OneDrive")] if p}

    # ---- persistence
    def audit(self, line: str):
        try:
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        except OSError:
            pass

    def _read_json(self, path: Path, default):
        try:
            return T.load_json(path)
        except (OSError, ValueError):
            return default

    def _write_json(self, path: Path, obj):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)

    def action_state(self) -> dict:
        if not self.data:
            return {}
        return self._read_json(self.state_file, {}).get(self.data["meta"].get("stamp", ""), {})

    def save_result(self, key: str, res: dict):
        if not self.data:
            return
        with self.lock:
            st = self._read_json(self.state_file, {})
            stamp = self.data["meta"].get("stamp", "")
            st = {stamp: st.get(stamp, {})}             # only the current report's state is kept
            st[stamp][key] = res
            self._write_json(self.state_file, st)

    def manifest(self) -> list:
        return self._read_json(self.manifest_file, [])

    def save_manifest(self, entries: list):
        with self.lock:
            self._write_json(self.manifest_file, entries)

    # ---- report data + allow-list of actionable items
    def load_data(self):
        p = self.out / "TidyUp-Data-latest.json"
        self.data = self._read_json(p, None) if p.exists() else None
        self.cands, self.files, self.dirs, self.dup_groups = {}, {}, {}, []
        if not self.data:
            return
        for c in self.data["candidates"]:
            self.cands[f"c:{c['id']}"] = c

        def add_file(path, size, src):
            e = self.files.setdefault(path.lower(), dict(path=path, bytes=size, lists=set()))
            e["lists"].add(src)

        for src in ("compress", "archive", "offload"):
            for i in self.data[src]["items"]:
                add_file(i["path"], i["bytes"], src)
        for i in self.data["large"]:
            add_file(i["path"], i["bytes"], "large")
        for g in self.data["duplicates"].get("groups", []):
            self.dup_groups.append([f.lower() for f in g["files"]])
            for f in g["files"]:
                add_file(f, g["size"], "dup")
        for n in self.data["tree"]:
            self.dirs[n[3].lower()] = dict(path=n[3], bytes=n[4], files=n[5])

    def app_info(self) -> dict:
        e = os.environ
        under_ = [e.get(k, "") for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                                          "LOCALAPPDATA", "APPDATA")]
        blocked = dict(exact=sorted(self.forbidden), under=[os.path.normcase(p) for p in under_ if p])
        return dict(token=self.token, version=T.VERSION, elevated=self.elevated, symlink_ok=self.symlink_ok,
                    disclaimer_accepted=self.disclaimer_accepted(), disclaimer_version=DISCLAIMER_VERSION,
                    blocked=blocked,
                    archive_root=self.archive_root, onedrive=self.onedrive, state=self.action_state(),
                    archive_defaults=dict(make_online_only=self.archive_cfg.get("make_online_only", True),
                                          leave_link=self.archive_cfg.get("leave_link", False)),
                    job=self.job.to_json() if self.job else None)

    # ---- guards
    def guard(self, path: str, allow_protected: bool = False):
        n = os.path.normcase(os.path.normpath(path))
        if not os.path.isabs(path) or n in self.forbidden or len(n) <= 3:
            raise ActionError("refused: this location is too important to touch")
        if not allow_protected:
            np_ = T.norm(path)
            if any(r.match(np_ + "/") or r.match(np_) for r in self.no_touch):
                raise ActionError("refused: system or application data (moving/changing it would break an app)")
            if file_attrs(path) & T.A_SYSTEM:
                raise ActionError("refused: Windows system file")

    def resolve(self, op: str, keys: list[str]) -> list[dict]:
        items, errors = [], []
        for key in keys:
            kind, _, ref = key.partition(":")
            if kind == "c" and key in self.cands:
                c = self.cands[key]
                items.append(dict(key=key, kind="cand", path=c["path"], bytes=c["bytes"], cand=c, label=c["label"]))
            elif kind == "f" and ref.lower() in self.files:
                f = self.files[ref.lower()]
                items.append(dict(key=key, kind="file", path=f["path"], bytes=f["bytes"], lists=f["lists"]))
            elif kind == "d" and ref.lower() in self.dirs:
                d = self.dirs[ref.lower()]
                items.append(dict(key=key, kind="dir", path=d["path"], bytes=d["bytes"]))
            elif kind == "m":
                e = next((e for e in self.manifest() if e["id"] == ref), None)
                if e:
                    items.append(dict(key=key, kind="manifest", path=e["original"], bytes=e.get("bytes", 0), entry=e))
                else:
                    errors.append(key)
            else:
                errors.append(key)
        if errors:
            raise ActionError(f"{len(errors)} item(s) are not part of the current report - run a new scan first")
        return items

    # ---- jobs
    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start_scan(self, body: dict) -> Job:
        self.require_disclaimer()
        roots = body.get("roots") or None
        if roots:
            if not isinstance(roots, list) or not all(isinstance(r, str) and os.path.isdir(r) for r in roots):
                raise ActionError("every root must be an existing folder or drive")
        no_dups = bool(body.get("no_duplicates"))
        scan_roots = roots or self.cfg.get("roots") or ["C:\\"]
        job = Job(self, "scan", "Scanning " + ", ".join(scan_roots))
        expected = 0
        for r in scan_roots:
            if len(os.path.abspath(r)) <= 3:      # a whole drive: disk "used" is a good estimate of the work
                try:
                    expected += shutil.disk_usage(r).used
                except OSError:
                    pass
        job.progress = {"phase": "scan", "expected": expected or None}

        def work():
            try:
                job.say(f"Scan started: {', '.join(scan_roots)}" + (" (no duplicate check)" if no_dups else ""))
                r = T.run_scan(CONFIG, RULES, roots, None, no_dups, True, progress=job.progress, cancel=job.cancel)
                self.load_data()
                job.say(f"Scan finished: {r['stats']['files']:,} files, {T.human(r['stats']['bytes'])} in "
                        f"{r['duration']:.0f} s. Report: {r['html']}")
                job.state = "done"
            except T.ScanCancelled:
                job.say("Scan cancelled", "warn")
                job.state = "cancelled"
            except Exception as e:  # noqa: BLE001 - surface anything to the page
                job.error = str(e)
                job.say(f"Scan failed: {e}", "error")
                self.audit(traceback.format_exc())
                job.state = "failed"
            finally:
                job.ended = time.time()

        self.job = job
        threading.Thread(target=work, daemon=True).start()
        return job

    def start_action(self, body: dict) -> Job:
        self.require_disclaimer()
        op = body.get("op")
        handlers = {"delete": self.do_delete, "compress": self.do_compress, "archive": self.do_archive,
                    "offload": self.do_offload, "restore": self.do_restore}
        if op not in handlers:
            raise ActionError("unknown action")
        keys = body.get("keys") or []
        if not isinstance(keys, list) or not keys or len(keys) > 5000:
            raise ActionError("select between 1 and 5000 items")
        opts = body.get("options") or {}
        items = self.resolve(op, [str(k) for k in keys])
        if op == "delete":
            chosen = {i["path"].lower() for i in items if i["kind"] == "file"}
            for g in self.dup_groups:
                if all(f in chosen for f in g):
                    raise ActionError("all copies of a duplicate group are selected - keep at least one")
        names = {"delete": "Deleting", "compress": "Compressing", "archive": "Archiving to OneDrive",
                 "offload": "Making online-only", "restore": "Restoring"}
        job = Job(self, "action", f"{names[op]} {len(items)} item(s)", op)
        job.items_total = len(items)
        job.bytes_total = sum(i["bytes"] or 0 for i in items)
        handler = handlers[op]

        def work():
            job.say(f"{names[op]} {len(items)} item(s), {T.human(job.bytes_total)}")
            try:
                if op == "archive":
                    self.prepare_archive_root(job)
                for it in items:
                    if job.cancel.is_set():
                        raise Cancelled()
                    job.current = it["path"]
                    t0 = time.time()
                    try:
                        res = handler(it, opts, job)
                        res.setdefault("ok", True)
                    except Cancelled:
                        raise
                    except ActionError as e:
                        res = dict(ok=False, msg=str(e))
                    except PermissionError as e:
                        res = dict(ok=False, msg=("access denied - in use, or needs administrator"
                                                  + ("" if self.elevated else " (restart PC TidyUp as administrator)")
                                                  + f": {e.filename or ''}"))
                    except OSError as e:
                        res = dict(ok=False, msg=f"{e.strerror or e}: {e.filename or ''}")
                    res.update(op=op, ts=time.strftime("%Y-%m-%d %H:%M"))
                    job.freed += res.get("freed", 0) or 0
                    job.recycled += res.get("recycled", 0) or 0
                    job.results[it["key"]] = res
                    self.save_result(it["key"], res)
                    job.items_done += 1
                    level = "info" if res["ok"] else "error"
                    job.say(f"{'OK  ' if res['ok'] else 'FAIL'} {it['path']}"
                            + (f" - freed {T.human(res['freed'])}" if res.get("freed") else "")
                            + (f" - {res['msg']}" if res.get("msg") else "")
                            + (f" ({time.time() - t0:.0f}s)" if time.time() - t0 > 2 else ""), level)
                ok = sum(1 for r in job.results.values() if r["ok"])
                job.say(f"Done: {ok}/{len(items)} succeeded, {T.human(job.freed)} freed"
                        + (f", {T.human(job.recycled)} moved to the Recycle Bin (empty it to get that space back)"
                           if job.recycled else ""))
                job.state = "done"
            except Cancelled:
                job.say("Cancelled - remaining items were not touched", "warn")
                job.state = "cancelled"
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                job.say(f"Stopped: {e}", "error")
                self.audit(traceback.format_exc())
                job.state = "failed"
            finally:
                job.current = ""
                job.ended = time.time()

        self.job = job
        threading.Thread(target=work, daemon=True).start()
        return job

    # ------------------------------------------------------------------ delete
    def recycle_targets(self, it: dict, job: Job) -> list[str]:
        """The concrete paths a delete would remove, after the same guards and age re-checks."""
        if it["kind"] == "file":
            self.guard(it["path"])
            return [it["path"]] if os.path.lexists(lp(it["path"])) else []
        c = it["cand"]
        if c["action"] == "info":
            raise ActionError("managed by Windows/an app - use: " + c["how"])
        self.check_app_closed(c)
        if c["rule"] == "recycle-bin":
            raise ActionError("emptying the Recycle Bin is always permanent - choose 'Delete permanently' for this item")
        path = c["path"]
        self.guard(path, allow_protected=True)
        if not os.path.lexists(lp(path)):
            return []
        min_age = float(c.get("min_age") or 0)
        cutoff = time.time() - min_age * T.DAY
        if c["kind"] == "folder":
            if min_age > 0 and tree_info(path, job)[2] > cutoff:
                raise ActionError(f"kept - changed within the last {min_age:.0f} days")
            return [path]
        if c["kind"] == "folder-contents":
            return [sub for sub, _ in c.get("items", [])
                    if os.path.lexists(lp(sub)) and not (min_age > 0 and tree_info(sub, job)[2] > cutoff)]
        pats = [p.lower() for p in c.get("ps_patterns", [])]
        out = []
        with os.scandir(lp(path)) as entries:
            for e in list(entries):
                if e.is_file(follow_symlinks=False) and any(fnmatch.fnmatch(e.name.lower(), p) for p in pats):
                    st = e.stat(follow_symlinks=False)
                    t = st.st_mtime if c.get("age_basis") == "modified" else max(st.st_mtime, st.st_atime)
                    if t <= cutoff:
                        out.append(T.join(path, e.name))
        return out

    def do_recycle(self, it: dict, job: Job) -> dict:
        if it["kind"] not in ("file", "cand"):
            raise ActionError("only clean-up candidates, files and duplicates can be deleted here")
        targets = self.recycle_targets(it, job)
        if not targets:
            return dict(freed=0, msg="nothing left to remove")
        info = recycle_bin_info(targets[0])
        if not info["enabled"]:
            raise ActionError(f"the Recycle Bin is switched off for {info['drive']} - nothing was removed; "
                              "choose 'Delete permanently' or turn it on in Recycle Bin properties")
        recycled = skipped = failed = 0
        notes = []
        batch, batch_bytes = [], 0

        def flush():
            nonlocal recycled, failed, batch, batch_bytes
            if not batch:
                return
            recycle([p for p, _ in batch])
            for p, size in batch:
                if os.path.lexists(lp(p)):
                    failed += 1
                else:
                    recycled += size
                    job.bytes_done += size
            batch, batch_bytes = [], 0

        for p in targets:
            if job.cancel.is_set():
                raise Cancelled()
            size = tree_info(p, job)[0]
            if len(p) >= 259:
                skipped += 1
                notes.append("path too long for the Recycle Bin")
                continue
            if size > info["max_bytes"]:
                skipped += 1
                notes.append(f"{os.path.basename(p)} ({T.human(size)}) is larger than the Recycle Bin "
                             f"({T.human(info['max_bytes'])}) - not removed")
                continue
            batch.append((p, size))
            batch_bytes += size
            if len(batch) >= 100 or batch_bytes > info["max_bytes"] // 2:
                flush()
        flush()
        msg = f"moved {T.human(recycled)} to the Recycle Bin - empty it to get the space back"
        if failed:
            msg += f"; {failed} item(s) in use or locked"
        if notes:
            msg += "; " + "; ".join(sorted(set(notes))[:3])
        return dict(ok=recycled > 0 or not (failed or skipped), freed=0, recycled=recycled, msg=msg)

    def do_delete(self, it: dict, opts: dict, job: Job) -> dict:
        if opts.get("mode") == "recycle":
            return self.do_recycle(it, job)
        if it["kind"] == "file":
            self.guard(it["path"])
            if is_link(it["path"]):
                raise ActionError("is a link - not deleted")
            st = os.stat(lp(it["path"]), follow_symlinks=False)
            remove_file(it["path"])
            job.bytes_done += st.st_size
            note = "also removed from OneDrive (it is in the OneDrive recycle bin)" if under(it["path"], self.onedrive) else ""
            return dict(freed=local_size(st), msg=note)
        if it["kind"] != "cand":
            raise ActionError("only clean-up candidates, files and duplicates can be deleted here")
        c = it["cand"]
        if c["action"] == "info":
            raise ActionError("managed by Windows/an app - use: " + c["how"])
        self.check_app_closed(c)
        path = c["path"]
        self.guard(path, allow_protected=True)          # candidates come from explicit rules (system ones included)
        if not os.path.lexists(lp(path)):
            return dict(freed=0, msg="already gone")
        min_age = float(c.get("min_age") or 0)
        cutoff = time.time() - min_age * T.DAY

        if c.get("ps"):
            exe = c["ps"].split()[0]
            if exe.lower().startswith(("clear-recyclebin", "delete-deliveryoptimizationcache")) or shutil.which(exe):
                before = tree_info(path, job)[0] if os.path.isdir(lp(path)) else 0
                job.say(f"running: {c['ps']}")
                r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", c["ps"]], timeout=3600)
                after = tree_info(path, job)[0] if os.path.isdir(lp(path)) else 0
                if r.returncode != 0 and before == after:
                    raise ActionError((r.stderr or r.stdout).strip()[:300] or f"'{c['ps']}' failed")
                job.bytes_done += before
                return dict(freed=max(0, before - after), msg=f"ran '{c['ps']}'")
            job.say(f"'{exe}' is not installed - deleting the folder contents instead", "warn")

        if c["kind"] == "folder":
            if min_age > 0:
                _, _, newest = tree_info(path, job)
                if newest > cutoff:
                    raise ActionError(f"kept - changed within the last {min_age:.0f} days")
            freed, failed = delete_tree(path, job, keep_root=True)
        elif c["kind"] == "folder-contents":
            freed = failed = 0
            for sub, _ in c.get("items", []):
                if job.cancel.is_set():
                    raise Cancelled()
                if not os.path.lexists(lp(sub)):
                    continue
                job.current = sub
                try:
                    if min_age > 0 and tree_info(sub, job)[2] > cutoff:
                        continue
                    f, x = delete_tree(sub, job)
                    freed += f
                    failed += x
                except OSError:
                    failed += 1
        else:  # files matching a file rule in one folder
            pats = [p.lower() for p in c.get("ps_patterns", [])]
            basis = c.get("age_basis", "used")
            freed = failed = 0
            with os.scandir(lp(path)) as entries:
                for e in list(entries):
                    if not e.is_file(follow_symlinks=False) or not any(fnmatch.fnmatch(e.name.lower(), p) for p in pats):
                        continue
                    st = e.stat(follow_symlinks=False)
                    t = st.st_mtime if basis == "modified" else max(st.st_mtime, st.st_atime)
                    if t > cutoff:
                        continue
                    try:
                        remove_file(T.join(path, e.name))
                        freed += local_size(st)
                        job.bytes_done += st.st_size
                    except OSError:
                        failed += 1
        msg = (f"{failed} item(s) in use or locked - close the app and retry"
               + ("" if self.elevated else ", or restart PC TidyUp as administrator")) if failed else ""
        return dict(ok=freed > 0 or not failed, freed=freed, msg=msg)

    # ------------------------------------------------------------------ compress
    def do_compress(self, it: dict, opts: dict, job: Job) -> dict:
        if it["kind"] not in ("file", "dir"):
            raise ActionError("compress works on files and folders")
        path = it["path"]
        self.guard(path)
        if is_link(path):
            raise ActionError("is a link")
        mode = opts.get("mode", "zip")
        isdir = os.path.isdir(lp(path))
        if mode == "ntfs":
            if under(path, self.onedrive):
                raise ActionError("NTFS compression is not used inside OneDrive - choose ZIP")
            files = [f for f, _ in walk_files(path, job)] if isdir else [path]
            before = sum(alloc_size(f) for f in files)
            cmd = ["compact", "/c", "/s", "/i", "/q", "*"] if isdir else ["compact", "/c", "/i", "/q", os.path.basename(path)]
            r = run(cmd, cwd=path if isdir else os.path.dirname(path), timeout=7200)
            after = sum(alloc_size(f) for f in files)
            job.bytes_done += it["bytes"] or 0
            if r.returncode != 0 and after >= before:
                raise ActionError(r.stdout.strip()[-200:] or "compact failed")
            return dict(freed=max(0, before - after), msg="NTFS-compressed (files stay usable)")

        # ZIP: write, verify, then remove the original
        if path.lower().endswith((".zip", ".7z", ".rar", ".gz", ".jpg", ".png", ".mp4", ".docx", ".xlsx", ".pptx", ".pdf")):
            raise ActionError("already a compressed format - zipping would not help")
        dest = unique_path(path.rstrip("\\") + ".zip")
        if isdir:
            files = list(walk_files(path, job))
            size = sum(s.st_size for _, s in files)
        else:
            st = os.stat(lp(path))
            files, size = [(path, st)], st.st_size
        if any(getattr(s, "st_file_attributes", 0) & T.CLOUD_ONLY for _, s in files):
            raise ActionError("contains online-only OneDrive files - zipping would download them first")
        try:
            with zipfile.ZipFile(lp(dest), "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                for f, s in files:
                    arc = os.path.relpath(f, os.path.dirname(path)) if isdir else os.path.basename(f)
                    zi = zipfile.ZipInfo.from_file(lp(f), arcname=arc)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    with open(lp(f), "rb") as src, zf.open(zi, "w", force_zip64=True) as out:
                        while True:
                            if job.cancel.is_set():
                                raise Cancelled()
                            b = src.read(4 * 1024 * 1024)
                            if not b:
                                break
                            out.write(b)
                            job.bytes_done += len(b)
            with zipfile.ZipFile(lp(dest)) as zf:
                if zf.testzip() is not None or sum(i.file_size for i in zf.infolist()) != size:
                    raise ActionError("verification of the ZIP failed - original kept")
        except BaseException:
            try:
                os.remove(lp(dest))
            except OSError:
                pass
            raise
        zsize = os.path.getsize(lp(dest))
        if size and zsize > 0.9 * size:
            os.remove(lp(dest))
            raise ActionError(f"only {100 - 100 * zsize / size:.0f}% smaller - original kept")
        if isdir:
            delete_tree(path, job)
        else:
            os.utime(lp(dest), (files[0][1].st_atime, files[0][1].st_mtime))
            remove_file(path)
        return dict(freed=size - zsize, msg=f"-> {os.path.basename(dest)} ({100 - 100 * zsize / max(size, 1):.0f}% smaller)")

    # ------------------------------------------------------------------ archive
    def prepare_archive_root(self, job: Job):
        if not self.onedrive or not os.path.isdir(self.onedrive):
            raise ActionError("OneDrive folder not found")
        os.makedirs(lp(self.archive_root), exist_ok=True)
        readme = os.path.join(self.archive_root, "_About TidyUp Archive.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write("Files and folders archived by TidyUp. Each item sits under its original path\n"
                        "(C\\Users\\... = C:\\Users\\...). _TidyUp-Archive-Index.csv lists where everything came from.\n"
                        "To bring something back, use Restore in the PC TidyUp app or move it back yourself.\n")
        job.say(f"Archive root: {self.archive_root}")

    def archive_dest(self, src: str) -> str:
        drive, rest = os.path.splitdrive(os.path.normpath(src))
        if drive.startswith("\\\\"):                 # \\server\share -> UNC\server\share
            drive = "UNC\\" + drive.lstrip("\\")
        drive = drive.rstrip(":") or "X"
        return os.path.join(self.archive_root, drive, rest.lstrip("\\"))

    def check_onedrive_names(self, rel_parts: list[str]):
        for part in rel_parts:
            low = part.lower()
            if (low in INVALID_ONEDRIVE_NAMES or os.path.splitext(low)[0] in INVALID_ONEDRIVE_NAMES
                    or "_vti_" in low or low.startswith("~$") or part != part.strip() or part.endswith(".")):
                raise ActionError(f"name '{part}' is not allowed in OneDrive - rename it first")

    def do_archive(self, it: dict, opts: dict, job: Job) -> dict:
        if it["kind"] == "cand":
            if it["cand"]["kind"] != "folder":
                raise ActionError("only whole folders can be archived from the clean-up list")
        elif it["kind"] not in ("file", "dir"):
            raise ActionError("archive works on files and folders")
        src = os.path.normpath(it["path"])
        self.guard(src)
        if not os.path.lexists(lp(src)):
            raise ActionError("no longer exists")
        if is_link(src):
            raise ActionError("is already a link")
        if under(src, self.archive_root):
            raise ActionError("already archived")
        if under(src, self.onedrive):
            raise ActionError("already in OneDrive - use 'Make online-only' instead")
        isdir = os.path.isdir(lp(src))
        dest = unique_path(self.archive_dest(src))
        if len(dest) > 400:
            raise ActionError("path would be longer than OneDrive's 400-character limit")
        rel = os.path.relpath(dest, self.onedrive).split(os.sep)
        self.check_onedrive_names(rel)
        maxf = int(self.archive_cfg.get("max_folder_files", 20000))
        if isdir:
            size, n, _ = tree_info(src, job, limit=maxf)
            if n > maxf:
                raise ActionError(f"more than {maxf:,} files - too many for OneDrive sync; Compress (ZIP) it, then archive the .zip")
            for f, s in walk_files(src, job):
                self.check_onedrive_names([os.path.basename(f)])
                if s.st_size > 250 * 1024 ** 3:
                    raise ActionError(f"{os.path.basename(f)} is larger than OneDrive's 250 GB limit")
        else:
            st = os.stat(lp(src))
            size, n = st.st_size, 1
            if size > 250 * 1024 ** 3:
                raise ActionError("larger than OneDrive's 250 GB file limit")

        os.makedirs(lp(os.path.dirname(dest)), exist_ok=True)
        same_volume = os.path.splitdrive(src)[0].lower() == os.path.splitdrive(dest)[0].lower()
        try:
            if same_volume:
                os.rename(lp(src), lp(dest))           # instant, nothing is copied
            else:
                if shutil.disk_usage(self.archive_root).free < size + 1024 ** 3:
                    raise ActionError("not enough free space on the OneDrive drive to copy it there")
                (shutil.copytree if isdir else shutil.copy2)(lp(src), lp(dest))
                delete_tree(src, job)
        except PermissionError:
            raise ActionError("in use - close the program that has it open and retry")
        job.bytes_done += size

        online_only = opts.get("online_only", self.archive_cfg.get("make_online_only", True))
        if online_only:
            run(["attrib", "+U", "-P", dest])
            if isdir:
                run(["attrib", "+U", "-P", os.path.join(dest, "*"), "/S", "/D"])

        link_type = link_path = None
        warn = ""
        if opts.get("leave_link", self.archive_cfg.get("leave_link", False)):
            try:
                if isdir:
                    import _winapi
                    _winapi.CreateJunction(dest, src)
                    link_type, link_path = "junction", src
                else:
                    try:
                        os.symlink(dest, src)
                        link_type, link_path = "symlink", src
                    except OSError as e:
                        if getattr(e, "winerror", 0) != 1314 or not opts.get("shortcut_fallback", True):
                            raise
                        make_shortcut(src + ".lnk", dest)
                        link_type, link_path = "shortcut", src + ".lnk"
            except (OSError, ActionError) as e:
                warn = f"moved, but the link could not be created: {e}"

        entry = dict(id=uuid.uuid4().hex[:10], ts=time.strftime("%Y-%m-%d %H:%M"), original=src, archived=dest,
                     kind="folder" if isdir else "file", bytes=size, files=n, link=link_type, link_path=link_path,
                     online_only=bool(online_only), restored=None)
        with self.lock:
            entries = self._read_json(self.manifest_file, [])
            entries.append(entry)
            self._write_json(self.manifest_file, entries)
        try:
            idx = os.path.join(self.archive_root, "_TidyUp-Archive-Index.csv")
            new = not os.path.exists(idx)
            with open(idx, "a", encoding="utf-8-sig") as f:
                if new:
                    f.write("archived_on;original_path;archived_path;size_bytes;files;link\n")
                f.write(f"{entry['ts']};{src};{dest};{size};{n};{link_type or ''}\n")
        except OSError:
            pass
        msg = warn or ("moved" + (f", {link_type} left at the old location" if link_type else "")
                       + ("; online-only once OneDrive has uploaded it" if online_only else ""))
        return dict(freed=size if online_only else 0, msg=msg, archived=dest)

    # ------------------------------------------------------------------ offload / restore
    def do_offload(self, it: dict, opts: dict, job: Job) -> dict:
        path = it["path"]
        if not under(path, self.onedrive) and "offload" not in it.get("lists", ()):
            raise ActionError("not in a OneDrive folder")
        if not os.path.exists(lp(path)):
            raise ActionError("no longer exists")
        isdir = os.path.isdir(lp(path))
        r = run(["attrib", "+U", "-P", path])
        if isdir:
            r = run(["attrib", "+U", "-P", os.path.join(path, "*"), "/S", "/D"])
        if r.returncode != 0:
            raise ActionError(r.stdout.strip() or r.stderr.strip() or "attrib failed")
        job.bytes_done += it["bytes"] or 0
        return dict(freed=it["bytes"] or 0, msg="marked online-only - OneDrive frees the space in a moment")

    def do_restore(self, it: dict, opts: dict, job: Job) -> dict:
        e = it["entry"]
        if e.get("restored"):
            raise ActionError("already restored")
        src, dest = e["archived"], e["original"]
        if not os.path.lexists(lp(src)):
            raise ActionError("the archived copy no longer exists in OneDrive")
        if e.get("link_path") and os.path.lexists(lp(e["link_path"])):
            if e["link"] == "shortcut":
                os.remove(lp(e["link_path"]))
            elif is_link(e["link_path"]):
                remove_link(e["link_path"])
        if os.path.lexists(lp(dest)):
            raise ActionError("something else now exists at the original location")
        os.makedirs(lp(os.path.dirname(dest)), exist_ok=True)
        job.say("downloading from OneDrive if needed ...")
        if os.path.isdir(lp(src)):
            shutil.copytree(lp(src), lp(dest), symlinks=True)
        else:
            shutil.copy2(lp(src), lp(dest))     # reading an online-only file downloads it
        size, n, _ = tree_info(dest, job)
        if n != e.get("files", n):
            raise ActionError("copy incomplete - the archived copy was kept")
        delete_tree(src, job)
        with self.lock:
            entries = self._read_json(self.manifest_file, [])
            for x in entries:
                if x["id"] == e["id"]:
                    x["restored"] = time.strftime("%Y-%m-%d %H:%M")
            self._write_json(self.manifest_file, entries)
        return dict(freed=0, msg=f"restored to {dest}")

    def manifest_status(self) -> list:
        out = []
        for e in reversed(self.manifest()):
            e = dict(e)
            if e.get("restored"):
                e["status"] = "restored"
            elif not os.path.lexists(lp(e["archived"])):
                e["status"] = "missing"
            else:
                if e["kind"] == "file":
                    a = file_attrs(e["archived"])
                    cloud = 1 if a & T.CLOUD_ONLY else 0
                    total = 1
                else:
                    cloud = total = 0
                    for _, s in walk_files(e["archived"]):
                        total += 1
                        cloud += 1 if s.st_file_attributes & T.CLOUD_ONLY else 0
                        if total >= 3000:
                            break
                e["status"] = ("online-only" if total and cloud == total else
                               "uploading / pending" if e.get("online_only") else "kept on this PC")
                e["cloud_pct"] = round(100 * cloud / total) if total else 100
            out.append(e)
        return out


PROCESS_ALIASES = {"msedge": "edge", "devenv": "visualstudio", "ms-teams": "msteams", "brave": "brave-browser",
                   "code - insiders": "code - insiders"}


def running_app_for(path: str) -> str | None:
    """Name of a running program whose name matches a folder in `path` (e.g. chrome.exe for ...\\Google\\Chrome\\...)."""
    try:
        out = run(["tasklist", "/FO", "CSV", "/NH"], timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    segments = {s.lower() for s in os.path.normpath(path).split(os.sep)}
    for line in out.splitlines():
        exe = line.split('","')[0].strip('"')
        name = exe[:-4].lower() if exe.lower().endswith(".exe") else exe.lower()
        for cand in {name, PROCESS_ALIASES.get(name, name)}:
            if len(cand) >= 4 and cand in segments:
                return exe
    return None


def reveal_in_explorer(p: str) -> dict:
    """Opens a folder in File Explorer, or the containing folder with the file selected.
    Never launches a file: files are always shown with /select. Missing paths open the nearest existing parent."""
    p = p.strip().strip('"')
    if not p or not os.path.isabs(p) or '"' in p or "::" in p or re.match(r"^[a-z]{2,}:", p, re.I):
        raise ActionError("not a local file or folder path")
    target, note = os.path.normpath(p), ""
    while not os.path.lexists(lp(target)):
        parent = os.path.dirname(target)
        if parent == target:
            raise ActionError("this location no longer exists")
        target, note = parent, "the item no longer exists - opened the nearest folder"
    if os.path.isdir(lp(target)) and not is_link(target):
        cmd = f'explorer.exe "{target}"'
    else:
        cmd = f'explorer.exe /select,"{target}"'
    subprocess.Popen(cmd)          # explorer.exe returns 1 even on success - nothing to check
    return {"ok": True, "opened": target, "note": note}


# --------------------------------------------------------------------------- settings (edited from the page)
LIST_SETTINGS = ("protected", "advice_exclude", "duplicates_exclude", "skip", "cloud_sync_roots")
APP_PROTECT_DEFAULTS = ["{windir}/**", "{programfiles}/**", "{programfilesx86}/**", "{programdata}/**"]


def _num(v, lo=0, hi=None, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("must be a number")
    if v < lo or (hi is not None and v > hi):
        raise ValueError(f"must be between {lo} and {hi}" if hi is not None else f"must be at least {lo}")
    if integer and int(v) != v:
        raise ValueError("must be a whole number")
    return int(v) if integer else v


def validate_config(new: dict, old: dict) -> tuple[list, list, dict]:
    """Returns (errors, warnings, cleaned config). Keys starting with '_' (comments) are preserved."""
    errors, warnings = [], []
    cfg = json.loads(json.dumps(new))
    variables = T.build_vars()

    def check(label, fn):
        try:
            fn()
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"{label}: {e}")

    roots = cfg.get("roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r.strip() for r in roots):
        errors.append("Folders to scan: add at least one folder or drive")
    else:
        cfg["roots"] = [r.strip() for r in roots]
        for r in cfg["roots"]:
            if not os.path.isdir(r):
                errors.append(f"Folders to scan: '{r}' does not exist")
    check("Reports to keep", lambda: cfg.__setitem__("keep_reports", _num(cfg.get("keep_reports", 10), 1, 1000, True)))
    if not isinstance(cfg.get("duplicates", True), bool):
        errors.append("Duplicate detection: must be on or off")
    if cfg.get("use_last_access_time", "auto") not in ("auto", True, False):
        errors.append("Last-access time: must be auto, on or off")
    for section in ("thresholds", "priority"):
        for k, v in (cfg.get(section) or {}).items():
            if not k.startswith("_"):
                check(f"{section}.{k}", lambda k=k, v=v, section=section: cfg[section].__setitem__(k, _num(v)))
    a = cfg.setdefault("archive", {})
    if not isinstance(a.get("root", ""), str) or not a.get("root", "").strip():
        errors.append("Archive root: required")
    else:
        resolved = T.archive_root(cfg)
        od = T.onedrive_root()
        if od and not under(resolved, od):
            warnings.append(f"Archive root {resolved} is not inside your OneDrive folder - archived items would not go to the cloud")
    for b in ("make_online_only", "leave_link", "shortcut_fallback"):
        if not isinstance(a.get(b, True), bool):
            errors.append(f"Archive {b}: must be on or off")
    check("Archive: max files per folder", lambda: a.__setitem__("max_folder_files", _num(a.get("max_folder_files", 20000), 1, 10_000_000, True)))
    ap = cfg.setdefault("app", {})
    check("App port", lambda: ap.__setitem__("port", _num(ap.get("port", 8765), 0, 65535, True)))
    check("App idle shutdown", lambda: ap.__setitem__("idle_shutdown_minutes", _num(ap.get("idle_shutdown_minutes", 20), 1, 1440)))
    if ap.get("port") != (old.get("app") or {}).get("port"):
        warnings.append("The new port is used the next time PC TidyUp starts")
    if cfg.get("report_dir") != old.get("report_dir"):
        warnings.append("The new report folder is used the next time PC TidyUp starts")
    for key in LIST_SETTINGS:
        vals = cfg.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            errors.append(f"{key}: must be a list of paths/patterns")
            continue
        cfg[key] = [v.strip() for v in vals if v.strip()]
        for v in cfg[key]:
            unknown = set(re.findall(r"\{([A-Za-z0-9_]+)\}", v)) - set(variables)
            if unknown:
                errors.append(f"{key}: '{v}' uses unknown variable(s) " + ", ".join("{" + u + "}" for u in unknown))
            else:
                try:
                    T.glob_re(T.expand(v, variables))
                except re.error as e:
                    errors.append(f"{key}: '{v}' is not a valid pattern ({e})")
    missing = [p for p in APP_PROTECT_DEFAULTS if p not in cfg.get("protected", [])]
    if missing:
        cfg["protected"] = missing + cfg.get("protected", [])
        warnings.append("Kept the system folders " + ", ".join(missing) + " in the protected list - they cannot be removed")
    return errors, warnings, cfg


def validate_user_rules(user: dict) -> list[str]:
    """Validates tidyup.rules.user.json content merged with the built-in rules, without touching the real file."""
    import tempfile
    if not isinstance(user, dict):
        return ["rules must be an object"]
    problems = []
    for kind in ("dir_rules", "file_rules"):
        lst = user.get(kind, [])
        if not isinstance(lst, list):
            problems.append(f"{kind} must be a list")
            continue
        ids = [r.get("id") for r in lst if isinstance(r, dict)]
        for i in {x for x in ids if ids.count(x) > 1}:
            problems.append(f"{kind}: id '{i}' is used twice")
        for r in lst:
            if not isinstance(r, dict) or not r.get("id"):
                problems.append(f"{kind}: every rule needs an id")
    if problems:
        return problems
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(RULES, os.path.join(d, RULES.name))
        with open(os.path.join(d, T.USER_RULES), "w", encoding="utf-8") as f:
            json.dump(user, f)
        try:
            merged = T.load_rules(os.path.join(d, RULES.name))
        except (ValueError, KeyError, TypeError) as e:
            return [f"could not read the rules: {e}"]
        return T.validate_rules(merged, T.build_vars())


def save_json_with_backup(path: Path, obj: dict):
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def settings_payload(app: "App") -> dict:
    user_path = RULES.with_name(T.USER_RULES)
    builtin = T.load_json(RULES)
    variables = {k: v.replace("/", "\\") for k, v in T.build_vars().items()}
    return dict(config=T.load_json(CONFIG), config_path=str(CONFIG),
                user_rules=T.load_json(user_path) if user_path.exists() else {"dir_rules": [], "file_rules": []},
                rules_path=str(user_path), builtin_path=str(RULES),
                builtin=dict(dir_rules=builtin.get("dir_rules", []), file_rules=builtin.get("file_rules", [])),
                variables=variables, hard_protected=APP_PROTECT_DEFAULTS, archive_root=app.archive_root,
                actions=T.ACTIONS)


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PC TidyUp"
    app: App = None  # set in main

    def log_message(self, *args):
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _send(self, code: int, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, default=str).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-TidyUp-Token", ""), self.app.token)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        app = self.app
        if u.path == "/":
            app.last_ping = time.time()
            return self._send(200, T.render_html(app.data, app.app_info()), "text/html")
        if u.path == "/disclaimer":
            try:
                return self._send(200, (HERE / "DISCLAIMER.md").read_text(encoding="utf-8"), "text/plain")
            except OSError:
                return self._send(404, "DISCLAIMER.md is missing - get it from the original distribution.", "text/plain")
        if u.path == "/api/ping":
            return self._send(200, {"ok": True, "elevated": app.elevated, "pid": os.getpid()})
        if not self._authed():
            return self._send(401, {"error": "invalid token - reload the page"})
        app.last_ping = time.time()
        if u.path == "/api/status":
            log_from = int((parse_qs(u.query).get("log_from") or ["0"])[0] or 0)
            return self._send(200, {"job": app.job.to_json(log_from) if app.job else None, "elevated": app.elevated,
                                    "stamp": (app.data or {}).get("meta", {}).get("stamp")})
        if u.path == "/api/settings":
            return self._send(200, settings_payload(app))
        if u.path == "/api/data":            # the current report, for in-place page updates after a scan
            return self._send(200, {"data": app.data, "state": app.action_state()})
        if u.path == "/api/recyclebin":
            try:
                return self._send(200, recycle_bin_info(os.environ.get("SystemDrive", "C:") + "\\"))
            except OSError as e:
                return self._send(200, {"enabled": None, "error": str(e)})
        if u.path == "/api/archive":
            return self._send(200, {"root": app.archive_root, "entries": app.manifest_status()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._authed():
            return self._send(403, {"error": "forbidden - reload the page"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "JSON expected"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5 * 1024 * 1024:
            return self._send(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        app = self.app
        app.last_ping = time.time()
        path = urlparse(self.path).path
        try:
            if path == "/api/cancel":
                if app.job and app.job.state == "running":
                    app.job.cancel.set()
                return self._send(200, {"ok": True})
            if path == "/api/shutdown":
                threading.Thread(target=shutdown_soon, args=(app,), daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/elevate":
                return self._send(200, elevate(app))
            if path == "/api/disclaimer/accept":
                if body.get("accept") is not True or body.get("version") != DISCLAIMER_VERSION:
                    raise ActionError("the disclaimer must be explicitly accepted")
                app.accept_disclaimer()
                return self._send(200, {"ok": True, "version": DISCLAIMER_VERSION})
            if path == "/api/reveal":
                return self._send(200, reveal_in_explorer(str(body.get("path") or "")))
            if path == "/api/explain":
                p = str(body.get("path") or "").strip().strip('"')
                if not p or not os.path.isabs(p):
                    raise ActionError("enter a full path, e.g. C:\\Users\\you\\Downloads\\setup.msi")
                return self._send(200, {"text": T.explain(p, T.load_json(CONFIG), T.load_rules(RULES))})
            if path == "/api/settings/config":
                if app.busy():
                    raise ActionError("wait until the running task has finished")
                errors, warnings, cfg = validate_config(body.get("config") or {}, app.cfg)
                if errors:
                    return self._send(400, {"error": "Not saved - please fix: " + "; ".join(errors[:3]), "errors": errors})
                save_json_with_backup(CONFIG, cfg)
                app.cfg = cfg
                app.apply_config()
                app.audit("settings: tidyup.config.json saved from the page")
                return self._send(200, {"ok": True, "warnings": warnings, "config": cfg, "archive_root": app.archive_root})
            if path == "/api/settings/rules":
                user = body.get("user_rules")
                problems = validate_user_rules(user)
                if problems:
                    return self._send(400, {"error": "Not saved - please fix: " + "; ".join(problems[:3]), "errors": problems})
                save_json_with_backup(RULES.with_name(T.USER_RULES), user)
                app.audit("settings: tidyup.rules.user.json saved from the page")
                return self._send(200, {"ok": True})
            if path in ("/api/scan", "/api/action"):
                with app.lock:
                    if app.busy():
                        raise ActionError("another task is still running")
                    job = app.start_scan(body) if path == "/api/scan" else app.start_action(body)
                return self._send(200, {"job": job.to_json()})
            return self._send(404, {"error": "not found"})
        except ActionError as e:
            return self._send(400, {"error": str(e)})


# --------------------------------------------------------------------------- lifecycle
SERVER: ThreadingHTTPServer | None = None


def shutdown_soon(app: App, delay: float = 0.5):
    time.sleep(delay)
    if app.job and app.job.state == "running":
        app.job.cancel.set()
        time.sleep(1)
    try:
        (app.out / ".tidyup-app.json").unlink()
    except OSError:
        pass
    if SERVER:
        SERVER.shutdown()


def elevate(app: App) -> dict:
    if app.elevated:
        return {"ok": True, "elevated": True}
    if app.busy():
        raise ActionError("wait until the running task has finished")
    args = (f'"{Path(__file__).resolve()}" --port {app.port} --no-browser --wait-port '
            f'--config "{CONFIG}" --rules "{RULES}"')
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, str(HERE), 1)
    if rc <= 32:
        raise ActionError("administrator restart was cancelled")
    threading.Thread(target=shutdown_soon, args=(app, 1.0), daemon=True).start()
    return {"ok": True, "restarting": True}


def watchdog(app: App, idle_minutes: float):
    while True:
        time.sleep(30)
        if not app.busy() and time.time() - app.last_ping > idle_minutes * 60:
            app.audit("idle - stopping the PC TidyUp app")
            shutdown_soon(app, 0)
            return


def existing_instance(out: Path) -> str | None:
    try:
        info = T.load_json(out / ".tidyup-app.json")
        url = f"http://127.0.0.1:{info['port']}/"
        with urllib.request.urlopen(url + "api/ping", timeout=2) as r:
            if json.loads(r.read()).get("ok"):
                return url
    except Exception:  # noqa: BLE001
        return None
    return None


def main(argv=None):
    global SERVER, CONFIG, RULES
    ap = argparse.ArgumentParser(description="PC TidyUp app: report + actions in the browser")
    ap.add_argument("--port", type=int, help="port (default from config app.port; 0 = any free port)")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--rules", default=str(RULES))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--wait-port", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    CONFIG, RULES = Path(args.config).resolve(), Path(args.rules).resolve()
    user_rules, example = RULES.with_name(T.USER_RULES), RULES.with_name("tidyup.rules.user.example.json")
    if not user_rules.exists() and example.exists():      # first start after a fresh clone
        shutil.copyfile(example, user_rules)

    cfg = T.load_json(CONFIG)
    out = T.report_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    if not args.wait_port:
        url = existing_instance(out)
        if url:
            print(f"PC TidyUp is already running: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    port = args.port if args.port is not None else int((cfg.get("app") or {}).get("port", 8765))
    deadline = time.time() + (30 if args.wait_port else 0)
    while True:
        try:
            Handler.app = None
            SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            if time.time() < deadline:
                time.sleep(0.5)
                continue
            if port == 0:
                raise
            port = 0                       # configured port busy: take any free one
    port = SERVER.server_address[1]
    app = App(port)
    Handler.app = app
    with open(out / ".tidyup-app.json", "w", encoding="utf-8") as f:
        json.dump({"port": port, "pid": os.getpid()}, f)
    url = f"http://127.0.0.1:{port}/"
    app.audit(f"app started on {url} (elevated={app.elevated})")
    print(f"PC TidyUp app running at {url}{'  [administrator]' if app.elevated else ''}")
    print("Use PC TidyUp entirely at your own risk - provided 'as is', without warranty or liability. See DISCLAIMER.md.")
    print("Keep this window open while you use the page. Ctrl+C (or 'Stop PC TidyUp' on the page) to quit.")
    threading.Thread(target=watchdog, args=(app, float((cfg.get("app") or {}).get("idle_shutdown_minutes", 20))),
                     daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        SERVER.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            (out / ".tidyup-app.json").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
