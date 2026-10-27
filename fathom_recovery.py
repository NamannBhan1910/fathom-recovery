#!/usr/bin/env python3
"""fathom_recovery - per-panel recovery copies for Fathom panels (single file, drop-in).

Copy this one file into any panel workspace (needs only boto3) and call it from
that panel's generation code. Every panel writes to its own folder:

    s3://<bucket>/6_Recovery/<Panel Name>/<user>/<YYYYMMDDTHHMMSSZ>-<8 hex>/<filename>

* <Panel Name>  REQUIRED. Supplied by the user / caller at execution time.
                Matching is exact (case and spaces matter).
* <user>        explicit arg > RECOVERY_USER env > bridge.whoami() > "unknown-user"
* timestamp-id  UTC time of the copy + random 8 hex. Unique per copy: nothing is
                ever overwritten, every version is kept.
* Every recovery object gets `sha256` metadata (hash of the source bytes).

Defaults (override with env vars RECOVERY_DESTINATION_BUCKET / _PREFIX):
    bucket = mosaic-derivatives-margining, prefix = 6_Recovery/

Library use
-----------
    from fathom_recovery import put_object_and_copy_to_recovery, copy_created_object_to_recovery

    from fathom_recovery import upload_to_recovery
    from pathlib import Path

    # simplest: push data (bytes / str content / Path to a local file) straight to recovery
    upload_to_recovery(csv_bytes, "report.csv", panel_name="Daily Checklist Brokerage")
    upload_to_recovery(Path("report.csv"), panel_name="Daily Checklist Brokerage")

    put_object_and_copy_to_recovery(bucket, key, csv_bytes,
                                    panel_name="Daily Checklist Brokerage",
                                    content_type="text/csv")
    copy_created_object_to_recovery(bucket, key, panel_name="Daily Checklist Brokerage")

CLI (dry run unless --apply is given)
-------------------------------------
    python3 fathom_recovery.py copy    --panel-name "<Panel>" --source-bucket B --source-key K [--user U] [--apply]
    python3 fathom_recovery.py upload  --panel-name "<Panel>" --file local.csv [--filename N] [--user U] [--apply]
    python3 fathom_recovery.py sweep   --panel-name "<Panel>" [--dir .] [--pattern "*.csv"] [--apply]
    python3 fathom_recovery.py list    --panel-name "<Panel>" [--user U]
    python3 fathom_recovery.py restore --recovery-path "<Panel>/<user>/<run>/<file>" \
                                       --target-bucket B --target-key K [--overwrite] [--apply]

Output never prints bucket names (they are redacted).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

__version__ = "1.3.2"

DESTINATION_BUCKET = os.environ.get("RECOVERY_DESTINATION_BUCKET", "mosaic-derivatives-margining")
DESTINATION_PREFIX = os.environ.get("RECOVERY_DESTINATION_PREFIX", "6_Recovery/")
FALLBACK_USER = "unknown-user"


# ------------------------------------------------------------------ helpers
def _clean_prefix(prefix: str) -> str:
    cleaned = (prefix or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def _redact(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[:3]}...{value[-3:]}" if len(value) > 8 else "<redacted>"


def _safe_segment(value: Any, what: str) -> str:
    """Make a value safe to use as ONE S3 path segment (keeps spaces and case).

    Slashes / backslashes / control characters become spaces, so a name can
    never create extra folders or escape its own. Blank, '.' and '..' are rejected.
    """
    seg = re.sub(r"[\\/\x00-\x1f]+", " ", str(value or "")).strip()
    seg = re.sub(r"\s+", " ", seg).strip(" .")
    if not seg or seg in {".", ".."}:
        raise ValueError(f"{what} is required")
    if what == "panel_name" and re.search(r"[<>]", seg):
        raise ValueError("panel_name still looks like a placeholder (contains < or >); set the real panel name")
    return seg


def resolve_user(user: str | None = None) -> str:
    """explicit arg > RECOVERY_USER env > Fathom identity (bridge.whoami) > unknown-user."""
    candidate = (user or os.environ.get("RECOVERY_USER") or "").strip()
    if not candidate:
        try:
            from bridge import whoami  # only available inside a Fathom panel

            info = whoami()
            if isinstance(info, dict):
                candidate = str(info.get("username") or info.get("email") or "").strip()
        except Exception:
            candidate = ""
    if "@" in candidate:
        candidate = candidate.split("@")[0]
    try:
        return _safe_segment(candidate, "user")
    except ValueError:
        return FALLBACK_USER


def make_run_folder(now: datetime | None = None) -> str:
    """'<YYYYMMDDTHHMMSSZ>-<8 hex>', e.g. 20261027T144507Z-58d755e8."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def build_destination_key(
    source_key: str,
    panel_name: str,
    user: str | None = None,
    destination_prefix: str = DESTINATION_PREFIX,
    now: datetime | None = None,
) -> str:
    filename = posixpath.basename(source_key or "")
    if not filename:
        raise ValueError("source_key must include a filename")
    panel = _safe_segment(panel_name, "panel_name")
    return f"{_clean_prefix(destination_prefix)}{panel}/{resolve_user(user)}/{make_run_folder(now)}/{filename}"


