# fathom-recovery (v1.9.2)

One single-file, command-line tool (`fathom_recovery.py`, needs only `boto3`) that gives **any Fathom panel** a
recovery copy in S3. It is storage only: it copies files and **never deletes anything**, locally or in S3.

> **v1.9.x is a CLI, not a library.** The importable Python functions of v1.7.x and older, and its `upload`, `copy`
> and `init` commands, no longer exist. Use the commands below, or `panel_snippet.py` to run them from Python.
> A panel still on v1.7.x keeps working with its own tag; do not mix files from different tags.

## Where files go

```
s3://<bucket>/6_Recovery/<user>/<Panel>/
    code/<relative path>                      whole workspace, ONE live copy (no date folder)
    <YYYYMMDD>_current_data/<relative path>   data files of outputs/, one folder per UTC day, beside code/
    <old folders>                             older layouts, never touched
```

| Part | Rule |
|---|---|
| `<user>` | `--user`, else env `RECOVERY_USER`, else `bridge.whoami()` (username, else the part of the email before `@`), else `unknown-user`. |
| `<Panel>` | `panel_name` from `recovery_config.json`. Rejected if empty, `.`, `..`, or containing `<` or `>`. |
| Folder matching | Ignores case and extra spaces, the same rule for user and panel. The exact spelling wins; one case-insensitive match uses that spelling; several matches and none exact fail ("the exact spelling is needed"); none creates the folder with the given spelling. If listing is not permitted, the given spelling is used with a warning. |
| Bucket / prefix | Defaults `mosaic-derivatives-margining` / `6_Recovery`, overridable with `RECOVERY_DESTINATION_BUCKET` / `RECOVERY_DESTINATION_PREFIX`. The bucket name is never printed. |

### One folder per UTC day (data)

`<YYYYMMDD>_current_data/` is the **only** per-day folder this tool creates. It holds the setup probe
`selfcheck_probe.txt`, the data files and `.fathom_manifest.json`. The tool never creates a timestamp run folder.

- **At setup** today's folder is created (probe plus any data already in `outputs/`).
- **Later the same day** new or changed files go into the SAME folder; a file changed again overwrites that day's copy.
- **On a new UTC day** the first sweep that has new or changed data creates the next `<YYYYMMDD>_current_data/`.
  Yesterday's folder is never modified. A sweep with nothing new creates nothing.
- UTC midnight is 8:00 PM EDT (7:00 PM EST), so an evening run can already land in the next day's folder.

## Setup (once per panel)

```
[ -d fathom-recovery-v1.9.2 ] || git clone --depth 1 --branch v1.9.2 https://github.com/NamannBhan1910/fathom-recovery.git fathom-recovery-v1.9.2
python3 fathom-recovery-v1.9.2/setup_panel.py --panel-name "<Panel>" --output-dir outputs --apply
```

`setup_panel.py` installs `scripts/fathom_recovery.py` from that clone and runs its `setup` command, which writes
`recovery_config.json` if missing (an existing one is kept), creates `outputs/` if missing, runs `selfcheck`, then the
first data `sweep` and the first `code-sweep`. Without `--apply` it is a dry run and writes nothing to S3.
Or, with the script already installed:

```
python3 scripts/fathom_recovery.py setup --panel-name "<Panel>" --output-dir outputs --apply
```

A FAIL mentioning `AccessDenied`, `NoCredentialsError` or `NoSuchBucket` means that panel's runtime has no S3 permission
on `6_Recovery/`; the tool prints `STOP: ...` and exits with code 2.

## Commands

All are dry runs unless `--apply` is given. `--panel-name` can replace `--config` on `selfcheck`, `list`, `list-code`.

