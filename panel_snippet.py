"""Paste-in block for a panel's generation code.

Recovery failures are logged and raised so the panel can handle them explicitly.
Set PANEL_NAME to the exact name the user gave (case and spaces matter).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))  # where fathom_recovery.py lives
from fathom_recovery import copy_created_object_to_recovery, put_object_and_copy_to_recovery

PANEL_NAME = "<Panel Name given by the user>"


def save_with_recovery(bucket: str, key: str, data: bytes | str, content_type: str | None = None) -> dict:
    """Write the panel's file, then copy it to 6_Recovery/<PANEL_NAME>/..."""
    try:
        res = put_object_and_copy_to_recovery(bucket, key, data, panel_name=PANEL_NAME, content_type=content_type)
    except Exception as exc:  # noqa: BLE001 - report, don't hide
        print(f"RECOVERY FAILED for {os.path.basename(key)}: {type(exc).__name__}: {exc}")
        raise
    if not res["ok"]:
        print(f"RECOVERY WARNING for {res['filename']}: copy not verified")
    else:
        print(f"Recovery copy OK: {res['copy_result']['recovery_path']}")
    return res


def backup_existing(bucket: str, key: str) -> dict:
    """Recovery copy of an object another step already created."""
    return copy_created_object_to_recovery(bucket, key, panel_name=PANEL_NAME)