def _is_not_found(exc: ClientError) -> bool:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = exc.response.get("Error", {}).get("Code")
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


def _head_object(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    resp = s3.head_object(Bucket=bucket, Key=key)
    lm = resp.get("LastModified")
    return {
        "filename": posixpath.basename(key),
        "exists": True,
        "size_bytes": resp.get("ContentLength"),
        "content_type": resp.get("ContentType"),
        "metadata": resp.get("Metadata") or {},
        "last_modified_utc": lm.astimezone(timezone.utc).isoformat() if lm else None,
    }


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise


def _sha256_of_object(s3: Any, bucket: str, key: str) -> str:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    h = hashlib.sha256()
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def _require_panel(panel_name: Any) -> str:
    return _safe_segment(panel_name, "panel_name")


# ----------------------------------------------------------------- backup
def copy_created_object_to_recovery(
    source_bucket: str,
    source_key: str,
    *,
    panel_name: str,
    user: str | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    dry_run: bool = False,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Copy an already-created S3 object into the panel's recovery folder."""
    if not source_bucket:
        raise ValueError("source_bucket is required")
    if not source_key:
        raise ValueError("source_key is required")
    if not destination_bucket:
        raise ValueError("destination_bucket is required")
    _require_panel(panel_name)

    client = s3 or boto3.client("s3")
    filename = posixpath.basename(source_key)
    dest_key = build_destination_key(source_key, panel_name, user, destination_prefix)
    relative_dest = dest_key[len(_clean_prefix(destination_prefix)):]

    source_meta = _head_object(client, source_bucket, source_key)

    if dry_run:
        return {
            "ok": True,
            "action": "would_copy",
            "filename": filename,
            "recovery_path": relative_dest,
            "source_verified": True,
            "source_size_bytes": source_meta["size_bytes"],
            "dry_run": True,
        }

    sha256 = _sha256_of_object(client, source_bucket, source_key)
    content_type = mimetypes.guess_type(dest_key)[0] or source_meta.get("content_type") or "application/octet-stream"
    client.copy_object(
        Bucket=destination_bucket,
        Key=dest_key,
        CopySource={"Bucket": source_bucket, "Key": source_key},
        ContentType=content_type,
        Metadata={"sha256": sha256},
        MetadataDirective="REPLACE",
    )
    dest_meta = _head_object(client, destination_bucket, dest_key)
    size_ok = dest_meta["size_bytes"] == source_meta["size_bytes"]
    return {
        "ok": bool(dest_meta["exists"]) and size_ok,
        "action": "copied",
        "filename": filename,
        "recovery_path": relative_dest,
        "source_verified": True,
        "destination_verified": bool(dest_meta["exists"]),
        "source_size_bytes": source_meta["size_bytes"],
        "destination_size_bytes": dest_meta["size_bytes"],
        "sha256": sha256,
        "dry_run": False,
    }


def put_object_and_copy_to_recovery(
    source_bucket: str,
    source_key: str,
    body: bytes | str,
    *,
    panel_name: str,
    user: str | None = None,
    content_type: str | None = None,
    metadata: dict[str, str] | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    dry_run_copy: bool = False,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Create the source object, then immediately copy that exact object to recovery.

    panel_name is validated BEFORE anything is written, so a bad name never
    leaves a source object without a recovery copy.
    """
    _require_panel(panel_name)
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    client = s3 or boto3.client("s3")
    client.put_object(
        Bucket=source_bucket,
        Key=source_key,
        Body=body_bytes,
        ContentType=content_type or mimetypes.guess_type(source_key)[0] or "application/octet-stream",
        Metadata=metadata or {},
    )
    copy_result = copy_created_object_to_recovery(
        source_bucket,
        source_key,
        panel_name=panel_name,
        user=user,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
        dry_run=dry_run_copy,
        s3=client,
    )
    return {
        "ok": bool(copy_result.get("ok")),
        "filename": posixpath.basename(source_key),
        "created_source_object": True,
        "source_size_bytes": len(body_bytes),
        "copy_result": copy_result,
    }


# ------------------------------------------------------------------ upload
def upload_to_recovery(
    data: bytes | str | os.PathLike,
    filename: str | None = None,
    *,
    panel_name: str,
    user: str | None = None,
    content_type: str | None = None,
    dry_run: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Store data directly in the panel's recovery folder (no source object needed).

    data      bytes, str (utf-8) or a path to a local file (os.PathLike).
              NOTE: a plain str is treated as CONTENT; pass pathlib.Path for a file.
    filename  name stored in S3; defaults to the local file's name when data is a path.

    Writes <prefix><panel>/<user>/<timestamp>-<8 hex>/<filename> with sha256 metadata,
    then verifies size, stored sha256 and the content hash read back from S3.
    panel_name and filename are validated BEFORE anything is written.
    """
    _require_panel(panel_name)
    if isinstance(data, os.PathLike):
        with open(data, "rb") as fh:
            body = fh.read()
        filename = filename or os.path.basename(os.fspath(data))
    elif isinstance(data, str):
        body = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        body = bytes(data)
    else:
        raise TypeError("data must be bytes, str or a pathlib.Path")
    name = posixpath.basename((filename or "").replace("\\", "/"))
    if not name or name in {".", ".."}:
        raise ValueError("filename is required")
    if not destination_bucket:
        raise ValueError("destination_bucket is required")

    key = build_destination_key(name, panel_name, user, destination_prefix)
    rel = key[len(_clean_prefix(destination_prefix)):]
    sha = hashlib.sha256(body).hexdigest()
    base = {"filename": name, "recovery_path": rel, "size_bytes": len(body), "sha256": sha}
    if dry_run:
        return {**base, "ok": True, "action": "would_upload", "dry_run": True}

    client = s3 or boto3.client("s3")
    client.put_object(
        Bucket=destination_bucket, Key=key, Body=body,
        ContentType=content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
        Metadata={"sha256": sha},
    )
    head = _head_object(client, destination_bucket, key)
    size_ok = head["size_bytes"] == len(body)
    meta_ok = head["metadata"].get("sha256") == sha
    content_ok = _sha256_of_object(client, destination_bucket, key) == sha
    return {
        **base,
        "ok": size_ok and meta_ok and content_ok,
        "action": "uploaded",
        "size_verified": size_ok,
        "sha256_metadata_verified": meta_ok,
        "content_verified": content_ok,
        "dry_run": False,
    }


# ------------------------------------------------------------ list / restore
def list_recoveries(
    panel_name: str,
    *,
    user: str | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> list[dict[str, Any]]:
    """List recovery copies for one panel (read-only), newest first.

    Understands the standard layout (panel/user/run/file) and older layouts
    (panel/file, panel/user/file) - for those, user / run_id may be None.
    """
    panel = _require_panel(panel_name)
    client = s3 or boto3.client("s3")
    root = _clean_prefix(destination_prefix)
    prefix = f"{root}{panel}/" + (f"{_safe_segment(user, 'user')}/" if user else "")
    out: list[dict[str, Any]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=destination_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue  # folder marker
            rel = key[len(root):]
            parts = rel.split("/")[1:]  # drop panel
            if len(parts) >= 3:
                who, run_id, fname = parts[0], parts[1], "/".join(parts[2:])
            elif len(parts) == 2:
                who, run_id, fname = parts[0], None, parts[1]
            else:
                who, run_id, fname = None, None, parts[0]
            lm = obj.get("LastModified")
            out.append({
                "recovery_path": rel,
                "user": who,
                "run_id": run_id,
                "filename": fname,
                "size_bytes": obj.get("Size"),
                "last_modified_utc": lm.astimezone(timezone.utc).isoformat() if lm else None,
            })
    out.sort(key=lambda r: (r["last_modified_utc"] or "", r["recovery_path"]), reverse=True)
    return out


def restore_from_recovery(
    recovery_path: str,
    target_bucket: str,
    target_key: str,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Copy one recovery object back to a target bucket/key.

    Never overwrites an existing target unless overwrite=True. After a real
    restore the sha256 recorded at backup time is re-checked against the target.
    """
    if not target_bucket or not target_key:
        raise ValueError("target_bucket and target_key are required")
    segs = [s for s in (recovery_path or "").split("/")]
    if len(segs) < 2 or any(s in {"", ".", ".."} for s in segs):
        raise ValueError("recovery_path must look like '<Panel>/<user>/<run>/<file>' with no empty or '..' parts")

    client = s3 or boto3.client("s3")
    src_key = f"{_clean_prefix(destination_prefix)}{recovery_path}"
    src_meta = _head_object(client, destination_bucket, src_key)
    exists = _object_exists(client, target_bucket, target_key)
    base = {
        "filename": posixpath.basename(recovery_path),
        "recovery_path": recovery_path,
        "source_size_bytes": src_meta["size_bytes"],
        "target_exists": exists,
    }
    if exists and not overwrite:
        return {**base, "ok": False, "action": "refused", "reason": "target_exists (use overwrite)", "dry_run": dry_run}
    if dry_run:
        return {**base, "ok": True, "action": "would_restore", "dry_run": True}

    client.copy_object(
        Bucket=target_bucket,
        Key=target_key,
        CopySource={"Bucket": destination_bucket, "Key": src_key},
        MetadataDirective="COPY",
    )
    tgt_meta = _head_object(client, target_bucket, target_key)
    size_ok = tgt_meta["size_bytes"] == src_meta["size_bytes"]
    expected = src_meta["metadata"].get("sha256")
    hash_ok = True if not expected else _sha256_of_object(client, target_bucket, target_key) == expected
    return {
        **base,
        "ok": size_ok and hash_ok,
        "action": "restored",
        "size_verified": size_ok,
        "sha256_verified": hash_ok if expected else None,
        "dry_run": False,
    }


# ---------------------------------------------------------------- selfcheck
def selfcheck(
    panel_name: str,
    *,
    user: str | None = None,
    apply: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """One-command health check for a panel. Read-only unless apply=True.

    Steps: panel name valid -> can list the panel folder -> (apply) write a tiny
    probe file and verify size + sha256 -> (apply) list sees the probe.
    The probe is written to <panel>/<user>/<run>/selfcheck_probe.txt and is never deleted.
    """
    steps: list[dict[str, Any]] = []

    def run(name: str, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
            return True
        except Exception as exc:  # noqa: BLE001 - report every failure
            steps.append({"step": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            return False

    client = s3
    state: dict[str, Any] = {}

    def s_name():
        state["key"] = build_destination_key("selfcheck_probe.txt", panel_name, user, destination_prefix)
        return "layout: " + state["key"][len(_clean_prefix(destination_prefix)):]

    def s_list():
        nonlocal client
        client = client or boto3.client("s3")
        state["before"] = len(list_recoveries(panel_name, destination_bucket=destination_bucket,
                                              destination_prefix=destination_prefix, s3=client))
        return f"list ok, {state['before']} existing object(s)"

    def s_write():
        data = f"fathom_recovery selfcheck {datetime.now(timezone.utc).isoformat()}\n".encode()
        sha = hashlib.sha256(data).hexdigest()
        client.put_object(Bucket=destination_bucket, Key=state["key"], Body=data,
                          ContentType="text/plain", Metadata={"sha256": sha})
        head = _head_object(client, destination_bucket, state["key"])
        if head["size_bytes"] != len(data) or head["metadata"].get("sha256") != sha:
            raise RuntimeError("size/sha256 mismatch after write")
        if _sha256_of_object(client, destination_bucket, state["key"]) != sha:
            raise RuntimeError("content hash mismatch after write")
        return f"wrote and verified {len(data)} bytes"

    def s_relist():
        n = len(list_recoveries(panel_name, destination_bucket=destination_bucket,
                                destination_prefix=destination_prefix, s3=client))
        if n != state["before"] + 1:
            raise RuntimeError(f"expected {state['before'] + 1} objects, saw {n}")
        return f"list now shows {n} object(s)"

    ok = run("panel_name_and_layout", s_name) and run("list_access", s_list)
    if ok and apply:
        ok = run("write_and_verify_probe", s_write) and run("list_sees_probe", s_relist)
    elif ok:
        steps.append({"step": "write_and_verify_probe", "ok": True, "detail": "skipped (dry run; add --apply)"})
    return {"ok": all(s["ok"] for s in steps), "action": "selfcheck", "applied": apply, "steps": steps}


# ------------------------------------------------------------------- sweep
SWEEP_STATE_NAME = ".fathom_recovery_state.json"
SWEEP_DEFAULT_PATTERNS = ("*.csv", "*.xlsx", "*.xls", "*.json", "*.parquet", "*.txt")
_SWEEP_SKIP_DIRS = {".git", ".fathom_runtime", "__pycache__", "knowledge", "node_modules", ".venv", "tests"}


def _load_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def sweep(
    directory: str | os.PathLike,
    panel_name: str,
    *,
    patterns: tuple[str, ...] | list[str] = SWEEP_DEFAULT_PATTERNS,
    recursive: bool = True,
    state_path: str | os.PathLike | None = None,
    user: str | None = None,
    apply: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Upload files in `directory` that are new or changed since the last sweep.

    No edits to the panel's generation code are needed: run this after (or on a schedule
    alongside) the code that generates files. A small JSON state file (path -> sha256) records
    what was already uploaded, so unchanged files are never uploaded twice. A file is recorded
    only after its upload verified OK, so failures are retried on the next sweep.
    Dry run unless apply=True (nothing uploaded, state untouched).
    """
    _require_panel(panel_name)
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError("directory does not exist")
    state_file = Path(state_path).resolve() if state_path else root / SWEEP_STATE_NAME
    state = _load_state(state_file)
    pats = list(patterns) or list(SWEEP_DEFAULT_PATTERNS)

    found = sorted(root.rglob("*") if recursive else root.glob("*"))
    client = s3
    files: list[dict[str, Any]] = []
    counts = {"scanned": 0, "uploaded": 0, "would_upload": 0, "unchanged": 0, "failed": 0}
    for p in found:
        if not p.is_file() or p.resolve() == state_file or p.name == SWEEP_STATE_NAME or p.name.endswith(".tmp"):
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _SWEEP_SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if not any(fnmatch.fnmatch(p.name, pat) for pat in pats):
            continue
        counts["scanned"] += 1
        rel = "/".join(rel_parts)
        try:
            data = p.read_bytes()  # read once: the hash and the upload use the same bytes
        except OSError as exc:
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        if state.get(rel) == sha:
            counts["unchanged"] += 1
            files.append({"file": rel, "action": "unchanged"})
            continue
        if not apply:
            counts["would_upload"] += 1
            files.append({"file": rel, "action": "would_upload", "size_bytes": len(data)})
            continue
        try:
            client = client or boto3.client("s3")
            res = upload_to_recovery(
                data, p.name, panel_name=panel_name, user=user, s3=client,
                destination_bucket=destination_bucket, destination_prefix=destination_prefix,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sweep
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if res["ok"]:
            state[rel] = sha
            counts["uploaded"] += 1
            files.append({"file": rel, "action": "uploaded", "recovery_path": res["recovery_path"],
                          "size_bytes": res["size_bytes"]})
        else:
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": "upload not verified"})
    if apply and counts["uploaded"]:
        _save_state(state_file, state)
    return {"ok": counts["failed"] == 0, "action": "sweep", "applied": apply, **counts, "files": files}



# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fathom per-panel recovery copies")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--destination-bucket", default=DESTINATION_BUCKET)
        sp.add_argument("--destination-prefix", default=DESTINATION_PREFIX)

    c = sub.add_parser("copy", help="copy an existing object into the panel's recovery folder")
    c.add_argument("--panel-name", required=True, help='e.g. "Daily Checklist Brokerage"')
    c.add_argument("--source-bucket", required=True)
    c.add_argument("--source-key", required=True)
    c.add_argument("--user", default=None)
    c.add_argument("--apply", action="store_true", help="actually copy (default is dry run)")
    common(c)

    ls = sub.add_parser("list", help="list recovery copies for a panel (read-only)")
    ls.add_argument("--panel-name", required=True)
    ls.add_argument("--user", default=None)
    ls.add_argument("--limit", type=int, default=50)
    common(ls)

    r = sub.add_parser("restore", help="restore one recovery object to a target bucket/key")
    r.add_argument("--recovery-path", required=True, help="path from 'list', relative to the recovery root")
    r.add_argument("--target-bucket", required=True)
    r.add_argument("--target-key", required=True)
    r.add_argument("--overwrite", action="store_true")
    r.add_argument("--apply", action="store_true", help="actually restore (default is dry run)")
    common(r)

    u = sub.add_parser("upload", help="upload a local file into the panel's recovery folder")
    u.add_argument("--panel-name", required=True)
    u.add_argument("--file", required=True, help="local file to upload")
    u.add_argument("--filename", default=None, help="name to store (default: the local file name)")
    u.add_argument("--user", default=None)
    u.add_argument("--apply", action="store_true", help="actually upload (default is dry run)")
    common(u)

    sw = sub.add_parser("sweep", help="upload new/changed files in a folder (dry run unless --apply)")
    sw.add_argument("--panel-name", required=True)
    sw.add_argument("--dir", default=".", help="folder to scan (default: current folder)")
    sw.add_argument("--pattern", action="append", default=None,
                    help="glob such as *.csv (repeatable; default: csv, xlsx, xls, json, parquet, txt)")
    sw.add_argument("--no-recursive", action="store_true")
    sw.add_argument("--state-file", default=None, help="where to remember uploaded files")
    sw.add_argument("--user", default=None)
    sw.add_argument("--apply", action="store_true", help="actually upload (default is dry run)")
    common(sw)

    sc = sub.add_parser("selfcheck", help="health check for a panel (read-only unless --apply)")
    sc.add_argument("--panel-name", required=True)
    sc.add_argument("--user", default=None)
    sc.add_argument("--apply", action="store_true", help="also write + verify one small probe file")
    common(sc)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "copy":
            result = copy_created_object_to_recovery(
                args.source_bucket, args.source_key, panel_name=args.panel_name, user=args.user,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
                dry_run=not args.apply,
            )
            result["source_bucket_redacted"] = _redact(args.source_bucket)
        elif args.cmd == "list":
            rows = list_recoveries(
                args.panel_name, user=args.user,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
            )
            result = {"ok": True, "panel": args.panel_name, "total": len(rows), "shown": rows[: args.limit]}
        elif args.cmd == "upload":
            from pathlib import Path

            result = upload_to_recovery(
                Path(args.file), args.filename, panel_name=args.panel_name, user=args.user,
                dry_run=not args.apply,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
            )
        elif args.cmd == "sweep":
            result = sweep(
                args.dir, args.panel_name, patterns=args.pattern or SWEEP_DEFAULT_PATTERNS,
                recursive=not args.no_recursive, state_path=args.state_file, user=args.user,
                apply=args.apply, destination_bucket=args.destination_bucket,
                destination_prefix=args.destination_prefix,
            )
        elif args.cmd == "selfcheck":
            result = selfcheck(
                args.panel_name, user=args.user, apply=args.apply,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
            )
        else:
            result = restore_from_recovery(
                args.recovery_path, args.target_bucket, args.target_key, overwrite=args.overwrite,
                dry_run=not args.apply, destination_bucket=args.destination_bucket,
                destination_prefix=args.destination_prefix,
            )
            result["target_bucket_redacted"] = _redact(args.target_bucket)
        result["destination_bucket_redacted"] = _redact(args.destination_bucket)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    except (ClientError, BotoCoreError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
