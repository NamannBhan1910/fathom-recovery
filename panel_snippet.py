"""Paste-in block for a panel's generation code (fathom_recovery >= 1.2.0).

Set PANEL_NAME to the exact name the user gave (case and spaces matter).
Files land in:  6_Recovery/<user>/<PANEL_NAME>/<timestamp>-<8 hex>/<filename>

Two ways to use it (neither runs by itself; the panel's code must call one):
  push(data, filename)  - upload ONE file right when it is created
  sweep_now(folder)     - upload every NEW or CHANGED file in a folder; call it once at the
                          end of the generation step, after the files are fully written

`fathom_recovery.py` is looked up next to this file first, then in a `scripts/` folder next to
it (where sync_recovery.py installs it by default).
"""
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "scripts")):
    if os.path.isfile(os.path.join(_p, "fathom_recovery.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
from fathom_recovery import copy_created_object_to_recovery, upload_to_recovery

PANEL_NAME = "<Panel Name given by the user>"


def push(data, filename=None, content_type=None) -> dict:
    """data = bytes | str (content) | pathlib.Path (local file). Raises on failure, warns if unverified."""
    try:
        res = upload_to_recovery(data, filename, panel_name=PANEL_NAME, content_type=content_type)
    except Exception as exc:  # noqa: BLE001 - report, don't hide
        print(f"RECOVERY FAILED for {filename or data}: {type(exc).__name__}: {exc}")
        raise
    if res["ok"]:
        print(f"Recovery upload OK: {res['recovery_path']}")
    else:
        print(f"RECOVERY WARNING for {res['filename']}: upload not verified: {res}")
    return res


def sweep_now(directory: str = ".", patterns=None) -> dict:
    """Upload every NEW or CHANGED file under `directory` (no per-file calls needed).

    Call this once at the end of the panel's generation code. Files already uploaded and
    unchanged are skipped, so it is safe to call after every run.
    `patterns` defaults to the same list as the `sweep` command (csv, xlsx, xls, json, parquet, txt).
    """
    from fathom_recovery import SWEEP_DEFAULT_PATTERNS, sweep

    res = sweep(directory, PANEL_NAME, patterns=patterns or SWEEP_DEFAULT_PATTERNS, apply=True)
    print(f"Recovery sweep: uploaded={res['uploaded']} unchanged={res['unchanged']} failed={res['failed']}")
    for f in res["files"]:
        if f["action"] == "failed":
            print(f"RECOVERY FAILED for {f['file']}: {f.get('error')}")
    return res


def backup_existing(bucket: str, key: str) -> dict:
    """Recovery copy of an object another step already created in S3."""
    return copy_created_object_to_recovery(bucket, key, panel_name=PANEL_NAME)


# Examples:
#   push(csv_bytes, "report.csv")
#   push(Path("report.csv"))
#   sweep_now("outputs")
