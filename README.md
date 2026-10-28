# fathom-recovery

One small, dependency-light module that gives **any Fathom panel** a recovery copy of the
files it generates. Panels live in different conversations/projects, so the repo is designed
to be copied into (or pulled into) each one without any panel-specific code.

## Where files go

```
s3://mosaic-derivatives-margining/6_Recovery/<user>/<Panel Name>/<YYYYMMDDTHHMMSSZ>-<8 hex>/<filename>
```

| Part | Source |
|---|---|
| `<Panel Name>` | **Required.** Lives inside the user's own folder. Given by the user at execution. Matched to an existing folder ignoring case and extra spaces (`daily checklist equities` uses `Daily Checklist Equities`). A name that matches no folder creates one, with the spelling given. If two existing folders differ only by case, the call fails and the exact spelling must be used. |
| `<user>` | First level. `--user` / `user=` > `RECOVERY_USER` env > `bridge.whoami()` > `unknown-user`. Every user gets their own folder, and panel names are matched only inside it. |
| `<timestamp>-<8 hex>` | UTC time of the copy + random suffix. Unique per copy, so **nothing is overwritten**; every version is kept. |

Each recovery object carries `sha256` metadata of the source bytes. The helper checks the copied size
against the source and reports `ok: false` on mismatch. Panel names are sanitised (slashes can't
create extra folders; blank, `.` and `..` are rejected) **before** any write.

Bucket/prefix defaults can be overridden with `RECOVERY_DESTINATION_BUCKET` / `RECOVERY_DESTINATION_PREFIX`.

## Layout change in v1.6.0: user first, then panel (v1.6.1: user folder matched ignoring case, LAYOUT marker)

Before v1.6.0 files went to `6_Recovery/<Panel>/<user>/...`. From v1.6.0 they go to
`6_Recovery/<user>/<Panel>/...`, so each user has one folder holding all of their panels.

- The user folder is matched ignoring case (`nbhan` -> existing `NBHAN`), then panel names are matched (case
  and extra spaces ignored) only inside that user's folder, so two users can have panels with the same name
  without sharing a folder.
- `list` shows the current user's panel by default; `--user <name>` looks at another user's.
- Nothing already in S3 was moved or renamed. Objects written before v1.6.0 stay under the old path and
  can be found with `list --legacy` and restored with their `recovery_path` as before.

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
   It writes `6_Recovery/<user>/<PANEL_NAME>/<timestamp>-<8 hex>/<file>` with `sha256` metadata and
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

### Fastest setup for a new panel (v1.6.1+)

Run these in the panel's workspace root (in Fathom, ask the panel's agent to run them). The clone goes
into a folder named after the version, so running the lines again never fails with "already exists":

```
[ -d fathom-recovery-v1.6.1 ] || git clone --depth 1 --branch v1.6.1 https://github.com/NamannBhan1910/fathom-recovery.git fathom-recovery-v1.6.1
python3 fathom-recovery-v1.6.1/setup_panel.py --panel-name "<Panel>" --output-dir outputs --apply
```

`setup_panel.py` installs `scripts/fathom_recovery.py` from that clone, writes `recovery_config.json` and
`RECOVERY_INSTRUCTIONS.md` (an existing config is kept), runs `selfcheck`, and does a first `sweep`. Every
step prints PASS / FAIL. Without `--apply` it is a dry run and writes nothing to S3. `--with-snippet`
also copies `panel_snippet.py`. A FAIL mentioning AccessDenied or NoCredentialsError means that panel's
runtime has no S3 permission on `6_Recovery/`. `RECOVERY_INSTRUCTIONS.md` now includes these setup lines, so
the panel's agent can do the setup itself.

### Making a panel upload new files (v1.5.0+)

Nothing in this repo watches for files or runs by itself. What it does give you is a standing
instruction for the panel's agent, plus a config file so the command is the same in every panel:

```
python3 sync_recovery.py --repo <git url> --ref v1.5.0 --dest scripts --run-tests \
    --panel-name "<Panel>" --output-dir outputs
```

This installs `scripts/fathom_recovery.py` and writes two local files in the current folder:

- `recovery_config.json` - `{"panel_name": ..., "output_dir": ...}`
- `RECOVERY_INSTRUCTIONS.md` - the rule for the panel's agent: after creating or changing a data
  file, run `python3 scripts/fathom_recovery.py sweep --config recovery_config.json --apply`, and
  run it once at the start of each session.

Give the contents of `RECOVERY_INSTRUCTIONS.md` to the panel's agent as a standing instruction.
It works only as long as the agent follows it; a missed run leaves files local until the next sweep.
`sweep` uploads only new or changed files, so running it extra times is safe. For uploads that do
not depend on the agent, run the same `sweep --config` command on a schedule.

Re-running the install on a panel that already has `recovery_config.json` upgrades the code and
keeps the config. `init` alone (`fathom_recovery.py init --panel-name "<Panel>"`) writes just the two
files and never touches S3.

## Finding and restoring

```
python3 fathom_recovery.py list    --panel-name "My Panel" [--user someone] [--limit 20]
python3 fathom_recovery.py restore --recovery-path "<user>/My Panel/<run>/<file>" \
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

- A panel is only protected once something calls the helper: its generation code, its agent following
  `RECOVERY_INSTRUCTIONS.md`, or a schedule.
- Case and extra spaces in the panel name don't matter (v1.4.0+), but a typo still creates a new folder.
- The helper never deletes anything. Retention/cleanup, if wanted, should be an S3 lifecycle rule on `6_Recovery/`.
- Needs AWS credentials with `s3:GetObject` and `s3:PutObject` on the source and recovery locations, and `s3:ListBucket` for `list`.
  For `upload` only the recovery prefix is needed: `s3:PutObject`, `s3:GetObject` (read-back check) and `s3:ListBucket`
  on `6_Recovery/*`. Run `selfcheck --apply` in each panel to confirm; AccessDenied means that panel has no access yet.
- The `<user>` folder is matched ignoring case (`resolve_user_folder()`): `nbhan` uses an existing `NBHAN`.
  An exact spelling always wins; if only different-case folders exist and there are several (`NBHAN` and `NbHan`),
  the call fails and the exact spelling must be used. If the listing is not permitted, the name is used as given
  with a warning. Let `resolve_user()` pick the user (or set `RECOVERY_USER`) instead of hardcoding one.
- `fathom_recovery.py` contains the line `LAYOUT = "user/panel"`. A panel can check which layout its copy
  writes with `grep -c 'LAYOUT = "user/panel"' scripts/fathom_recovery.py` (1 = current, 0 = older panel-first).

## Registered panel names

Spelling matters except for case and extra spaces. Add a row before a panel starts uploading.

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
python3 sync_recovery.py --repo <git url> --ref v1.6.1 --dest scripts --run-tests --panel-name "<Panel>"
```
(`sync_recovery.py` itself can be run again and again; only a plain `git clone` into an existing folder
fails, so clone into a new or versioned folder name.)
Prints the installed version, commit and sha256. Pin a tag, not `main`.
To upgrade a panel, rerun with the new tag.

**Verify in the panel:**

```
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>"           # read-only
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>" --apply   # writes 1 probe file, verifies size + sha256
```
`selfcheck --apply` leaves a small `selfcheck_probe.txt` in the panel's recovery folder (nothing is ever deleted).
