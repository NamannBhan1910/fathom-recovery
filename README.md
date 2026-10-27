# fathom-recovery

One small, dependency-light module that gives **any Fathom panel** a recovery copy of the
files it generates. Panels live in different conversations/projects, so the repo is designed
to be copied into (or pulled into) each one without any panel-specific code.

## Where files go

```
s3://<RECOVERY_DESTINATION_BUCKET>/6_Recovery/<Panel Name>/<user>/<YYYYMMDDTHHMMSSZ>-<8 hex>/<filename>
```

| Part | Source |
|---|---|
| `<Panel Name>` | **Required.** Given by the user at execution. Exact match: case and spaces matter. A new name creates a new panel folder. |
| `<user>` | `--user` / `user=` > `RECOVERY_USER` env > `bridge.whoami()` > `unknown-user` |
| `<timestamp>-<8 hex>` | UTC time of the copy + random suffix. Unique per copy, so **nothing is overwritten**; every version is kept. |

Each recovery object carries `sha256` metadata of the source bytes. The helper checks the copied size
against the source and reports `ok: false` on mismatch. Panel names are sanitised (slashes can't
create extra folders; blank, `.` and `..` are rejected) **before** any write.

Set `RECOVERY_DESTINATION_BUCKET` in each panel (for the current project, `mosaic-derivatives-margining`) or pass `--destination-bucket` / `destination_bucket=`. There is no bucket default. The prefix defaults to `6_Recovery/` and can be changed with `RECOVERY_DESTINATION_PREFIX`.

## Repo layout

```
fathom-recovery/
  fathom_recovery.py        # the whole library + CLI (single file, needs only boto3)
  panel_snippet.py          # paste-in block for a panel's generation code
  sync_recovery.py          # install a pinned release into another panel
  publish_personal_repo.py  # create/push the initial private personal GitHub repo
  tests/test_fathom_recovery.py   # offline tests, fake S3, no network
  README.md
```

## Using it in a panel (rollout)

1. **Get the file into the panel workspace.** Copy `fathom_recovery.py` into the panel's
   `scripts/` folder (or push this folder to your git remote and pull it in each panel's
   workspace; that is the way to keep one version across projects).
2. **Call it where the panel creates its output** (see `panel_snippet.py`):

```python
from fathom_recovery import put_object_and_copy_to_recovery

put_object_and_copy_to_recovery(
    source_bucket, source_key, csv_bytes,
    panel_name=PANEL_NAME,          # the name the user gave
    content_type="text/csv",
)
```
   Set `RECOVERY_DESTINATION_BUCKET` in the panel's runtime before calling the helper.
   Already-created object? Use `copy_created_object_to_recovery(bucket, key, panel_name=...)`.
3. **Check it** with a dry run, then `--apply` once:

```
python3 fathom_recovery.py copy --panel-name "My Panel" --source-bucket B --source-key K          # dry run
python3 fathom_recovery.py copy --panel-name "My Panel" --source-bucket B --source-key K --apply
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

## Sharing across panels (git)

**First publication (personal GitHub account):** In your VS Code terminal,
run `python <path-to-this-repo>/publish_personal_repo.py`. Enter a GitHub token at the masked prompt.
The script verifies the account is `NamannBhan1910`, creates a **private** repository
if needed, then pushes `main` and `v1.2.0`. The token is not saved. It needs permission
to create a private repository and push code. When ready for company ownership, transfer
the repository in GitHub and update each panel's `--repo` URL.

**Publish (owner, once per release):** commit, tag (`git tag -a v1.2.0 -m v1.2.0`), and
`git push origin main v1.2.0` to the shared private repo. Give panel owners read access
(or a read-only deploy token).

**Install in a panel:**

```
# Configure the panel's git credentials or set FATHOM_RECOVERY_GIT_TOKEN
# as a runtime secret for a private HTTPS repo. Never put it in the URL.
python3 sync_recovery.py --repo <git url> --ref v1.2.0 --dest scripts --run-tests
```
For Git hosts that require a different HTTPS username, set `FATHOM_RECOVERY_GIT_USERNAME` too. The token is passed through the Git process environment, not command arguments or the repository URL. Restrict the token to read access for the repo.
Prints the installed version, commit and sha256. Pin a tag, not `main`.
To upgrade a panel, rerun with the new tag.

**Verify in the panel:**

```
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>"           # read-only
python3 scripts/fathom_recovery.py selfcheck --panel-name "<Panel>" --apply   # writes 1 probe file, verifies size + sha256
```
These commands require `RECOVERY_DESTINATION_BUCKET` to be set (or `--destination-bucket` on the command).
`selfcheck --apply` leaves a small `selfcheck_probe.txt` in the panel's recovery folder (nothing is ever deleted).
