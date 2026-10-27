"""Paste-in block for a panel's generation code (fathom_recovery >= 1.2.0).

Set PANEL_NAME to the exact name the user gave (case and spaces matter).
Files land in:  6_Recovery/<PANEL_NAME>/<user>/<timestamp>-<8 hex>/<filename>

`upload_to_recovery` is the simplest way to push a panel's output: bytes, text or a local
file go straight to the panel's recovery folder and are verified (size + sha256).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))  # where fathom_recovery.py lives
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


def sweep_now(directory: str = ".", patterns=("*.csv", "*.xlsx", "*.json", "*.parquet")) -> dict:
    """Upload every NEW or CHANGED file under `directory` (no per-file calls needed).

    Call this once at the end of the panel's generation code. Files already uploaded and
    unchanged are skipped, so it is safe to call after every run.
    """
    from fathom_recovery import sweep

    res = sweep(directory, PANEL_NAME, patterns=patterns, apply=True)
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
