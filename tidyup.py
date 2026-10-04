#!/usr/bin/env python3
# PC TidyUp - Copyright (c) 2026 Mehrdad Ghazvinizadeh
# Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE.md).
# Free for personal / noncommercial use; any commercial use requires the author's written permission.
"""
PC TidyUp - storage usage review for Windows.

Scans one or more roots, explains where the space goes and suggests what to
delete, compress, archive or offload to the cloud. It never deletes anything
itself: it writes an HTML + Markdown report, a history snapshot (for trends
between runs) and a dry-run-by-default PowerShell cleanup script.

    python tidyup.py                      # uses tidyup.config.json
    python tidyup.py --root C:\\Users\\me   # scan a different root
    python tidyup.py --no-duplicates --open

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import heapq
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

VERSION = "1.0.1"
HERE = Path(__file__).resolve().parent
DAY = 86400.0
MB = 1024 * 1024

# Windows file attributes
A_HIDDEN = 0x2
A_SYSTEM = 0x4
A_REPARSE = 0x400
A_COMPRESSED = 0x800
A_OFFLINE = 0x1000
A_RECALL_ON_OPEN = 0x40000
A_PINNED = 0x80000
A_UNPINNED = 0x100000
A_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY = A_OFFLINE | A_RECALL_ON_OPEN | A_RECALL_ON_DATA_ACCESS

ACTIONS = ["safe", "likely", "review", "info"]
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def path_is_junction(path: str) -> bool:
    """os.path.isjunction for every Python version (the built-in exists from 3.12 only)."""
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & A_REPARSE) and \
        getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def entry_is_junction(entry, full_path: str) -> bool:
    """DirEntry.is_junction for every Python version."""
    if hasattr(entry, "is_junction"):
        return entry.is_junction()
    try:
        if not entry.stat(follow_symlinks=False).st_file_attributes & A_REPARSE:
            return False                     # cheap: no reparse point at all
    except OSError:
        return False
    return path_is_junction(full_path)


class ScanCancelled(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def norm(p: str) -> str:
    """Comparable form of a path: lower case, forward slashes, no trailing slash."""
    p = p.replace("\\", "/").lower()
    if p.startswith("//?/"):
        p = p[4:]
    return p.rstrip("/") if len(p) > 3 else p


def longpath(p: str) -> str:
    if p.startswith("\\\\"):
        return p
    return "\\\\?\\" + p


def join(parent: str, name: str) -> str:
    return parent + name if parent.endswith("\\") else parent + "\\" + name


def ts_iso(t: float) -> str | None:
    if not t:
        return None
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_vars() -> dict:
    env = os.environ
    home = env.get("USERPROFILE") or str(Path.home())
    sd = env.get("SystemDrive", "C:")
    raw = {
        "home": home,
        "localappdata": env.get("LOCALAPPDATA", home + r"\AppData\Local"),
        "appdata": env.get("APPDATA", home + r"\AppData\Roaming"),
        "temp": env.get("TEMP", home + r"\AppData\Local\Temp"),
        "programdata": env.get("ProgramData", sd + r"\ProgramData"),
        "windir": env.get("SystemRoot", sd + r"\Windows"),
        "systemdrive": sd + "\\",
        "programfiles": env.get("ProgramFiles", sd + r"\Program Files"),
        "programfilesx86": env.get("ProgramFiles(x86)", sd + r"\Program Files (x86)"),
        "downloads": home + r"\Downloads",
        "onedrive": env.get("OneDriveCommercial") or env.get("OneDrive") or (home + r"\OneDrive"),
        "onedrivepersonal": env.get("OneDriveConsumer") or (home + r"\OneDrive"),
    }
    out = {}
    for k, v in raw.items():
        try:
            if os.path.exists(v):
                v = os.path.realpath(v)  # expands 8.3 short names such as MEHRDA~1
        except OSError:
            pass
        out[k] = norm(v)
    return out


def expand(pattern: str, variables: dict) -> str:
    def rep(m):
        key = m.group(1).lower()
        return variables[key] if key in variables else m.group(0)
    p = re.sub(r"\{([A-Za-z0-9_]+)\}", rep, pattern)
    return p.replace("\\", "/").lower()


def _glob_body(pat: str) -> str:
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3; continue
        if pat.startswith("**", i):
            out.append(".*"); i += 2; continue
        c = pat[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j > i:
                out.append(pat[i:j + 1]); i = j + 1; continue
            out.append(r"\[")
        elif c == "{":
            depth, j = 0, i
            while j < len(pat):
                if pat[j] == "{": depth += 1
                elif pat[j] == "}":
                    depth -= 1
                    if depth == 0: break
                j += 1
            inner = pat[i + 1:j]
            parts, buf, d = [], "", 0
            for ch in inner:
                if ch == "," and d == 0:
                    parts.append(buf); buf = ""; continue
                d += ch == "{"; d -= ch == "}"
                buf += ch
            parts.append(buf)
            out.append("(?:" + "|".join(_glob_body(p) for p in parts) + ")")
            i = j + 1; continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def glob_re(pattern: str) -> re.Pattern:
    """Glob -> regex. '*' stays inside one folder, '**' crosses folders, {a,b} alternates."""
    return re.compile(_glob_body(pattern) + r"\Z")


def last_segment_keys(pattern: str) -> set | None:
    """Literal names the last path segment can take, or None when it contains wildcards."""
    depth, cut = 0, -1
    for i, ch in enumerate(pattern):
        if ch == "{": depth += 1
        elif ch == "}": depth -= 1
        elif ch == "/" and depth == 0: cut = i
    seg = pattern[cut + 1:]
    if any(c in seg for c in "*?["):
        return None
    m = re.fullmatch(r"\{([^{}]*)\}", seg)
    if m:
        alts = m.group(1).split(",")
        if any("/" in a for a in alts):
            return {a.rsplit("/", 1)[-1] for a in alts}
        return set(alts)
    if "{" in seg:
        return None
    return {seg}


def atime_trustworthy(setting) -> bool:
    if isinstance(setting, bool):
        return setting
    try:
        out = subprocess.run(["fsutil", "behavior", "query", "disablelastaccess"],
                             capture_output=True, text=True, timeout=10).stdout
        return "ENABLED" in out.upper() or re.search(r"=\s*[02]\b", out) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- rules
class DirRule:
    def __init__(self, d: dict, variables: dict):
        self.id = d["id"]
        self.label = d.get("label", self.id)
        self.action = d.get("action", "review")
        self.mode = d.get("mode", "whole")            # whole | children
        self.min_age = float(d.get("min_age_days", 0))
        self.system = bool(d.get("system", False))     # may match inside protected areas
        self.reason = d.get("reason", "")
        self.how = d.get("how", "")
        self.ps = d.get("ps")                           # optional PowerShell command used instead of deleting
        self.check_running = bool(d.get("check_running"))  # skip while the owning app is running
        pats = d["path"] if isinstance(d["path"], list) else [d["path"]]
        self.patterns = [expand(p, variables) for p in pats]
        self.regexes = [glob_re(p) for p in self.patterns]
        ex = d.get("exclude", [])
        self.excludes = [glob_re(expand(p, variables)) for p in ([ex] if isinstance(ex, str) else ex)]
        keys = [last_segment_keys(p) for p in self.patterns]
        self.keys = None if any(k is None for k in keys) else set().union(*keys)

    def matches(self, npath: str) -> bool:
        return any(r.match(npath) for r in self.regexes) and not any(r.match(npath) for r in self.excludes)


class FileRule:
    def __init__(self, d: dict, variables: dict):
        self.id = d["id"]
        self.label = d.get("label", self.id)
        self.action = d.get("action", "review")
        self.min_age = float(d.get("min_age_days", 0))
        self.min_size = float(d.get("min_size_kb", 0)) * 1024
        self.age_basis = d.get("age_basis", "used")     # used | modified
        self.system = bool(d.get("system", False))
        self.inside = bool(d.get("inside_rule_folders", False))  # also evaluated inside dir-rule folders
        self.reason = d.get("reason", "")
        self.how = d.get("how", "")
        self.exts = {e.lower() for e in d.get("exts", [])}
        self.names = [n.lower() for n in d.get("names", [])]
        self.name_res = [glob_re(n) for n in self.names]
        paths = d.get("path")
        paths = [paths] if isinstance(paths, str) else (paths or [])
        self.path_res = [glob_re(expand(p, variables)) for p in paths]
        self.ps_patterns = sorted({"*" + e for e in self.exts} | set(self.names))

    def matches_name(self, lname: str, ext: str) -> bool:
        if ext in self.exts:
            return True
        return any(r.match(lname) for r in self.name_res)


USER_RULES = "tidyup.rules.user.json"


def load_rules(rules_path) -> dict:
    """Built-in rules + your own rules from tidyup.rules.user.json (next to the rules file).
    A user rule with the same id as a built-in one replaces it; "disabled": true switches a rule off.
    User rules are checked before the built-in ones."""
    rules_path = Path(rules_path)
    rules = load_json(rules_path)
    user_path = rules_path.with_name(USER_RULES)
    rules["_user_rules"] = 0
    if user_path.exists():
        user = load_json(user_path)
        for kind in ("dir_rules", "file_rules"):
            mine = [r for r in user.get(kind, []) if isinstance(r, dict)]
            ids = {r.get("id") for r in mine}
            rules[kind] = mine + [r for r in rules.get(kind, []) if r.get("id") not in ids]
            rules["_user_rules"] += sum(1 for r in mine if not r.get("disabled"))
        cats = rules.setdefault("categories", {})
        for cat, exts in user.get("categories", {}).items():
            if cat.startswith("_"):
                continue
            mine = {e.lower() for e in exts}
            for other in cats:                         # an extension belongs to one category only
                cats[other] = [e for e in cats[other] if e.lower() not in mine]
            cats.pop(cat, None)
            cats[cat] = list(exts)
        rules.setdefault("compression_ratios", {}).update(
            {k: v for k, v in user.get("compression_ratios", {}).items() if not k.startswith("_")})
    for kind in ("dir_rules", "file_rules"):
        rules[kind] = [r for r in rules.get(kind, []) if not r.get("disabled")]
    return rules


def validate_rules(rules: dict, variables: dict) -> list[str]:
    problems, seen = [], set()
    for kind, cls in (("dir_rules", "DirRule"), ("file_rules", "FileRule")):
        for i, r in enumerate(rules.get(kind, [])):
            where = f"{kind}[{i}] {r.get('id', '(no id)')}"
            if not r.get("id"):
                problems.append(f"{where}: missing 'id'")
            elif (kind, r["id"]) in seen:
                problems.append(f"{where}: duplicate id")
            seen.add((kind, r.get("id")))
            if r.get("action", "review") not in ACTIONS:
                problems.append(f"{where}: action must be one of {', '.join(ACTIONS)}")
            if kind == "dir_rules":
                if not r.get("path"):
                    problems.append(f"{where}: missing 'path'")
                if r.get("mode", "whole") not in ("whole", "children"):
                    problems.append(f"{where}: mode must be 'whole' or 'children'")
            elif not (r.get("exts") or r.get("names")):
                problems.append(f"{where}: needs 'exts' or 'names'")
            for e in r.get("exts", []):
                if not str(e).startswith("."):
                    problems.append(f"{where}: extension '{e}' must start with a dot")
            try:
                (DirRule if kind == "dir_rules" else FileRule)(r, variables)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{where}: {e}")
            unknown = set(re.findall(r"\{([A-Za-z0-9_]+)\}", json.dumps(r.get("path", ""))))
            unknown -= set(variables)
            if unknown:
                problems.append(f"{where}: unknown variable(s) {', '.join('{' + u + '}' for u in unknown)}")
    return problems


def explain(path: str, cfg: dict, rules: dict) -> str:
    """Which rules would match this file or folder (ignoring the size/age thresholds)."""
    variables = build_vars()
    sc = Scanner(cfg, rules, variables, False, True)
    path = os.path.abspath(path)
    out = [f"Path: {path}", f"Exists: {os.path.exists(path)}"]
    npath = norm(path)
    out.append(f"Protected (no file-level advice): {'yes' if sc._is_protected(npath) else 'no'}")
    out.append(f"In a OneDrive/SharePoint folder: {'yes' if sc._in_sync_root(npath) else 'no'}")
    parts = npath.split("/")
    hits = []
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i]) if i > 1 else parts[0] + "/"
        prot = sc._is_protected(sub)
        for r in sc.dir_rules:
            if (r.system or not prot) and r.matches(sub):
                hits.append(f"  folder rule '{r.id}' ({r.action}, {r.mode}, min age {r.min_age:g} d) on {sub}")
    out.append("Folder rules on this path or a parent:" if hits else "Folder rules: none match this path or its parents")
    out += hits
    if not os.path.isdir(path):
        lname = os.path.basename(path).lower()
        dot = lname.rfind(".")
        ext = lname[dot:] if dot > 0 else ""
        frs = [r for r in sc.file_rules if r.matches_name(lname, ext)
               and (not r.path_res or any(x.match(npath) for x in r.path_res))]
        if hits and frs:
            inside = [r for r in frs if r.inside]
            out.append("Note: a folder rule covers this file, so it is handled as part of that folder"
                       + (f"; only {', '.join(r.id for r in inside)} still applies (inside_rule_folders)" if inside else
                          " - the file rules below do not apply here"))
        out.append("File rules (first one wins):" if frs else "File rules: none match this file name")
        out += [f"  file rule '{r.id}' ({r.action}, min age {r.min_age:g} d by {r.age_basis}, min size {r.min_size / 1024:g} KB)"
                for r in frs]
        out.append(f"Category: {sc.ext_cat.get(ext, 'Other' if ext else 'No extension')}; "
                   f"compression estimate: {int(sc.ratios[ext] * 100)}%" if ext in sc.ratios else
                   f"Category: {sc.ext_cat.get(ext, 'Other' if ext else 'No extension')}; not a compression candidate")
    return "\n".join(out)


# --------------------------------------------------------------------------- scanner
class Scanner:
    # dir record indices
    P, PARENT, DEPTH, OWN, OWNF, NEW, TOT, TOTF, NEWT, DMT = range(10)

    def __init__(self, cfg: dict, rules: dict, variables: dict, use_atime: bool, quiet: bool):
        self.cfg = cfg
        self.t = cfg.get("thresholds", {})
        self.vars = variables
        self.use_atime = use_atime
        self.quiet = quiet
        self.now = time.time()

        self.dir_rules = [DirRule(r, variables) for r in rules.get("dir_rules", [])]
        self.rules_by_key = defaultdict(list)
        self.rules_wild = []
        for r in self.dir_rules:
            if r.keys is None:
                self.rules_wild.append(r)
            else:
                for k in r.keys:
                    self.rules_by_key[k].append(r)
        self.file_rules = [FileRule(r, variables) for r in rules.get("file_rules", [])]
        self.file_rules_by_ext = defaultdict(list)
        self.file_rules_by_name = []
        for r in self.file_rules:
            for e in r.exts:
                self.file_rules_by_ext[e].append(r)
            if r.name_res:
                self.file_rules_by_name.append(r)
        self.file_rules_inside = [r for r in self.file_rules if r.inside]

        self.protected = [glob_re(expand(p, variables)) for p in cfg.get("protected", [])]
        self.skip = [glob_re(expand(p, variables)) for p in cfg.get("skip", [])]
        self.advice_exclude = [glob_re(expand(p, variables)) for p in cfg.get("advice_exclude", [])]
        self.dup_exclude = [glob_re(expand(p, variables)) for p in cfg.get("duplicates_exclude", [])]
        self.sync_roots = sorted({variables[k] for k in ("onedrive", "onedrivepersonal")} |
                                 {expand(p, variables) for p in cfg.get("cloud_sync_roots", [])})

        cats = rules.get("categories", {})
        self.ext_cat = {e.lower(): c for c, exts in cats.items() for e in exts}
        self.ratios = {k.lower(): float(v) for k, v in rules.get("compression_ratios", {}).items()}

        # results
        self.D: list[list] = []
        self.anchors: list[tuple[DirRule, int]] = []
        self.anchor_files: dict[int, list] = defaultdict(list)     # children-mode: files directly inside
        self.cat = defaultdict(lambda: [0, 0])
        self.ext = defaultdict(lambda: [0, 0])
        self.age = defaultdict(lambda: [0, 0])
        self.large = []                                             # min-heap (size, path, used, cat)
        self.groups = {}                                            # (rule, dir) -> group
        self.compress = []                                          # heap by est savings
        self.compress_tot = [0, 0, 0]                               # files, bytes, savings
        self.archive = []
        self.archive_tot = [0, 0]
        self.offload = []
        self.offload_tot = [0, 0]
        self.by_size = defaultdict(list)
        self.progress = {"phase": "scan"}   # read by the PC TidyUp app for live progress
        self.cancel = None                   # threading.Event set by the app to stop a scan
        self.stats = dict(dirs=0, files=0, bytes=0, cloud_files=0, cloud_bytes=0,
                          denied=0, errors=0, links=0, skipped=0)
        self.denied_samples = []

    # ---- matching
    def _match_dir(self, npath: str, lname: str, protected: bool):
        found = None
        for r in self.rules_by_key.get(lname, ()):
            if (r.system or not protected) and r.matches(npath):
                found = r; break
        if found is None:
            for r in self.rules_wild:
                if (r.system or not protected) and r.matches(npath):
                    found = r; break
        return found

    def _is_protected(self, npath: str) -> bool:
        probe = npath + "/"
        return any(r.match(probe) or r.match(npath) for r in self.protected)

    def _in_sync_root(self, npath: str) -> bool:
        return any(npath == r or npath.startswith(r + "/") for r in self.sync_roots if r)

    @staticmethod
    def _bucket(days: float) -> str:
        if days < 30: return "0-30 days"
        if days < 90: return "1-3 months"
        if days < 365: return "3-12 months"
        if days < 730: return "1-2 years"
        return "2+ years"

    @staticmethod
    def _push(heap, cap, item):
        if len(heap) < cap:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)

    # ---- walk
    def _tick(self, path):
        self.progress.update(phase="scan", dirs=self.stats["dirs"], files=self.stats["files"],
                             bytes=self.stats["bytes"], cloud_bytes=self.stats["cloud_bytes"], path=path)
        if self.cancel is not None and self.cancel.is_set():
            raise ScanCancelled()

    def scan(self, roots: list[str]):
        last = last_tick = time.time()
        for root in roots:
            root = os.path.abspath(root)
            if len(root) == 2 and root[1] == ":":
                root += "\\"
            # stack items: (path, parent_idx, depth, inherited_protected, inherited_sync, anchor_stack)
            stack = [(root, -1, 0, False, False, (), 0.0)]
            while stack:
                path, pidx, depth, prot_in, sync_in, anchors, dmt = stack.pop()
                idx = len(self.D)
                self.D.append([path, pidx, depth, 0, 0, 0.0, 0, 0, 0.0, dmt])
                self.stats["dirs"] += 1
                npath = norm(path)
                lname = npath.rsplit("/", 1)[-1]
                protected = prot_in or self._is_protected(npath)
                in_sync = sync_in or self._in_sync_root(npath)
                rule = self._match_dir(npath, lname, protected)
                if rule:
                    self.anchors.append((rule, idx))
                    anchors = anchors + ((rule, idx),)
                inner = anchors[-1] if anchors else None
                in_anchor = bool(anchors)

                now = time.time()
                if now - last_tick > 0.5:
                    last_tick = now
                    self._tick(path)
                if not self.quiet and now - last > 5:
                    last = now
                    print(f"  ... {self.stats['dirs']:,} folders, {self.stats['files']:,} files, "
                          f"{human(self.stats['bytes'])}  {path[:90]}", file=sys.stderr, flush=True)
                try:
                    it = os.scandir(longpath(path))
                except PermissionError:
                    self.stats["denied"] += 1
                    if len(self.denied_samples) < 25:
                        self.denied_samples.append(path)
                    continue
                except OSError:
                    self.stats["errors"] += 1
                    continue
                rec = self.D[idx]
                subdirs = []
                with it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.is_symlink() or entry_is_junction(e, longpath(join(path, e.name))):
                                    self.stats["links"] += 1
                                    continue
                                child = join(path, e.name)
                                if self.skip and any(r.match(norm(child)) for r in self.skip):
                                    self.stats["skipped"] += 1
                                    continue
                                try:
                                    dm = e.stat(follow_symlinks=False).st_mtime
                                except OSError:
                                    dm = 0.0
                                subdirs.append((child, dm))
                                continue
                            if e.is_symlink():
                                self.stats["links"] += 1
                                continue
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            self.stats["errors"] += 1
                            continue
                        self._file(e.name, st, path, npath, idx, rec, protected, in_sync, in_anchor, inner)
                for child, dm in reversed(subdirs):
                    stack.append((child, idx, depth + 1, protected, in_sync, anchors, dm))
        self._totals()

    def _file(self, name, st, dpath, dnpath, didx, rec, protected, in_sync, in_anchor, inner):
        attrs = getattr(st, "st_file_attributes", 0)
        size = st.st_size
        if attrs & CLOUD_ONLY:
            self.stats["cloud_files"] += 1
            self.stats["cloud_bytes"] += size
            return
        mtime = st.st_mtime
        used = max(mtime, st.st_atime) if self.use_atime else mtime
        used_days = max(0.0, (self.now - used) / DAY)
        mod_days = max(0.0, (self.now - mtime) / DAY)
        lname = name.lower()
        dot = lname.rfind(".")
        ext = lname[dot:] if dot > 0 else ""
        cat = self.ext_cat.get(ext, "Other" if ext else "No extension")

        self.stats["files"] += 1
        self.stats["bytes"] += size
        rec[self.OWN] += size
        rec[self.OWNF] += 1
        if mtime > rec[self.NEW]:
            rec[self.NEW] = mtime
        c = self.cat[cat]; c[0] += size; c[1] += 1
        x = self.ext[ext or "(none)"]; x[0] += size; x[1] += 1
        a = self.age[self._bucket(used_days)]; a[0] += size; a[1] += 1
        fpath = join(dpath, name)
        self._push(self.large, self.t.get("large_file_top_n", 100), (size, fpath, used, cat))

        if in_anchor:
            rule, aidx = inner
            if rule.mode == "children" and aidx == didx:
                self.anchor_files[aidx].append((fpath, size, mtime))
            if not self.file_rules_inside:
                return

        # file rules (logs, temp, dumps, installers, ...)
        cands = list(self.file_rules_by_ext.get(ext, ()))
        for r in self.file_rules_by_name:
            if r not in cands and any(rx.match(lname) for rx in r.name_res):
                cands.append(r)
        matched = None
        for r in cands:
            if in_anchor and not r.inside:
                continue
            if protected and not r.system:
                continue
            if size < r.min_size:
                continue
            age = mod_days if r.age_basis == "modified" else used_days
            if age < r.min_age:
                continue
            if r.path_res and not any(rx.match(dnpath + "/" + lname) for rx in r.path_res):
                continue
            matched = r
            break
        if matched:
            key = (matched.id, dpath)
            g = self.groups.get(key)
            if g is None:
                g = self.groups[key] = dict(rule=matched, dir=dpath, files=0, bytes=0,
                                            oldest=mtime, newest=mtime, samples=[])
            g["files"] += 1; g["bytes"] += size
            g["oldest"] = min(g["oldest"], mtime); g["newest"] = max(g["newest"], mtime)
            self._push(g["samples"], 5, (size, name))
            return
        if protected or in_anchor:
            return

        # compress / archive / offload (not for app-internal data)
        t = self.t
        if self.advice_exclude and size >= MB * min(t.get("compress_min_mb", 20), t.get("archive_min_mb", 100),
                                                   t.get("offload_min_mb", 20), t.get("duplicate_min_mb", 10)):
            probe = dnpath + "/"
            if any(r.match(probe) or r.match(dnpath) for r in self.advice_exclude):
                return
        ratio = self.ratios.get(ext)
        if (ratio and size >= t.get("compress_min_mb", 20) * MB and used_days >= t.get("compress_min_age_days", 90)
                and not attrs & A_COMPRESSED):
            est = int(size * ratio)
            self.compress_tot[0] += 1; self.compress_tot[1] += size; self.compress_tot[2] += est
            self._push(self.compress, t.get("max_rows", 500), (est, size, fpath, used, ext, in_sync))
        elif in_sync or attrs & (A_PINNED | A_UNPINNED):
            if size >= t.get("offload_min_mb", 20) * MB and used_days >= t.get("offload_min_age_days", 180):
                self.offload_tot[0] += 1; self.offload_tot[1] += size
                self._push(self.offload, t.get("max_rows", 500), (size, fpath, used, cat, bool(attrs & A_PINNED)))
        elif size >= t.get("archive_min_mb", 100) * MB and used_days >= t.get("archive_min_age_days", 365):
            self.archive_tot[0] += 1; self.archive_tot[1] += size
            self._push(self.archive, t.get("max_rows", 500), (size, fpath, used, cat))

        if size >= t.get("duplicate_min_mb", 10) * MB:
            probe = dnpath + "/"
            if not any(r.match(probe) or r.match(dnpath) for r in self.dup_exclude):
                self.by_size[size].append((fpath, st.st_atime_ns, st.st_mtime_ns))

    def _totals(self):
        D = self.D
        for rec in reversed(D):
            rec[self.TOT] += rec[self.OWN]
            rec[self.TOTF] += rec[self.OWNF]
            if rec[self.NEW] > rec[self.NEWT]:
                rec[self.NEWT] = rec[self.NEW]
            p = rec[self.PARENT]
            if p >= 0:
                pr = D[p]
                pr[self.TOT] += rec[self.TOT]
                pr[self.TOTF] += rec[self.TOTF]
                if rec[self.NEWT] > pr[self.NEWT]:
                    pr[self.NEWT] = rec[self.NEWT]

    # ---- duplicates
    def duplicates(self, budget_s: float):
        full_max = self.t.get("duplicate_full_hash_max_mb", 512) * MB
        start = time.time()
        chunk = 1 * MB
        groups, timed_out = [], False

        def read_hash(path, size, full):
            h = hashlib.blake2b(digest_size=16)
            lp = longpath(path)
            with open(lp, "rb") as f:
                if full:
                    while True:
                        b = f.read(4 * MB)
                        if not b: break
                        h.update(b)
                else:
                    for off in (0, size // 2, max(0, size - chunk)):
                        f.seek(off); h.update(f.read(chunk))
            return h.hexdigest()

        sizes = sorted((s for s, l in self.by_size.items() if len(l) > 1), reverse=True)
        for n, size in enumerate(sizes):
            self.progress.update(phase="duplicates", done=n, total=len(sizes))
            if self.cancel is not None and self.cancel.is_set():
                raise ScanCancelled()
            if time.time() - start > budget_s:
                timed_out = True
                break
            full = size <= full_max
            by_hash = defaultdict(list)
            for path, at_ns, mt_ns in self.by_size[size]:
                try:
                    hsh = read_hash(path, size, full)
                except OSError:
                    continue
                finally:
                    try:  # reading must not make the file look "recently used"
                        os.utime(longpath(path), ns=(at_ns, mt_ns))
                    except OSError:
                        pass
                by_hash[hsh].append(path)
            for hsh, paths in by_hash.items():
                if len(paths) > 1:
                    groups.append(dict(size=size, files=sorted(paths), wasted=size * (len(paths) - 1),
                                       verified=full))
        groups.sort(key=lambda g: -g["wasted"])
        return groups, timed_out

    # ---- candidates from directory rules
    def dir_candidates(self):
        D = self.D
        out = []
        children = defaultdict(list)
        anchor_ids = {i for _, i in self.anchors if _.mode == "children"}
        if anchor_ids:
            for i, rec in enumerate(D):
                if rec[self.PARENT] in anchor_ids:
                    children[rec[self.PARENT]].append(i)
        for rule, idx in self.anchors:
            rec = D[idx]
            if rule.mode == "children":
                items = []
                for ci in children.get(idx, ()):
                    c = D[ci]
                    newest = max(c[self.NEWT], c[self.DMT])
                    age = (self.now - newest) / DAY if newest else 99999
                    if age >= rule.min_age:
                        items.append((c[self.TOT], c[self.P], c[self.TOTF], newest))
                for fpath, size, mtime in self.anchor_files.get(idx, ()):
                    if (self.now - mtime) / DAY >= rule.min_age:
                        items.append((size, fpath, 1, mtime))
                b = sum(i[0] for i in items)
                if not items or (b == 0 and rule.action != "info"):
                    continue
                newest = max((i[3] for i in items), default=0)
                items.sort(reverse=True)
                out.append(dict(rule=rule, idx=idx, path=rec[self.P], bytes=b,
                                files=sum(i[2] for i in items), newest=newest, items=items))
            else:
                newest = max(rec[self.NEWT], rec[self.DMT])
                age = (self.now - newest) / DAY if newest else 99999
                if age < rule.min_age or (rec[self.TOT] == 0):
                    continue
                out.append(dict(rule=rule, idx=idx, path=rec[self.P], bytes=rec[self.TOT],
                                files=rec[self.TOTF], newest=newest, items=None))
        # keep the outermost reported candidate when rules nest (node_modules inside node_modules ...)
        reported, final = set(), []
        for c in sorted(out, key=lambda c: D[c["idx"]][self.DEPTH]):
            p, nested = D[c["idx"]][self.PARENT], False
            while p >= 0:
                if p in reported:
                    nested = True; break
                p = D[p][self.PARENT]
            if nested:
                continue
            reported.add(c["idx"])
            final.append(c)
        return final


# --------------------------------------------------------------------------- report data
def build_data(sc: Scanner, roots, dups, dup_timeout, duration, cfg, previous):
    now = sc.now
    t = sc.t
    D = sc.D

    def days(ts):
        return int((now - ts) / DAY) if ts else None

    candidates = []
    dir_cands = sc.dir_candidates()
    covered = set()
    for c in dir_cands:  # what a folder rule actually reports: the folder, or only its old entries
        covered.update([c["path"].lower()] if c["items"] is None else (i[1].lower() for i in c["items"]))

    def is_covered(p):
        p = p.lower()
        while True:
            if p in covered:
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent

    for c in dir_cands:
        r = c["rule"]
        candidates.append(dict(
            rule=r.id, label=r.label, action=r.action, kind="folder" if r.mode == "whole" else "folder-contents",
            path=c["path"], bytes=c["bytes"], files=c["files"], newest=ts_iso(c["newest"]),
            age_days=days(c["newest"]), min_age=r.min_age, reason=r.reason, how=r.how, ps=r.ps,
            check_running=r.check_running,
            items=[[i[1], i[0]] for i in (c["items"] or [])[:5000]],
            items_total=len(c["items"] or [])))
    for (rid, d), g in sc.groups.items():
        r = g["rule"]
        if r.inside and is_covered(d):
            continue
        candidates.append(dict(
            rule=r.id, label=r.label, action=r.action, kind="files", path=d, bytes=g["bytes"],
            files=g["files"], newest=ts_iso(g["newest"]), oldest=ts_iso(g["oldest"]),
            age_days=days(g["newest"]), min_age=r.min_age, reason=r.reason, how=r.how,
            samples=[n for _, n in sorted(g["samples"], reverse=True)],
            ps_patterns=r.ps_patterns, age_basis=r.age_basis))
    min_c = t.get("min_candidate_mb", 1) * MB
    hidden = [c for c in candidates if c["bytes"] < min_c and c["action"] != "info"]
    candidates = [c for c in candidates if c["bytes"] >= min_c or c["action"] == "info"]
    candidates.sort(key=lambda c: -c["bytes"])
    for i, c in enumerate(candidates):
        c["id"] = i + 1

    totals = {a: 0 for a in ACTIONS}
    counts = {a: 0 for a in ACTIONS}
    for c in candidates:
        totals[c["action"]] += c["bytes"]
        counts[c["action"]] += 1

    tree_min = t.get("tree_min_mb", 250) * MB
    tree_depth = t.get("tree_max_depth", 8)
    tree, keep = [], {}
    for i, rec in enumerate(D):
        if rec[Scanner.TOT] >= tree_min and rec[Scanner.DEPTH] <= tree_depth:
            pid = keep.get(rec[Scanner.PARENT], -1)
            keep[i] = len(tree)
            name = rec[Scanner.P] if pid < 0 else rec[Scanner.P].rsplit("\\", 1)[-1]
            tree.append([len(tree), pid, name, rec[Scanner.P], rec[Scanner.TOT], rec[Scanner.TOTF],
                         ts_iso(rec[Scanner.NEWT]), rec[Scanner.OWN], rec[Scanner.DEPTH]])

    disks, seen = [], set()
    for r in roots:
        drive = (os.path.splitdrive(os.path.abspath(r))[0] or r) + "\\"
        if drive.lower() in seen:
            continue
        seen.add(drive.lower())
        try:
            du = shutil.disk_usage(drive)
            disks.append(dict(root=drive, total=du.total, used=du.used, free=du.free))
        except OSError:
            pass

    snapshot_dirs = {D[i][Scanner.P]: D[i][Scanner.TOT] for i in range(len(D))
                     if D[i][Scanner.DEPTH] <= 4 and D[i][Scanner.TOT] >= 100 * MB}

    history = None
    if previous:
        deltas = []
        prev_dirs = previous.get("dirs", {})
        for p, b in snapshot_dirs.items():
            before = prev_dirs.get(p, 0)
            if abs(b - before) >= 200 * MB:
                deltas.append(dict(path=p, before=before, after=b, delta=b - before))
        for p, before in prev_dirs.items():
            if p not in snapshot_dirs and before >= 200 * MB:
                deltas.append(dict(path=p, before=before, after=0, delta=-before))
        deltas.sort(key=lambda d: -abs(d["delta"]))
        # drop parent entries that only echo one child
        history = dict(previous=previous.get("generated"), prev_free=previous.get("free"),
                       prev_scanned=previous.get("scanned_bytes"), prev_totals=previous.get("totals"),
                       deltas=deltas[:60])

    comp = sorted(sc.compress, reverse=True)
    arch = sorted(sc.archive, reverse=True)
    off = sorted(sc.offload, reverse=True)
    large = sorted(sc.large, reverse=True)
    data = dict(
        meta=dict(tool="PC TidyUp", version=VERSION, generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                  host=platform.node(), user=os.environ.get("USERNAME", ""), roots=roots,
                  duration_s=round(duration), atime_used=sc.use_atime,
                  thresholds=t, elevated=is_admin(), denied_samples=sc.denied_samples),
        disk=disks,
        scan=sc.stats,
        categories=sorted(([k, v[0], v[1]] for k, v in sc.cat.items()), key=lambda x: -x[1]),
        extensions=sorted(([k, v[0], v[1]] for k, v in sc.ext.items()), key=lambda x: -x[1])[:40],
        age=[[b, sc.age[b][0], sc.age[b][1]] for b in
             ("0-30 days", "1-3 months", "3-12 months", "1-2 years", "2+ years")],
        tree=tree,
        candidates=candidates,
        totals=totals, counts=counts,
        hidden_small=dict(count=len(hidden), bytes=sum(c["bytes"] for c in hidden)),
        compress=dict(files=sc.compress_tot[0], bytes=sc.compress_tot[1], savings=sc.compress_tot[2],
                      items=[dict(path=p, bytes=s, est=e, ext=x, used=ts_iso(u), used_days=days(u), cloud=cl)
                             for e, s, p, u, x, cl in comp]),
        archive=dict(files=sc.archive_tot[0], bytes=sc.archive_tot[1],
                     items=[dict(path=p, bytes=s, cat=c, used=ts_iso(u), used_days=days(u)) for s, p, u, c in arch]),
        offload=dict(files=sc.offload_tot[0], bytes=sc.offload_tot[1],
                     items=[dict(path=p, bytes=s, cat=c, used=ts_iso(u), used_days=days(u), pinned=pn)
                            for s, p, u, c, pn in off]),
        duplicates=dict(groups=dups[:300], wasted=sum(g["wasted"] for g in dups), groups_total=len(dups),
                        timed_out=dup_timeout, skipped=dups is None),
        large=[dict(path=p, bytes=s, cat=c, used=ts_iso(u), used_days=days(u)) for s, p, u, c in large],
        history=history,
    )
    snapshot = dict(generated=data["meta"]["generated"], roots=roots,
                    free=sum(d["free"] for d in disks), scanned_bytes=sc.stats["bytes"],
                    totals=dict(totals, compress=sc.compress_tot[2], archive=sc.archive_tot[1],
                                offload=sc.offload_tot[1], duplicates=data["duplicates"]["wasted"]),
                    categories={k: v[0] for k, v in sc.cat.items()}, dirs=snapshot_dirs)
    return data, snapshot


PRIORITY = {
    1: ("Do now", "Big win, low risk - act right away"),
    2: ("Quick check", "Glance at it, then act"),
    3: ("Decide", "Needs your judgement before acting"),
    4: ("Optional", "Small gain - whenever convenient"),
}


def prioritize(data: dict, cfg: dict):
    """Adds a priority (1-4) to every recommendation, an action plan and a disk health verdict."""
    pc = {"safe_p1_mb": 500, "safe_p2_mb": 50, "likely_p2_mb": 100, "review_p3_mb": 1024, "info_p3_mb": 5120,
          "offload_p1_mb": 500, "compress_p2_mb": 200, "archive_p3_mb": 1024, "duplicates_p3_mb": 500,
          "critical_free_pct": 10, "critical_free_gb": 15, "warning_free_pct": 20}
    pc.update(cfg.get("priority", {}))
    M = MB
    growth = {d["path"].lower(): d["delta"] for d in (data.get("history") or {}).get("deltas", []) if d["delta"] > 0}

    def cand_prio(c):
        b, a = c["bytes"], c["action"]
        if a == "safe":
            return 1 if b >= pc["safe_p1_mb"] * M else 2 if b >= pc["safe_p2_mb"] * M else 4
        if a == "likely":
            return 2 if b >= pc["likely_p2_mb"] * M else 4
        if a == "review":
            return 3 if b >= pc["review_p3_mb"] * M else 4
        return 3 if b >= pc["info_p3_mb"] * M else 4

    plan = []
    for c in data["candidates"]:
        c["prio"] = cand_prio(c)
        g = growth.get(c["path"].lower())
        if g:
            c["growing"] = g
        plan.append(dict(kind="candidate", key=f"c:{c['id']}", prio=c["prio"], label=c["label"], path=c["path"],
                         bytes=c["bytes"], action=c["action"], growing=g))
    agg = [
        ("offload", "Make stale OneDrive files online-only", data["offload"]["bytes"], data["offload"]["files"],
         1 if data["offload"]["bytes"] >= pc["offload_p1_mb"] * M else 4),
        ("compress", "Compress unused text-like files", data["compress"]["savings"], data["compress"]["files"],
         2 if data["compress"]["savings"] >= pc["compress_p2_mb"] * M else 4),
        ("archive", "Archive large unused files to OneDrive", data["archive"]["bytes"], data["archive"]["files"],
         3 if data["archive"]["bytes"] >= pc["archive_p3_mb"] * M else 4),
        ("dups", "Remove duplicate copies", data["duplicates"]["wasted"], data["duplicates"]["groups_total"],
         3 if data["duplicates"]["wasted"] >= pc["duplicates_p3_mb"] * M else 4),
    ]
    for kind, label, b, n, prio in agg:
        if b > 0:
            plan.append(dict(kind=kind, key=None, prio=prio, label=label, bytes=b, count=n, tab=kind))
    plan.sort(key=lambda p: (p["prio"], -p["bytes"]))
    data["plan"] = plan
    data["priority_labels"] = {str(k): v for k, v in PRIORITY.items()}
    data["priority_totals"] = {str(k): sum(p["bytes"] for p in plan if p["prio"] == k and p.get("action") != "info")
                               for k in PRIORITY}

    order = ["good", "warning", "critical"]
    worst = "good"
    for d in data["disk"]:
        pct = 100 * d["free"] / d["total"] if d["total"] else 100
        lvl = ("critical" if pct < pc["critical_free_pct"] or d["free"] < pc["critical_free_gb"] * 1024 * M
               else "warning" if pct < pc["warning_free_pct"] else "good")
        d["health"] = lvl
        d["free_pct"] = round(pct, 1)
        if order.index(lvl) > order.index(worst):
            worst = lvl
    data["health"] = worst


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- outputs
ACTION_TEXT = {
    "safe": "Low risk (caches and temp that apps rebuild)",
    "likely": "Likely obsolete - glance, then delete",
    "review": "Needs your decision",
    "info": "Managed by Windows / an app - use the tool named",
}


def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def write_markdown(data: dict, path: Path):
    m, L = data["meta"], []
    tot = data["totals"]
    L.append(f"# PC TidyUp storage report - {m['host']}")
    hl = {"good": "OK", "warning": "WARNING - getting full", "critical": "CRITICAL - almost full"}.get(data.get("health"), "")
    L.append(f"**Disk status: {hl}**\n")
    L.append(f"_Generated {m['generated']} - roots: {', '.join(m['roots'])} - scan took {m['duration_s']} s - "
             f"PC TidyUp {m['version']}_\n")
    L.append("## Summary\n")
    L.append("| Drive | Size | Used | Free |\n|---|---:|---:|---:|")
    for d in data["disk"]:
        L.append(f"| {d['root']} | {human(d['total'])} | {human(d['used'])} ({d['used']/d['total']:.0%}) | "
                 f"**{human(d['free'])}** |")
    s = data["scan"]
    L.append(f"\nScanned **{s['files']:,} local files / {human(s['bytes'])}** in {s['dirs']:,} folders. "
             f"{s['cloud_files']:,} OneDrive/SharePoint files ({human(s['cloud_bytes'])}) are online-only "
             f"and use no local space. {s['denied']:,} folders could not be read"
             f"{' (run elevated to include them)' if not m['elevated'] else ''}.\n")
    L.append("### Potential gains\n")
    L.append("| Recommendation | Space | Items |\n|---|---:|---:|")
    L.append(f"| Delete - low risk (caches, temp) | **{human(tot['safe'])}** | {data['counts']['safe']} |")
    L.append(f"| Delete - likely obsolete (old logs, dumps, installers, stale build output) | **{human(tot['likely'])}** | {data['counts']['likely']} |")
    L.append(f"| Delete or keep - your decision | {human(tot['review'])} | {data['counts']['review']} |")
    L.append(f"| Compress (estimated saving) | {human(data['compress']['savings'])} of {human(data['compress']['bytes'])} | {data['compress']['files']} |")
    L.append(f"| OneDrive \"Free up space\" (keep in cloud only) | {human(data['offload']['bytes'])} | {data['offload']['files']} |")
    L.append(f"| Archive off this disk (large, unused 1y+) | {human(data['archive']['bytes'])} | {data['archive']['files']} |")
    L.append(f"| Duplicate copies | {human(data['duplicates']['wasted'])} | {data['duplicates']['groups_total']} groups |")
    L.append(f"| Windows/app-managed (info) | {human(tot['info'])} | {data['counts']['info']} |")

    if data.get("plan"):
        L.append("\n## Action plan (by priority)\n")
        for k in (1, 2, 3):
            rows = [p for p in data["plan"] if p["prio"] == k and p.get("action") != "info"]
            if not rows:
                continue
            name, desc = PRIORITY[k]
            L.append(f"**[P{k}] {name}** - {desc} ({human(sum(p['bytes'] for p in rows))})\n")
            for p in rows[:12]:
                where = f" - `{md_escape(p['path'])}`" if p.get("path") else f" - {p.get('count', 0)} items"
                grow = f" (grew {human(p['growing'])} since last run)" if p.get("growing") else ""
                L.append(f"- {md_escape(p['label'])}: **{human(p['bytes'])}**{where}{grow}")
            if len(rows) > 12:
                L.append(f"- ... {len(rows) - 12} more")
            L.append("")

    h = data.get("history")
    if h:
        L.append(f"\n## Changes since last run ({h['previous']})\n")
        if h.get("prev_free") is not None:
            free = sum(d["free"] for d in data["disk"])
            L.append(f"Free space: {human(h['prev_free'])} -> **{human(free)}** ({'+' if free >= h['prev_free'] else '-'}{human(abs(free - h['prev_free']))})\n")
        if h["deltas"]:
            L.append("| Folder | Before | Now | Change |\n|---|---:|---:|---:|")
            for d in h["deltas"][:20]:
                L.append(f"| `{md_escape(d['path'])}` | {human(d['before'])} | {human(d['after'])} | "
                         f"{'+' if d['delta'] > 0 else '-'}{human(abs(d['delta']))} |")

    def cand_table(action, limit=40):
        rows = [c for c in data["candidates"] if c["action"] == action]
        if not rows:
            L.append("_Nothing found._\n"); return
        L.append("| # | Priority | What | Where | Size | Items | Last change | How |\n|---:|---|---|---|---:|---:|---|---|")
        for c in rows[:limit]:
            L.append(f"| {c['id']} | P{c.get('prio', 4)} | {md_escape(c['label'])} | `{md_escape(c['path'])}` | {human(c['bytes'])} | "
                     f"{c['files']:,} | {c.get('newest') or '-'} | {md_escape(c['how'] or c['reason'])} |")
        if len(rows) > limit:
            L.append(f"\n_+{len(rows) - limit} more in the HTML report._")
        L.append("")

    L.append("\n## 1. Low risk - caches & temporary files (rebuilt automatically; close apps first)\n")
    cand_table("safe")
    L.append("## 2. Likely obsolete - logs, dumps, old installers, stale build output\n")
    cand_table("likely")
    L.append("## 3. Your decision\n")
    cand_table("review")

    L.append("## 4. Compression candidates\n")
    c = data["compress"]
    L.append(f"{c['files']} compressible files ({human(c['bytes'])}) unused for "
             f"{m['thresholds'].get('compress_min_age_days', 90)}+ days; estimated saving **{human(c['savings'])}**. "
             "Zip/7z them (and delete the original) or use transparent NTFS compression "
             "(`compact /c /s:\"<folder>\"`, not for OneDrive folders).\n")
    if c["items"]:
        L.append("| File | Size | Est. saving | Last used |\n|---|---:|---:|---|")
        for i in c["items"][:30]:
            L.append(f"| `{md_escape(i['path'])}` | {human(i['bytes'])} | {human(i['est'])} | {i['used']} |")
    L.append("\n## 5. OneDrive / SharePoint - free up local space\n")
    o = data["offload"]
    L.append(f"{o['files']} synced files ({human(o['bytes'])}) are stored locally but unused for "
             f"{m['thresholds'].get('offload_min_age_days', 180)}+ days. Right-click > **Free up space** "
             "(or `attrib +U -P \"<path>\"`) keeps them in the cloud with no data loss.\n")
    if o["items"]:
        L.append("| File | Size | Last used |\n|---|---:|---|")
        for i in o["items"][:30]:
            L.append(f"| `{md_escape(i['path'])}` | {human(i['bytes'])} | {i['used']} |")
    L.append("\n## 6. Archive candidates\n")
    a = data["archive"]
    L.append(f"{a['files']} large files ({human(a['bytes'])}) not used for "
             f"{m['thresholds'].get('archive_min_age_days', 365)}+ days. Move to OneDrive/SharePoint "
             "(then Free up space), an external disk or a team archive - or delete.\n")
    if a["items"]:
        L.append("| File | Type | Size | Last used |\n|---|---|---:|---|")
        for i in a["items"][:30]:
            L.append(f"| `{md_escape(i['path'])}` | {i['cat']} | {human(i['bytes'])} | {i['used']} |")
    L.append("\n## 7. Duplicates\n")
    du = data["duplicates"]
    if du.get("skipped"):
        L.append("_Duplicate detection was skipped._")
    else:
        L.append(f"{du['groups_total']} groups of identical files; {human(du['wasted'])} held by extra copies."
                 + (" (time budget reached - list incomplete)" if du["timed_out"] else "") + "\n")
        for g in du["groups"][:15]:
            L.append(f"- **{human(g['size'])} x {len(g['files'])}**" + ("" if g["verified"] else " _(sampled hash)_"))
            for f in g["files"]:
                L.append(f"  - `{f}`")
    L.append("\n## 8. Windows / app-managed space (information)\n")
    cand_table("info")
    L.append("## 9. Where the space goes\n")
    L.append("| File type | Size | Files |\n|---|---:|---:|")
    for k, b, n in data["categories"][:20]:
        L.append(f"| {k} | {human(b)} | {n:,} |")
    L.append("\n| Last used | Size | Files |\n|---|---:|---:|")
    for k, b, n in data["age"]:
        L.append(f"| {k} | {human(b)} | {n:,} |")
    L.append("\n**Biggest folders** (2 levels)\n")
    L.append("| Folder | Size | Files |\n|---|---:|---:|")
    tops = [n for n in data["tree"] if n[8] in (1, 2)]
    for n in sorted(tops, key=lambda n: -n[4])[:30]:
        L.append(f"| `{md_escape(n[3])}` | {human(n[4])} | {n[5]:,} |")
    L.append("\n**Largest files**\n")
    L.append("| File | Size | Last used |\n|---|---:|---|")
    for i in data["large"][:25]:
        L.append(f"| `{md_escape(i['path'])}` | {human(i['bytes'])} | {i['used']} |")
    L.append("\n---\nTidyUp (c) 2026 Mehrdad Ghazvinizadeh - free for personal/noncommercial use under the "
             "PolyForm Noncommercial License 1.0.0; commercial use requires the author's written permission.\n")
    L.append("**Disclaimer:** PC TidyUp is used entirely at your own risk. It is provided \"as is\", without any warranty; "
             "to the maximum extent permitted by law, the author and publisher are not liable for any damage, data loss, "
             "data corruption or disruption. Make backups before acting. See DISCLAIMER.md.\n")
    L.append("Generated by PC TidyUp. Nothing was deleted. Use the generated cleanup script "
             "(dry run by default) or act on the items manually. Times are 'last used' "
             f"({'last access or modification' if m['atime_used'] else 'last modification'}).")
    path.write_text("\n".join(L), encoding="utf-8")


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def write_cleanup(data: dict, path: Path):
    m = data["meta"]
    L = [f"<#",
         f"  PC TidyUp cleanup script - generated {m['generated']} on {m['host']} from the PC TidyUp report.",
         f"  PC TidyUp (c) 2026 Mehrdad Ghazvinizadeh - PolyForm Noncommercial License 1.0.0; commercial use requires written permission.",
         f"  DRY RUN by default: it only lists what it would do. Read the report first, then run:",
         f"     .\\{path.name} -Apply                  # low-risk items only (caches, temp)",
         f"     .\\{path.name} -Apply -IncludeLikely   # + old logs, dumps, installers, stale build output",
         f"     .\\{path.name} -Apply -IncludeReview   # + items that need your decision (read them first!)",
         f"     .\\{path.name} -Apply -IncludeOffload  # + make stale OneDrive files online-only",
         f"     .\\{path.name} -Apply -Recycle         # move to the Recycle Bin instead of deleting permanently",
         f"  -Recycle can be undone, but the disk space only comes back once the Recycle Bin is emptied.",
         f"  Close browsers, Teams, VS Code and Visual Studio first so their caches are not locked.",
         f"  System locations (C:\\Windows, ProgramData) need an elevated PowerShell; otherwise they are skipped.",
         f"  Items are re-checked at run time; anything newer than the age limit is kept.",
         f"  DISCLAIMER: you run this script entirely at your own risk and responsibility. It is provided 'as is',",
         f"  without any warranty. To the maximum extent permitted by law the author and publisher are not liable for any",
         f"  damage, data loss, data corruption or disruption. Make backups first. Running it with -Apply means you",
         f"  accept DISCLAIMER.md.",
         f"#>",
         "[CmdletBinding()]",
         "param([switch]$Apply, [switch]$IncludeLikely, [switch]$IncludeReview, [switch]$IncludeOffload, [switch]$Recycle)",
         "Add-Type -AssemblyName Microsoft.VisualBasic",
         "$script:Verb = if ($Recycle) { 'recycled' } else { 'removed' }",
         "function Send-TidyToRecycleBin([string]$Path) {",
         "  try {",
         "    if (Test-Path -LiteralPath $Path -PathType Container) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($Path, 'OnlyErrorDialogs', 'SendToRecycleBin') }",
         "    else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($Path, 'OnlyErrorDialogs', 'SendToRecycleBin') }",
         "  } catch { Write-Warning \"  could not recycle ${Path}: $($_.Exception.Message)\" } }",
         "$ErrorActionPreference = 'Continue'",
         "$script:Freed = [int64]0; $script:Planned = [int64]0",
         "function Get-TidySize([string]$Path) {",
         "  $i = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue",
         "  if (-not $i) { return 0 }",
         "  if ($i.PSIsContainer) { return [int64](Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }",
         "  return [int64]$i.Length }",
         "function Get-TidyNewest([string]$Path) {",
         "  $i = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue",
         "  if (-not $i) { return $null }",
         "  if (-not $i.PSIsContainer) { return $i.LastWriteTime }",
         "  $n = Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1",
         "  if ($n) { return $n.LastWriteTime } else { return $i.LastWriteTime } }",
         "function Remove-TidyPath([string]$Path, [double]$MinAgeDays = 0) {",
         "  if (-not (Test-Path -LiteralPath $Path)) { return }",
         "  if ($MinAgeDays -gt 0) { $n = Get-TidyNewest $Path; if ($n -and $n -gt (Get-Date).AddDays(-$MinAgeDays)) { Write-Host \"  keep (recently changed) $Path\" -ForegroundColor DarkGray; return } }",
         "  $size = Get-TidySize $Path; $script:Planned += $size",
         "  if (-not $Apply) { Write-Host (\"  [dry-run] remove {0,10:N0} MB  {1}\" -f ($size/1MB), $Path); return }",
         "  if ($Recycle) { Send-TidyToRecycleBin $Path } else { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue }",
         "  $left = Get-TidySize $Path; $script:Freed += ($size - $left)",
         "  Write-Host (\"  $script:Verb {0,10:N0} MB  {1}\" -f (($size - $left)/1MB), $Path) }",
         "function Remove-TidyFiles([string]$Dir, [string[]]$Patterns, [double]$MinAgeDays, [string]$Basis) {",
         "  if (-not (Test-Path -LiteralPath $Dir)) { return }",
         "  $cut = (Get-Date).AddDays(-$MinAgeDays)",
         "  $files = Get-ChildItem -LiteralPath $Dir -File -Force -ErrorAction SilentlyContinue | Where-Object {",
         "    $n = $_.Name; $hit = $false; foreach ($p in $Patterns) { if ($n -like $p) { $hit = $true; break } }",
         "    $t = if ($Basis -eq 'used') { @($_.LastWriteTime, $_.LastAccessTime) | Sort-Object -Descending | Select-Object -First 1 } else { $_.LastWriteTime }",
         "    $hit -and $t -lt $cut }",
         "  if (-not $files) { return }",
         "  $size = [int64]($files | Measure-Object Length -Sum).Sum; $script:Planned += $size",
         "  if (-not $Apply) { Write-Host (\"  [dry-run] remove {0,10:N0} MB  {1} file(s) in {2}\" -f ($size/1MB), @($files).Count, $Dir); return }",
         "  $freed = [int64]0; foreach ($f in $files) { try { if ($Recycle) { Send-TidyToRecycleBin $f.FullName } else { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop }; if (-not (Test-Path -LiteralPath $f.FullName)) { $freed += $f.Length } } catch {} }",
         "  $script:Freed += $freed; Write-Host (\"  $script:Verb {0,10:N0} MB  in {1}\" -f ($freed/1MB), $Dir) }",
         "function Invoke-TidyCommand([string]$Label, [scriptblock]$Command) {",
         "  if (-not $Apply) { Write-Host \"  [dry-run] run: $Label\"; return }",
         "  Write-Host \"  run: $Label\"; try { & $Command } catch { Write-Warning $_ } }",
         "function Set-TidyOnlineOnly([string]$Path) {",
         "  if (-not (Test-Path -LiteralPath $Path)) { return }",
         "  $size = (Get-Item -LiteralPath $Path -Force).Length; $script:Planned += $size",
         "  if (-not $Apply) { Write-Host (\"  [dry-run] online-only {0,10:N0} MB  {1}\" -f ($size/1MB), $Path); return }",
         "  attrib.exe +U -P \"$Path\" | Out-Null; $script:Freed += $size; Write-Host (\"  online-only {0,10:N0} MB  {1}\" -f ($size/1MB), $Path) }",
         ""]

    def emit(c, indent):
        out = [f"{indent}# #{c['id']} {c['label']} - {human(c['bytes'])} - {c['reason']}".rstrip()]
        if c.get("ps") and c["rule"] == "recycle-bin":
            out.append(f"{indent}Invoke-TidyCommand {ps_quote(c['ps'])} {{ {c['ps']} }}   # always permanent")
        elif c.get("ps"):     # tool commands delete permanently, so -Recycle recycles the folder instead
            out.append(f"{indent}if ($Recycle) {{ Remove-TidyPath {ps_quote(c['path'])} {int(c['min_age'])} }} "
                       f"else {{ Invoke-TidyCommand {ps_quote(c['ps'])} {{ {c['ps']} }} }}")
        elif c["kind"] == "files":
            pats = ",".join(ps_quote(p) for p in c["ps_patterns"])
            out.append(f"{indent}Remove-TidyFiles {ps_quote(c['path'])} @({pats}) {int(c['min_age'])} "
                       f"{ps_quote(c.get('age_basis', 'used'))}")
        elif c["kind"] == "folder-contents":
            for p, _ in c["items"]:
                out.append(f"{indent}Remove-TidyPath {ps_quote(p)} {int(c['min_age'])}")
            if c["items_total"] > len(c["items"]):
                out.append(f"{indent}# ... {c['items_total'] - len(c['items'])} more entries not listed")
        else:
            out.append(f"{indent}Remove-TidyPath {ps_quote(c['path'])} {int(c['min_age'])}")
        return out

    sections = [("safe", "LOW RISK - caches and temporary files (close the apps first)", None),
                ("likely", "LIKELY OBSOLETE", "$IncludeLikely"),
                ("review", "YOUR DECISION - read the report before enabling", "$IncludeReview")]
    for action, title, flag in sections:
        rows = [c for c in data["candidates"] if c["action"] == action]
        L.append(f"# {'=' * 70}\n# {title}  ({len(rows)} items, {human(sum(c['bytes'] for c in rows))})\n# {'=' * 70}")
        if flag:
            L.append(f"if ({flag}) {{")
        L.append(f"Write-Host '--- {title}' -ForegroundColor Cyan")
        for c in rows:
            L.extend(emit(c, "  " if flag else ""))
        if flag:
            L.append("} else { Write-Host '(skipped - add " + flag.replace("$", "-") + ")' -ForegroundColor DarkGray }")
        L.append("")
    rows = data["offload"]["items"]
    L.append(f"# {'=' * 70}\n# ONEDRIVE - make stale synced files online-only ({len(rows)} files)\n# {'=' * 70}")
    L.append("if ($IncludeOffload) {\nWrite-Host '--- ONEDRIVE free up space' -ForegroundColor Cyan")
    for i in rows:
        L.append(f"  Set-TidyOnlineOnly {ps_quote(i['path'])}")
    L.append("} else { Write-Host '(OneDrive offload skipped - add -IncludeOffload)' -ForegroundColor DarkGray }")
    L.append("")
    L.append("if ($Apply -and $Recycle) { Write-Host (\"`nMoved about {0:N1} GB to the Recycle Bin - empty it to get the space back\" -f ($script:Freed/1GB)) -ForegroundColor Green }")
    L.append("elseif ($Apply) { Write-Host (\"`nFreed about {0:N1} GB\" -f ($script:Freed/1GB)) -ForegroundColor Green }")
    L.append("else { Write-Host (\"`nDry run: about {0:N1} GB would be freed. Add -Apply to do it.\" -f ($script:Planned/1GB)) -ForegroundColor Yellow }")
    # UTF-8 with BOM so Windows PowerShell 5.1 reads non-ASCII paths correctly
    path.write_text("\n".join(L), encoding="utf-8-sig")


def render_html(data: dict | None, app: dict | None = None) -> str:
    template = (HERE / "report_template.html").read_text(encoding="utf-8")

    def js(o):
        return json.dumps(o, separators=(",", ":"), default=str).replace("</", "<\\/")
    return template.replace("/*__TIDYUP_DATA__*/null", js(data)).replace("/*__TIDYUP_APP__*/null", js(app))


def write_html(data: dict, path: Path):
    path.write_text(render_html(data), encoding="utf-8")


# --------------------------------------------------------------------------- run
def report_dir(cfg: dict, out: str | None = None) -> Path:
    out_dir = Path(out or cfg.get("report_dir", "reports"))
    return out_dir if out_dir.is_absolute() else HERE / out_dir


def onedrive_root() -> str:
    return os.environ.get("OneDriveCommercial") or os.environ.get("OneDrive") or ""


def archive_root(cfg: dict) -> str:
    """Common OneDrive root that archived items are moved under (config: archive.root)."""
    raw = (cfg.get("archive") or {}).get("root", "{onedrive}\\TidyUp Archive")
    env = {"onedrive": onedrive_root(), "home": os.environ.get("USERPROFILE", str(Path.home()))}
    p = re.sub(r"\{([A-Za-z0-9_]+)\}", lambda m: env.get(m.group(1).lower(), m.group(0)), raw)
    return os.path.normpath(p)


def run_scan(config=HERE / "tidyup.config.json", rules_path=HERE / "tidyup.rules.json",
             roots=None, out=None, no_duplicates=False, quiet=True, progress=None, cancel=None) -> dict:
    """Scans, writes all outputs and returns their paths plus the report data.
    `progress` (dict) is updated live; setting `cancel` (threading.Event) raises ScanCancelled."""
    cfg = load_json(Path(config))
    rules = load_rules(rules_path)
    problems = validate_rules(rules, build_vars())
    if problems:
        raise ValueError("rule problems - fix them or run 'python tidyup.py --check-rules':\n  " + "\n  ".join(problems))
    roots = roots or cfg.get("roots") or ["C:\\"]
    out_dir = report_dir(cfg, out)
    (out_dir / "history").mkdir(parents=True, exist_ok=True)
    (out_dir / "cleanup").mkdir(parents=True, exist_ok=True)

    variables = build_vars()
    use_atime = atime_trustworthy(cfg.get("use_last_access_time", "auto"))
    sc = Scanner(cfg, rules, variables, use_atime, quiet)
    if progress is not None:
        sc.progress = progress
    sc.cancel = cancel
    if not quiet:
        print(f"PC TidyUp {VERSION}: scanning {', '.join(roots)} "
              f"(last-access time {'used' if use_atime else 'ignored'}) ...", file=sys.stderr)
    t0 = time.time()
    sc.scan(roots)
    dups, dup_timeout = [], False
    if not no_duplicates and cfg.get("duplicates", True):
        if not quiet:
            print("  checking duplicates ...", file=sys.stderr)
        dups, dup_timeout = sc.duplicates(sc.t.get("duplicate_time_budget_s", 300))
    sc.progress.update(phase="report")
    duration = time.time() - t0

    previous = None
    for h in reversed(sorted((out_dir / "history").glob("snapshot-*.json"))):
        try:
            p = load_json(h)
            if p.get("roots") == roots:
                previous = p
                break
        except (OSError, ValueError):
            continue

    data, snapshot = build_data(sc, roots, dups, dup_timeout, duration, cfg, previous)
    if no_duplicates:
        data["duplicates"]["skipped"] = True
    prioritize(data, cfg)

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    html_path = out_dir / f"TidyUp-Report-{stamp}.html"
    md_path = out_dir / f"TidyUp-Report-{stamp}.md"
    ps_path = out_dir / "cleanup" / f"TidyUp-Cleanup-{stamp}.ps1"
    json_path = out_dir / "history" / f"TidyUp-Data-{stamp}.json"
    data["meta"].update(cleanup_script=str(ps_path), stamp=stamp, data_file=str(json_path),
                        archive_root=archive_root(cfg), onedrive=onedrive_root())
    write_html(data, html_path)
    write_markdown(data, md_path)
    write_cleanup(data, ps_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    shutil.copyfile(html_path, out_dir / "TidyUp-Report-latest.html")
    shutil.copyfile(md_path, out_dir / "TidyUp-Report-latest.md")
    shutil.copyfile(json_path, out_dir / "TidyUp-Data-latest.json")
    with open(out_dir / "history" / f"snapshot-{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f)

    keep = int(cfg.get("keep_reports", 10))
    for pattern in ("TidyUp-Report-2*.html", "TidyUp-Report-2*.md", "cleanup/TidyUp-Cleanup-*.ps1",
                    "history/TidyUp-Data-*.json"):
        for f in sorted(out_dir.glob(pattern))[:-keep] if keep > 0 else []:
            try:
                f.unlink()
            except OSError:
                pass
    return dict(data=data, html=html_path, md=md_path, ps1=ps_path, json=json_path, duration=duration,
                stats=sc.stats)


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="PC TidyUp - storage usage review and cleanup advice. "
                                             "For the interactive app (actions, live progress) run tidyup_app.py.")
    ap.add_argument("--config", default=str(HERE / "tidyup.config.json"))
    ap.add_argument("--rules", default=str(HERE / "tidyup.rules.json"))
    ap.add_argument("--root", action="append", help="folder or drive to scan (repeatable); overrides config")
    ap.add_argument("--out", help="report folder (default from config)")
    ap.add_argument("--no-duplicates", action="store_true", help="skip duplicate detection")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check-rules", action="store_true", help="validate tidyup.rules.json + tidyup.rules.user.json and exit")
    ap.add_argument("--explain", metavar="PATH", help="show which rules match a file or folder and exit")
    args = ap.parse_args(argv)

    if args.check_rules or args.explain:
        rules = load_rules(args.rules)
        if args.explain:
            print(explain(args.explain, load_json(Path(args.config)), rules))
            return 0
        problems = validate_rules(rules, build_vars())
        print(f"{len(rules['dir_rules'])} folder rules, {len(rules['file_rules'])} file rules "
              f"({rules['_user_rules']} from {USER_RULES})")
        for pr in problems:
            print("  PROBLEM:", pr)
        print("Rules OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    r = run_scan(args.config, args.rules, args.root, args.out, args.no_duplicates, args.quiet)
    data = r["data"]
    t = data["totals"]
    print(f"\nTidyUp done in {r['duration']:.0f} s - scanned {r['stats']['files']:,} files / {human(r['stats']['bytes'])}")
    for d in data["disk"]:
        print(f"  {d['root']}  free {human(d['free'])} of {human(d['total'])}  [{d['health']}]")
    for k in (1, 2, 3):
        print(f"  P{k} {PRIORITY[k][0]:<13} {human(data['priority_totals'][str(k)]):>10}")
    print(f"  low risk         {human(t['safe']):>10}")
    print(f"  likely obsolete  {human(t['likely']):>10}")
    print(f"  your decision    {human(t['review']):>10}")
    print(f"  compress saves   {human(data['compress']['savings']):>10}")
    print(f"  OneDrive offload {human(data['offload']['bytes']):>10}")
    print(f"  archive          {human(data['archive']['bytes']):>10}")
    print(f"  duplicates       {human(data['duplicates']['wasted']):>10}")
    print(f"\n  HTML:     {r['html']}\n  Markdown: {r['md']}\n  Cleanup:  {r['ps1']} (dry run by default)")
    print("\n  Use PC TidyUp entirely at your own risk - no warranty, no liability for damage or data loss. See DISCLAIMER.md.")
    if args.open:
        os.startfile(r["html"])  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    sys.exit(main())
