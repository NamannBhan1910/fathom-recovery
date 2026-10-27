# fathom-recovery

One small, dependency-light module that gives **any Fathom panel** a recovery copy of the
files it generates. Panels live in different conversations/projects, so the repo is designed
to be copied into (or pulled into) each one without any panel-specific code.

## Where files go

```
s3://mosaic-derivatives-margining/6_Recovery/<Panel Name>/<user>/<YYYYMMDDTHHMMSSZ>-<8 hex>/<filename>
```

| Part | Source |
|---|---|
| `<Panel Name>` | **Required.** Given by the user at execution. Exact match: case and spaces matter. A new name creates a new panel folder. |
| `<user>` | `--user` / `user=` > `RECOVERY_USER` env > `bridge.whoami()` > `unknown-user` |
| `<timestamp>-<8 hex>` | UTC time of the copy + random suffix. Unique per copy, so **nothing is overwritten**; every version is kept. |

Each recovery object carries `sha256` metadata of the source bytes. The helper checks the copied size
against the source and reports `ok: false` on mismatch. Panel names are sanitised (slashes can't
create extra folders; blank, `.` and `..` are rejected) **before** any write.

Bucket/prefix defaults can be overridden with `RECOVERY_DESTINATION_BUCKET` / `RECOVERY_DESTINATION_PREFIX`.

## Repo layout

```
fathom-recovery/
  fathom_recovery.py        # the whole library + CLI (single file, needs only boto3)
  panel_snippet.py          # paste-in block for a panel's generation code (push, sweep_now)
  sync_recovery.py          # installs fathom_recovery.py into a panel from this repo at a tag
  tests/test_fathom_recovery.py   # offline tests, fake S3, no network
  README.md
```

## Using it in a panel (rollout)

