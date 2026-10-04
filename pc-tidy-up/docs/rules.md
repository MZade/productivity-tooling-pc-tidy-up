# PC TidyUp: rules

Everything PC TidyUp recognises (a cache, an old log, an installer you no longer need) comes from a **rule**. Rules are data, not code: you can add, change or switch them off without programming.

- **Built-in rules** live in `tidyup.rules.json`. You never need to edit this file.
- **Your rules** live in `tidyup.rules.user.json`, created from `tidyup.rules.user.example.json` on first start. They are checked **before** the built-in rules.
- A rule of yours with the **same `id`** as a built-in rule **replaces** it. `{ "id": "dumps", "disabled": true }` switches a built-in rule off.

The easiest way to manage rules is **⚙ Settings → Rules** in the app. It has form editors, validation and a "Test a path" helper. This page describes the format behind it.

## Two kinds of rules

| Kind | Matches | Shown as |
|---|---|---|
| **Folder rule** (`dir_rules`) | Folders, by path pattern | The whole folder as one item (`mode: whole`), or each entry inside it that's old enough (`mode: children`) |
| **File rule** (`file_rules`) | Files, by extension and/or file-name pattern, optionally limited to a path | Grouped per folder |

Files inside a folder matched by a folder rule belong to that folder. File rules don't apply there unless they set `inside_rule_folders: true`.

## Fields

### Common

| Field | Meaning |
|---|---|
| `id` | Unique name. The same id as a built-in rule replaces that rule. |
| `label` | Name shown in the report |
| `action` | `safe` (regenerable), `likely` (probably obsolete), `review` (your decision), `info` (managed by Windows/an app; advice only). It sets the priority. |
| `min_age_days` | Only if unchanged or unused for at least this many days |
| `reason`, `how` | "Why" and "How to handle it", shown in the report |
| `system` | `true` = may match inside protected system folders |
| `disabled` | `true` = switched off |

### Folder rules

| Field | Meaning |
|---|---|
| `path` | One pattern or a list of patterns |
| `mode` | `whole` (default) or `children` |
| `exclude` | Patterns that must **not** match, e.g. `"{home}/appdata/**"` |
| `ps` | A PowerShell command used for permanent deletes instead of deleting files, e.g. `npm cache clean --force` |

### File rules

| Field | Meaning |
|---|---|
| `exts` | Extensions such as `[".log", ".tmp"]` |
| `names` | File-name patterns such as `["~$*", "*.log.[0-9]*"]` |
| `path` | Optional: only files in these locations |
| `min_size_kb` | Only files of at least this size |
| `age_basis` | `used` (default: last access or change) or `modified` (last change only) |
| `inside_rule_folders` | `true` = also inside folders matched by a folder rule |

## Patterns and variables

Patterns are case-insensitive globs with `/` as separator:

| Pattern | Matches |
|---|---|
| `*` | anything within one folder name |
| `**` | any number of folders |
| `{a,b}` | `a` or `b` |
| `?`, `[0-9]` | one character, a character range |

| Variable | Typical value |
|---|---|
| `{home}` | `C:\Users\<you>` |
| `{localappdata}` / `{appdata}` | `…\AppData\Local` / `…\AppData\Roaming` |
| `{temp}` | `…\AppData\Local\Temp` |
| `{downloads}` | `C:\Users\<you>\Downloads` |
| `{onedrive}` | your OneDrive folder |
| `{systemdrive}` | `C:\` |
| `{windir}`, `{programdata}`, `{programfiles}`, `{programfilesx86}` | the Windows folders |

## Examples

Build output in your repositories, older than 30 days:

```json
{ "id": "my-build-output", "label": "Build output in C:\\Repos", "action": "likely", "min_age_days": 30,
  "path": "{systemdrive}/repos/**/{bin,obj}",
  "reason": "Recreated by the next build.", "how": "Delete, or 'git clean -xdf' in the repo." }
```

Each old recording in a folder, as its own item:

```json
{ "id": "my-recordings", "label": "Old meeting recordings", "action": "review", "mode": "children",
  "min_age_days": 90, "path": "{home}/videos/recordings",
  "reason": "Recordings are also stored in the cloud.", "how": "Delete local copies you no longer need." }
```

Large exported packages anywhere:

```json
{ "id": "my-old-packages", "label": "Old deployable packages", "action": "review",
  "min_age_days": 60, "min_size_kb": 10240, "names": ["*DeployablePackage*.zip"],
  "reason": "Kept in the build system anyway.", "how": "Delete local copies." }
```

These go in `tidyup.rules.user.json` under `dir_rules` (the first two) or `file_rules` (the last one).

## Checking your rules

```powershell
python tidyup.py --check-rules                    # validates fields, actions, variables, patterns, duplicate ids
python tidyup.py --explain "C:\Repos\shop\bin"    # which rules match this path, protected?, in OneDrive?, category
```

Then run a new scan. In the app, **Settings → Test a path** does the same as `--explain`.

## Categories and compression

`tidyup.rules.json` also defines the **file-type categories** used in the charts and the **compression ratios** used to estimate savings. You can extend both in your user file:

```json
{ "categories": { "Packages": [".axpp"] }, "compression_ratios": { ".trx": 0.8 } }
```