| Command | What it does |
|---|---|
| `setup --panel-name N --output-dir D` | Config if missing, creates `D` if missing, `selfcheck`, first `sweep`, first `code-sweep`. |
| `sweep --config C --state-file .fathom_recovery/state.json` | Data files (csv xlsx xls json parquet txt pdf, any case) of `outputs/`, sub-folders kept, to today's `<YYYYMMDD>_current_data/`. Only new or changed files. Last lines: `data_folder=...`, `SUMMARY uploaded=X unchanged=Y failed=Z`. |
| `code-sweep --config C --state-file .fathom_recovery/code_state.json` | Whole workspace to `code/<path>` (see below). Last lines: `uploaded_by_folder`, `knowledge_dir_present=... scripts_dir_present=...`, `SUMMARY uploaded=X unchanged=Y failed=Z skipped_sensitive=S skipped_large=L`. |
| `selfcheck --config C [--apply]` | Read-only checks; with `--apply` writes and verifies one `selfcheck_probe.txt` in today's `<YYYYMMDD>_current_data/` (re-running the same day overwrites that one probe; it is not listed in the sweep manifest). |
| `list --config C [--user U] [--legacy]` | Every folder beside `code/` (the `<YYYYMMDD>_current_data` folders and old timestamp runs), newest first. `--legacy` lists the old `<prefix>/<Panel>/<user>/<run>/<file>` layout. |
| `list-code --config C [--user U] [--run R]` | No `--run`: the live `code/` copy plus any old day or timestamp folders inside `code/`. `--run live`: the live entries, then `S3_FILE_COUNT` and `MANIFEST_FILE_COUNT` (they should match). `--run <folder>`: that old folder. |
| `verify-code --config C [--user U]` | Read-only. Re-downloads every live `code/` object, hashes it and compares with the local file. Prints `OK` / `MISMATCH` / `MISSING_IN_S3` / `S3_ONLY` per entry, then `SUMMARY verified_ok=... mismatch=... missing_in_s3=... s3_only=...`; exit 1 on any mismatch or missing file. |
| `restore-code --config C [--run R] [--target-dir D] [--overwrite]` | Writes only into a NEW folder (default `restored_code/`). No `--run`: the live copy wins and files that exist only in old folders are added from the newest old folder that has them. `--run live`: live only. `--run <folder>`: exactly that folder. Empty-folder markers are recreated, hidden and empty files restored, every sha256 re-checked, existing files refused without `--overwrite`, keys with `..` rejected. Never touches live panel files. |
| `restore --recovery-path "<user>/<Panel>/<folder>/<file>" --target-bucket B --target-key K [--overwrite]` | Copies one recovery object to your target key; refuses an existing target without `--overwrite`; verifies sha256. |

### Whole-workspace backup (`code-sweep`)

`code-sweep` walks the workspace root (the folder holding `recovery_config.json`) and uploads **every** file and every
empty folder, including `knowledge/`, hidden files and folders, the platform's `.fathom_runtime/` and
`.workspace_meta.json`, the tool's own `.fathom_recovery/` state files and empty files.

- **Empty folders** are stored as a zero-byte marker whose key ends in `/` (manifest entry has `"type": "dir"`).
- **Skipped only:** `.git`, `__pycache__`, `node_modules`, `venv`, `.venv`, `site-packages`, `restored_code/`, symlinks
  (reported `skipped_symlink`), `*.tmp`, `.tmp_*.json`, files over 10 MB (reported `skipped_large`), and data files
  inside `output_dir` (they belong to `sweep`). Any other file in `outputs/` is backed up.
- **Sensitive names** are never uploaded and are reported as `skipped_sensitive_name`: extensions `.pem .key .pfx .p12 .jks
  .keystore`, files named `.env` / `.env.*`, and json, yaml, yml, toml, ini, cfg, conf or txt files whose name contains
  `secret`, `credential`, `password`, `passwd`, `token`, `private_key`, `service_account`, `id_rsa`, `apikey` or `api_key`.
  `.py`, `.md` and `.pdf` are never judged by name. File contents are not inspected, so keep secrets in environment variables.
- **One live copy**, no date folder: a changed file overwrites its copy; files deleted locally stay in S3.
- **State** `.fathom_recovery/code_state.json` (path -> sha256, plus `.fathom_layout` and `.fathom_state_synced`). A file is
  recorded only after its upload is verified (size, `sha256` metadata, content read back). State files never trigger an
  upload on their own; they are re-uploaded only when something else was uploaded. An idle sweep reports `uploaded=0`.
- `code/.fathom_manifest.json` (`run_id` "live") is merged on every sweep.
- Optional config keys: `"code_patterns": ["*.py"]` and `"code_exclude_dirs": ["tmp"]`.