1. **Get the file into the panel workspace.** Install it with `sync_recovery.py` (see "Sharing
   across panels" below), which keeps one tagged version across projects, or copy
   `fathom_recovery.py` into the panel's `scripts/` folder by hand.
2. **Push the panel's output** (see `panel_snippet.py`). Simplest path, `upload_to_recovery`
   (v1.2.0+), takes bytes, text, or a `pathlib.Path` to a local file:

```python
from pathlib import Path
from fathom_recovery import upload_to_recovery

upload_to_recovery(csv_bytes, "report.csv", panel_name=PANEL_NAME)   # bytes / str content
upload_to_recovery(Path("report.csv"), panel_name=PANEL_NAME)        # local file
```
   It writes `6_Recovery/<PANEL_NAME>/<user>/<timestamp>-<8 hex>/<file>` with `sha256` metadata and
   verifies size, stored sha256 and the content read back; `result["ok"]` is the pass/fail.
   A plain `str` is treated as **content**; pass a `Path` for a file.

   Other entry points: `copy_created_object_to_recovery(bucket, key, panel_name=...)` for an object
   already in S3, and `put_object_and_copy_to_recovery(...)` to write a source object and copy it.
3. **Check it** with a dry run, then `--apply` once (CLI):

```
python3 fathom_recovery.py upload --panel-name "My Panel" --file report.csv            # dry run
python3 fathom_recovery.py upload --panel-name "My Panel" --file report.csv --apply
python3 fathom_recovery.py copy   --panel-name "My Panel" --source-bucket B --source-key K [--apply]
```


### Uploading generated files in bulk (`sweep`, v1.3.0+)

**Nothing in this repo runs by itself.** Cloning or installing it uploads nothing. Files reach S3
only when something calls `push`, `sweep_now` or the `sweep` command.

`sweep` saves you from calling `push` once per file: it uploads every **new or changed** file in a
folder and remembers what it has uploaded (`.fathom_recovery_state.json`, path -> sha256), so
reruns skip unchanged files. A file is recorded only after its upload verified OK; failures are
retried on the next sweep. Something still has to trigger it:

| Trigger | When files reach S3 | What the panel needs |
|---|---|---|
| `push(data, filename)` after each file is written | Immediately | One line per file |
| `sweep_now("outputs")` at the end of the generation step | At the end of each run | One line per run |
| `sweep` command on a schedule | At the next scheduled run | A schedule (none is created by this repo) |

```
python3 scripts/fathom_recovery.py sweep --panel-name "<Panel>" --dir . --pattern "*.csv"          # dry run
python3 scripts/fathom_recovery.py sweep --panel-name "<Panel>" --dir . --pattern "*.csv" --apply  # upload
```
- Defaults (command and `sweep_now`): csv, xlsx, xls, json, parquet, txt. It skips `.git`,
  `.fathom_runtime`, `knowledge`, `tests`, `__pycache__`, `node_modules` and `.venv`.
- Call it after the files are fully written, so a half-written file is not uploaded.
- Files are uploaded by bare name, so same-named files in different subfolders are stored separately.
- Each file is read into memory whole, and the state file is saved once at the end of a sweep. If a
  sweep is interrupted, that run's files upload again next time (extra copies, no data loss).
- A deleted-then-recreated identical file is not re-uploaded (same hash).
- If a panel's workspace is reset, the state file is lost and the next sweep uploads everything once.

## Finding and restoring

```
python3 fathom_recovery.py list    --panel-name "My Panel" [--user someone] [--limit 20]
python3 fathom_recovery.py restore --recovery-path "My Panel/<user>/<run>/<file>" \
                                   --target-bucket B --target-key K            # dry run
                                   # add --apply to restore, --overwrite to replace an existing target
```
`list` is read-only and newest-first; it also shows older flat/legacy objects (run id `null`).
`restore` refuses to overwrite an existing target unless `--overwrite`, and re-checks the stored `sha256`.

## Tests

```
python3 -m unittest discover -s tests -v
```
Offline only; nothing touches S3.

## Rules of thumb

- A panel is only protected once its generation code calls the helper.
- Use the **same** panel name every time for the same panel (exact match, so a typo makes a new folder).
- The helper never deletes anything. Retention/cleanup, if wanted, should be an S3 lifecycle rule on `6_Recovery/`.
- Needs AWS credentials with `s3:GetObject` and `s3:PutObject` on the source and recovery locations, and `s3:ListBucket` for `list`.
  For `upload` only the recovery prefix is needed: `s3:PutObject`, `s3:GetObject` (read-back check) and `s3:ListBucket`
  on `6_Recovery/*`. Run `selfcheck --apply` in each panel to confirm; AccessDenied means that panel has no access yet.
- The `<user>` folder is case-sensitive (`NBHAN` and `nbhan` are different folders). Let `resolve_user()` pick it
  (or set `RECOVERY_USER`) instead of hardcoding one.

## Registered panel names

Exact spelling matters. Add a row before a panel starts uploading.

| Panel name | Notes |
|---|---|
| Fathom Recovery Backup | synthetic test data / selfcheck probes |
| Daily Checklist Brokerage | existing |
| Daily Checklist Equities | existing |
| Equities Checklist EU | existing |
| Team Daily Checklist Brokerage | existing |
| Brokerage Recon | existing (folder marker only) |

## Sharing across panels (git)

**Publish (owner, once per release):** commit, tag (`git tag -a v1.3.1 -m v1.3.1`), and
`git push origin main v1.3.1`. The repo is currently public and contains no credentials. If it is
made private, give panel owners read access or a read-only token.

**Install in a panel** (a fresh panel needs `sync_recovery.py` first; fetch it from the repo):

```
git clone --depth 1 --branch v1.3.1 https://github.com/NamannBhan1910/fathom-recovery.git
python3 fathom-recovery/sync_recovery.py --repo https://github.com/NamannBhan1910/fathom-recovery.git \
    --ref v1.3.1 --dest scripts --run-tests
```
For a private repo, `export FATHOM_RECOVERY_GIT_TOKEN=...` first (never put it in the URL).
`sync_recovery.py` prints the installed version, commit and sha256, and replaces any existing
`scripts/fathom_recovery.py`. Pin a tag, not `main`. To upgrade a panel, rerun with the new tag.
It installs only `fathom_recovery.py`; copy `panel_snippet.py` into the panel yourself and set `PANEL_NAME`.

**Verify in the panel:**

```
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>"           # read-only
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>" --apply   # writes 1 probe file, verifies size + sha256
```
`selfcheck --apply` leaves a small `selfcheck_probe.txt` in the panel's recovery folder (nothing is ever deleted).
