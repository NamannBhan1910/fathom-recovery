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
  panel_snippet.py          # paste-in block for a panel's generation code
  tests/test_fathom_recovery.py   # offline tests, fake S3, no network
  README.md
```

## Using it in a panel (rollout)

1. **Get the file into the panel workspace.** Copy `fathom_recovery.py` into the panel's
   `scripts/` folder (or push this folder to your git remote and pull it in each panel's
   workspace; that is the way to keep one version across projects).
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

**Publish (owner, once per release):** commit, tag (`git tag -a v1.2.0 -m v1.2.0`), and
`git push origin main v1.2.0` to the shared private repo. Give panel owners read access
(or a read-only deploy token).

**Install in a panel:**

```
export FATHOM_RECOVERY_GIT_TOKEN=...      # private HTTPS repos only; never put it in the URL
python3 sync_recovery.py --repo <git url> --ref v1.2.0 --dest scripts --run-tests
```
Prints the installed version, commit and sha256. Pin a tag, not `main`.
To upgrade a panel, rerun with the new tag.

**Verify in the panel:**

```
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>"           # read-only
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>" --apply   # writes 1 probe file, verifies size + sha256
```
`selfcheck --apply` leaves a small `selfcheck_probe.txt` in the panel's recovery folder (nothing is ever deleted).