## Upload rules (both sweeps)

Every upload is a `put_object` with `sha256` in the object metadata, then verified: size, metadata and the content hash read
back from S3. State is written atomically (temp file in `.fathom_recovery/`, then rename) and a file is recorded only after
it verified, so a failed file is retried next time and never stops the sweep. There are no delete or lifecycle calls anywhere.

## Using it from a panel

Nothing watches files or runs by itself; the panel's code, its agent or a schedule has to call it. The standing rules are:

1. After creating or changing a data file in `outputs/`, run the data sweep as the last step:
   `python3 -B scripts/fathom_recovery.py sweep --config recovery_config.json --state-file .fathom_recovery/state.json --apply`
2. After creating or changing anything else (code, `knowledge/`, configs, ...), run the code sweep:
   `python3 -B scripts/fathom_recovery.py code-sweep --config recovery_config.json --state-file .fathom_recovery/code_state.json --apply`
3. At the start of every session run both once (`knowledge/` and `.fathom_runtime/` can change under the panel).

From Python, `panel_snippet.py` wraps those two commands: `sweep_now()` and `code_sweep_now()` run the installed CLI with the
right state files and return `{ok, uploaded, unchanged, failed, exit_code, output}`.

Use `python3 -B` or `PYTHONDONTWRITEBYTECODE=1` so no `__pycache__` appears in the panel.

## Repo layout

```
fathom-recovery/
  fathom_recovery.py              # the whole tool (single file, needs only boto3)
  setup_panel.py                  # installs the script from this clone and runs its `setup`
  sync_recovery.py                # pulls a pinned tag from git into a panel
  panel_snippet.py                # sweep_now() / code_sweep_now() for a panel's own code
  tests/test_fathom_recovery.py   # offline tests, fake S3, no network
  README.md
```

## Sharing across panels (git)

**Publish (owner):** commit, tag (`git tag -a v1.9.2 -m v1.9.2`) and `git push origin <branch> v1.9.2`.

**Install in a panel:**

```
export FATHOM_RECOVERY_GIT_TOKEN=...      # private HTTPS repos only; never put it in the URL
python3 sync_recovery.py --repo <git url> --ref v1.9.2 --dest scripts --run-tests --panel-name "<Panel>" [--apply]
```

Prints the installed version, commit and sha256. Pin a tag, not `main`. `--panel-name` also runs the installed script's
`setup` (a dry run unless `--apply`).

## Checking a panel's copy

```
grep -c 'LAYOUT = "user/panel"' scripts/fathom_recovery.py
grep -c 'CODE_BACKUP = "1"'     scripts/fathom_recovery.py
grep -c 'CODE_FLAT = "1"'       scripts/fathom_recovery.py
grep -c 'CODE_SCOPE = "all"'    scripts/fathom_recovery.py
grep -c 'PROBE_DIR = "data-day"' scripts/fathom_recovery.py
```

All five print 1 for the v1.9.x script (`__version__ = "1.9.1"`). A script failing only the last is v1.9.0 (probe in a separate timestamp folder); failing
`CODE_FLAT` is v1.8.x or older (date folders under `code/`); failing `CODE_SCOPE` is v1.7.x (skips hidden files).
`PROBE_DIR = "data-day"` is a version marker only; the probe logic does not read it.

## Tests

```
python3 -B -m unittest discover -s tests -v
```

Offline only; a fake S3 stands in for the bucket and nothing real is written.

## Rules of thumb

- Case and extra spaces in the panel name do not matter, but a typo creates a new folder.
- The tool never deletes anything. Retention, if wanted, should be an S3 lifecycle rule on `6_Recovery/`.
- Needs credentials with `s3:PutObject`, `s3:GetObject` (read-back check) and `s3:ListBucket` on `6_Recovery/*`.
  Run `selfcheck --apply` in each panel to confirm.
- Never put a secret, password, token or key into a code or knowledge file; use an environment variable.
- Old objects (older timestamp runs, old `code/<date>/` folders, the legacy panel-first layout) are never moved or renamed;
  `list`, `list --legacy`, `list-code` and `restore-code` still read them.
