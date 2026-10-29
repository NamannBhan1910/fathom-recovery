#!/usr/bin/env python3
"""fathom_recovery - per-panel recovery copies for Fathom panels (single file, drop-in).

Copy this one file into any panel workspace (needs only boto3) and call it from
that panel's generation code. Every panel writes to its own folder:

    s3://<bucket>/6_Recovery/<user>/<Panel Name>/<YYYYMMDDTHHMMSSZ>-<8 hex>/<filename>

* <user>        explicit arg > RECOVERY_USER env > bridge.whoami() > "unknown-user".
                Files are segregated per user first, then per panel. Matched to an existing
                user folder ignoring case ('nbhan' uses 'NBHAN'); exact spelling wins.
* <Panel Name>  REQUIRED. Supplied by the user / caller at execution time.
                Matched to an existing folder of THAT user ignoring case and extra spaces.
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
    python3 fathom_recovery.py sweep   --config recovery_config.json [--apply]   # panel/folder from the config
    python3 fathom_recovery.py list    --panel-name "<Panel>" [--user U]
    python3 fathom_recovery.py restore --recovery-path "<user>/<Panel>/<run>/<file>" \
                                       --target-bucket B --target-key K [--overwrite] [--apply]

Code / knowledge backup (v1.7.0)
--------------------------------
    python3 fathom_recovery.py code-sweep    --config recovery_config.json [--apply]
    python3 fathom_recovery.py restore-code  --config recovery_config.json [--run R] [--target-dir D] [--apply]

`code-sweep` scans the WHOLE workspace (py, ipynb, md, json, js, html, sql, yaml, ... including
knowledge/) and uploads new/changed files to
    <prefix><user>/<Panel>/code/<YYYY-MM-DD>/<relative/path/file>
with the workspace folder structure kept, plus a `.fathom_manifest.json`. ONE folder per UTC day: every
sweep that day writes into it (a file changed twice in one day overwrites that day's copy). State is
kept in .fathom_recovery/code_state.json. Hidden files/folders (.git, .fathom_runtime, ...), __pycache__,
node_modules and files over 10 MB are skipped; data files that the normal `sweep` already backs up
from outputs/ are not uploaded twice. It only reads files; nothing local or in S3 is ever deleted.
`restore-code` downloads a run's files into a NEW local folder and never overwrites unless --overwrite.

Output never prints bucket names (they are redacted).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import mimetypes
mimetypes.add_type("text/csv", ".csv", strict=True)
import os
import posixpath
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

__version__ = "1.7.2"
LAYOUT = "user/panel"  # marker: <prefix>/<user>/<panel>/<run>/<file>. Panels grep for this line.
CODE_BACKUP = "1"  # marker: this script has code-sweep / restore-code. Panels grep for this line.
REPO_URL = "https://github.com/NamannBhan1910/fathom-recovery.git"

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


def make_day_folder(now: datetime | None = None) -> str:
    """'<YYYY-MM-DD>' (UTC), e.g. 2026-10-27. One folder per day for the code backup."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%d")


def build_destination_key(
    source_key: str,
    panel_name: str,
    user: str | None = None,
    destination_prefix: str = DESTINATION_PREFIX,
    now: datetime | None = None,
    keep_path: bool = False,
) -> str:
    """keep_path=False stores only the file name; keep_path=True keeps the folder path inside the file name."""
    if keep_path:
        segs = (source_key or "").replace("\\", "/").split("/")
        if any(s in {"", ".", ".."} for s in segs):
            raise ValueError("relative path must have no empty, '.' or '..' parts")
        filename = "/".join(segs)
    else:
        filename = posixpath.basename(source_key or "")
    if not filename:
        raise ValueError("source_key must include a filename")
    panel = _safe_segment(panel_name, "panel_name")
    return f"{_clean_prefix(destination_prefix)}{resolve_user(user)}/{panel}/{make_run_folder(now)}/{filename}"


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


def _list_folders(client: Any, bucket: str, root: str) -> list[str]:
    """Names of the immediate sub-folders under `root` (read-only delimiter listing)."""
    names: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            names.append(cp["Prefix"][len(root):].rstrip("/"))
    return names


def _match_folder(wanted: str, names: list[str], what: str) -> str:
    """exact spelling > the single case/extra-space-insensitive match > the name as given.

    Several case-insensitive matches (and no exact one) raise ValueError: the exact spelling is needed.
    """
    if wanted in names:
        return wanted
    want = " ".join(wanted.split()).casefold()
    matches = sorted({n for n in names if " ".join(n.split()).casefold() == want})
    if len(matches) > 1:
        raise ValueError(f"{what} {wanted!r} matches several existing folders {matches}; use the exact spelling")
    return matches[0] if matches else wanted


def resolve_user_folder(
    user: str | None = None,
    *,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> str:
    """Return the user folder to use under the prefix, ignoring case ('nbhan' -> existing 'NBHAN').

    The user comes from resolve_user() (arg > RECOVERY_USER > bridge.whoami() > unknown-user), then is
    matched against the existing top-level folders (read-only listing):
      * exact spelling exists            -> use it (so an exact name always wins, even if 'nbhan' also exists)
      * exactly one case-insensitive hit -> use that folder's spelling
      * several case-insensitive hits    -> ValueError (ambiguous; pass the exact spelling)
      * no hit                           -> the name as given (the folder is created on first upload)
    """
    who = resolve_user(user)
    client = s3 or boto3.client("s3")
    root = _clean_prefix(destination_prefix)
    return _match_folder(who, _list_folders(client, destination_bucket, root), "user")


def resolve_panel_folder(
    panel_name: str,
    *,
    user: str | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> str:
    """Return the panel folder name to use inside THIS USER's folder, ignoring case and extra spaces.

    S3 keys are case-sensitive, so 'daily checklist equities' would otherwise create a second
    folder. The user folder is matched case-insensitively first (resolve_user_folder), then this
    lists the panel folders under <prefix><user>/ (read-only):
      * exact spelling exists            -> use it
      * exactly one case-insensitive hit -> use that folder's spelling
      * several case-insensitive hits    -> ValueError (ambiguous; use the exact spelling)
      * no hit                           -> the name as given (the folder is created on first upload)
    """
    panel = _require_panel(panel_name)
    client = s3 or boto3.client("s3")
    who = resolve_user_folder(user, destination_bucket=destination_bucket,
                              destination_prefix=destination_prefix, s3=client)
    root = f"{_clean_prefix(destination_prefix)}{who}/"
    return _match_folder(panel, _list_folders(client, destination_bucket, root), "panel name")


def _panel_folder(
    panel_name: str, client: Any | None, bucket: str, prefix: str, user: str | None = None
) -> tuple[str, str | None, str]:
    """Resolve (panel folder, warning, user folder) against what already exists in S3.

    A listing failure falls back to the names as given plus a warning, because a backup should not be
    blocked just because listing is not permitted. Ambiguity (ValueError) still raises. Callers must
    build the key with the returned user folder, so a 'nbhan' caller lands in the existing 'NBHAN' folder.
    """
    panel = _require_panel(panel_name)
    if client is None:
        return panel, "folders not matched against existing folders (no S3 client)", resolve_user(user)
    try:
        who = resolve_user_folder(user, destination_bucket=bucket, destination_prefix=prefix, s3=client)
        root = f"{_clean_prefix(prefix)}{who}/"
        return _match_folder(panel, _list_folders(client, bucket, root), "panel name"), None, who
    except (ClientError, BotoCoreError) as exc:
        return (panel, f"folders not matched against existing folders ({type(exc).__name__})",
                resolve_user(user))



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
    panel_folder, panel_warning, user_folder = _panel_folder(
        panel_name, client, destination_bucket, destination_prefix, user)
    dest_key = build_destination_key(source_key, panel_folder, user_folder, destination_prefix)
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
            "panel_folder": panel_folder,
            **({"warning": panel_warning} if panel_warning else {}),
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
        "panel_folder": panel_folder,
        **({"warning": panel_warning} if panel_warning else {}),
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
    match_existing_folder: bool = True,
    relative_path: str | None = None,
) -> dict[str, Any]:
    """Store data directly in the panel's recovery folder (no source object needed).

    relative_path  keeps the file's workspace folder path: 'sub/a.csv' lands in <run>/sub/a.csv
                   instead of <run>/a.csv.

    The panel name is matched to an existing folder ignoring case and extra spaces (see
    resolve_panel_folder). match_existing_folder=False skips that lookup (sweep resolves once).

    data      bytes, str (utf-8) or a path to a local file (os.PathLike).
              NOTE: a plain str is treated as CONTENT; pass pathlib.Path for a file.
    filename  name stored in S3; defaults to the local file's name when data is a path.

    Writes <prefix><user>/<panel>/<timestamp>-<8 hex>/<filename> with sha256 metadata,
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

    panel_folder, panel_warning, user_folder = _require_panel(panel_name), None, resolve_user(user)
    client = s3
    if match_existing_folder and (not dry_run or s3 is not None):
        client = s3 or boto3.client("s3")
        panel_folder, panel_warning, user_folder = _panel_folder(
            panel_name, client, destination_bucket, destination_prefix, user)
    key = build_destination_key(relative_path or name, panel_folder, user_folder, destination_prefix,
                                keep_path=bool(relative_path))
    rel = key[len(_clean_prefix(destination_prefix)):]
    sha = hashlib.sha256(body).hexdigest()
    base = {"filename": name, "recovery_path": rel, "size_bytes": len(body), "sha256": sha,
            "panel_folder": panel_folder, **({"warning": panel_warning} if panel_warning else {})}
    if dry_run:
        return {**base, "ok": True, "action": "would_upload", "dry_run": True}

    client = client or boto3.client("s3")
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
    """List recovery copies for one user's panel (read-only), newest first.

    Layout: <prefix><user>/<panel>/<run>/<file>. `user` defaults to the current user (resolve_user),
    so a user only lists their own panel folder; pass user="other" to look at someone else's.
    Older objects written before v1.6.0 sit under <prefix><panel>/<user>/<run>/<file>; they are not
    mixed in here (see list_legacy_recoveries).
    """
    _require_panel(panel_name)
    client = s3 or boto3.client("s3")
    root = _clean_prefix(destination_prefix)
    who = resolve_user_folder(user, destination_bucket=destination_bucket,
                              destination_prefix=destination_prefix, s3=client)
    panel = resolve_panel_folder(panel_name, user=who, destination_bucket=destination_bucket,
                                 destination_prefix=destination_prefix, s3=client)
    prefix = f"{root}{who}/{panel}/"
    out: list[dict[str, Any]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=destination_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue  # folder marker
            rel = key[len(root):]
            parts = rel.split("/")[2:]  # drop user, panel
            if parts and parts[0] == CODE_FOLDER and len(parts) > 1:
                continue  # code/knowledge backups (v1.7.0) are listed by list_code(), not mixed into data runs
            if len(parts) >= 2:
                run_id, fname = parts[0], "/".join(parts[1:])
            else:
                run_id, fname = None, parts[0]
            lm = obj.get("LastModified")
            out.append({
                "recovery_path": rel,
                "user": who,
                "panel": panel,
                "run_id": run_id,
                "filename": fname,
                "size_bytes": obj.get("Size"),
                "last_modified_utc": lm.astimezone(timezone.utc).isoformat() if lm else None,
            })
    out.sort(key=lambda r: (r["last_modified_utc"] or "", r["recovery_path"]), reverse=True)
    return out


def list_legacy_recoveries(
    panel_name: str,
    *,
    user: str | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> list[dict[str, Any]]:
    """List objects in the OLD panel-first layout <prefix><panel>/<user>/<run>/<file> (read-only).

    Matches the panel folder ignoring case and spaces (exact listing of the top level). Optional `user`
    filter. Nothing is moved or renamed; this only lets old data be found and restored.
    """
    panel = _require_panel(panel_name)
    client = s3 or boto3.client("s3")
    root = _clean_prefix(destination_prefix)
    tops: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=destination_bucket, Prefix=root, Delimiter="/"):
        tops += [cp["Prefix"][len(root):].rstrip("/") for cp in page.get("CommonPrefixes", [])]
    want = panel.casefold()
    folders = [t for t in tops if " ".join(t.split()).casefold() == want]
    out: list[dict[str, Any]] = []
    for folder in folders:
        prefix = f"{root}{folder}/" + (f"{_safe_segment(user, 'user')}/" if user else "")
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=destination_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(root):]
                parts = rel.split("/")[1:]
                if len(parts) >= 3:
                    who, run_id, fname = parts[0], parts[1], "/".join(parts[2:])
                elif len(parts) == 2:
                    who, run_id, fname = parts[0], None, parts[1]
                else:
                    who, run_id, fname = None, None, parts[0]
                lm = obj.get("LastModified")
                out.append({"recovery_path": rel, "user": who, "panel": folder, "run_id": run_id,
                            "filename": fname, "size_bytes": obj.get("Size"),
                            "last_modified_utc": lm.astimezone(timezone.utc).isoformat() if lm else None})
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
        raise ValueError("recovery_path must look like '<user>/<Panel>/<run>/<file>' with no empty or '..' parts")

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
    The probe is written to <user>/<panel>/<run>/selfcheck_probe.txt and is never deleted.
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
        return "panel name is valid (folder is matched to existing spellings in the next step)"

    def s_list():
        nonlocal client
        client = client or boto3.client("s3")
        who = resolve_user_folder(user, destination_bucket=destination_bucket,
                                  destination_prefix=destination_prefix, s3=client)
        folder = resolve_panel_folder(panel_name, user=who, destination_bucket=destination_bucket,
                                      destination_prefix=destination_prefix, s3=client)
        state["key"] = build_destination_key("selfcheck_probe.txt", folder, who, destination_prefix)
        state["user"] = who
        state["before"] = len(list_recoveries(panel_name, user=who, destination_bucket=destination_bucket,
                                              destination_prefix=destination_prefix, s3=client))
        return (f"list ok, user folder {who!r}, panel folder {folder!r}, "
                f"{state['before']} existing object(s)")

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
        n = len(list_recoveries(panel_name, user=state["user"], destination_bucket=destination_bucket,
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
SWEEP_DEFAULT_PATTERNS = ("*.csv", "*.xlsx", "*.xls", "*.json", "*.parquet", "*.txt", "*.pdf")
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
    panel_folder = panel_name  # resolved once (case-insensitive) on the first real upload
    user_folder = user  # likewise: the existing user folder spelling is looked up once
    resolved = False
    files: list[dict[str, Any]] = []
    counts = {"scanned": 0, "uploaded": 0, "would_upload": 0, "unchanged": 0, "failed": 0}
    for p in found:
        if not p.is_file() or p.resolve() == state_file or p.name == SWEEP_STATE_NAME or p.name.endswith(".tmp"):
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _SWEEP_SKIP_DIRS for part in rel_parts[:-1]):
            continue
        low_name = p.name.lower()  # case-insensitive: REPORT.PDF matches *.pdf on Linux too
        if not any(fnmatch.fnmatchcase(low_name, pat.lower()) for pat in pats):
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
            if not resolved:
                panel_folder, _, user_folder = _panel_folder(
                    panel_name, client, destination_bucket, destination_prefix, user)
                resolved = True
            res = upload_to_recovery(
                data, p.name, panel_name=panel_folder, user=user_folder, s3=client,
                destination_bucket=destination_bucket, destination_prefix=destination_prefix,
                match_existing_folder=False, relative_path=rel,
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


# ------------------------------------------------- code + knowledge backup (v1.7.0)
# Separate from `sweep` (which backs up data files in outputs/ and is unchanged). The code sweep walks
# the WHOLE workspace and keeps folder paths:
#     <prefix><user>/<Panel>/code/<YYYY-MM-DD>/<relative/path/file>   (v1.7.1 and older: <timestamp>-<8 hex> runs)
# (v1.7.2) ONE folder per UTC day: all sweeps that day write into <YYYY-MM-DD>/ (only new/changed files),
# plus a manifest that is merged across the day's sweeps.
CODE_STATE_DIR = ".fathom_recovery"
CODE_STATE_NAME = "code_state.json"
CODE_FOLDER = "code"
CODE_DAY_KEY = ".fathom_day"  # state entry: the UTC day the hashes belong to (hidden name: never a real file)
CODE_MANIFEST_NAME = ".fathom_manifest.json"  # hidden name: can never collide with a swept workspace file
CODE_MAX_BYTES = 10 * 1024 * 1024
CODE_DEFAULT_PATTERNS = (
    "*.py", "*.ipynb", "*.md", "*.markdown", "*.json", "*.js", "*.jsx", "*.ts", "*.tsx", "*.html", "*.htm",
    "*.css", "*.sql", "*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg", "*.conf", "*.sh", "*.r", "*.txt",
    "*.pdf", "dockerfile", "makefile",
)
_CODE_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "site-packages"}
# Config-like files whose NAME suggests secrets are not uploaded (they are reported, so nothing is hidden).
_CODE_CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt"}
_CODE_SENSITIVE_WORDS = ("secret", "credential", "password", "passwd", "token", "private_key", "service_account",
                         "id_rsa", "apikey", "api_key")


def _is_sensitive_name(name: str) -> bool:
    low = name.lower()
    return os.path.splitext(low)[1] in _CODE_CONFIG_EXTS and any(w in low for w in _CODE_SENSITIVE_WORDS)


def _walk_code_files(root: Path, exclude_dirs: set[str]):
    """Yield workspace files, pruning hidden / generated folders before descending (never follows symlinks)."""
    skip = _CODE_SKIP_DIRS | exclude_dirs
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip)
        for fn in sorted(filenames):
            if not fn.startswith("."):
                yield Path(dirpath) / fn


def _put_verified(client: Any, bucket: str, key: str, body: bytes, sha: str, content_type: str | None = None) -> bool:
    """Write one object with sha256 metadata, then verify size, metadata and the content read back."""
    client.put_object(
        Bucket=bucket, Key=key, Body=body,
        ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
        Metadata={"sha256": sha},
    )
    head = _head_object(client, bucket, key)
    return (head["size_bytes"] == len(body) and head["metadata"].get("sha256") == sha
            and _sha256_of_object(client, bucket, key) == sha)


def code_sweep(
    directory: str | os.PathLike,
    panel_name: str,
    *,
    patterns: tuple[str, ...] | list[str] | None = None,
    exclude_dirs: tuple[str, ...] | list[str] = (),
    data_dir: str | os.PathLike | None = None,
    data_patterns: tuple[str, ...] | list[str] = SWEEP_DEFAULT_PATTERNS,
    state_path: str | os.PathLike | None = None,
    user: str | None = None,
    apply: bool = False,
    max_bytes: int = CODE_MAX_BYTES,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Back up the panel's code, notebooks, docs and knowledge/ files (new or changed since the last run).

    Scans `directory` (the whole workspace) and uploads to <prefix><user>/<panel>/code/<YYYY-MM-DD>/<relative path>.
    ONE folder per UTC day: the first sweep of a day uploads every file (a full snapshot of that day), later
    sweeps the same day only add files that changed since, overwriting that day's copy. The workspace folder
    structure is kept inside the day folder.
    Skipped: hidden files/folders (.git, .fathom_runtime, .fathom_recovery, ...), __pycache__, node_modules,
    venv, site-packages, symlinks, files over `max_bytes`, config-like files whose name looks secret
    (reported, not uploaded), and the data files under `data_dir` that the normal `sweep` already covers.
    State (relative path -> sha256) lives in <directory>/.fathom_recovery/code_state.json, outside any data
    folder, and a file is recorded only after its upload verified. Read-only on the workspace; nothing is ever
    deleted. Dry run unless apply=True (nothing uploaded, state untouched).
    """
    _require_panel(panel_name)
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError("directory does not exist")
    state_file = Path(state_path).resolve() if state_path else root / CODE_STATE_DIR / CODE_STATE_NAME
    state = _load_state(state_file)
    today = make_day_folder()
    if state.get(CODE_DAY_KEY) != today:
        # a new day: today's folder starts empty, so every file is uploaded once into it (a full snapshot).
        # Dropping yesterday's hashes also means a file that fails today is retried, not skipped.
        state = {}
    pats = [p.lower() for p in (patterns or CODE_DEFAULT_PATTERNS)]
    dpats = [p.lower() for p in (data_patterns or SWEEP_DEFAULT_PATTERNS)]
    data_root = Path(data_dir).resolve() if data_dir else None

    client = s3
    panel_folder, user_folder, warning = panel_name, user, None
    resolved, run_id = False, None
    files: list[dict[str, Any]] = []
    uploaded_meta: list[dict[str, Any]] = []
    counts = {"scanned": 0, "uploaded": 0, "would_upload": 0, "unchanged": 0, "failed": 0,
              "skipped_sensitive": 0, "skipped_large": 0}
    for p in _walk_code_files(root, {str(x).strip("/") for x in exclude_dirs}):
        if p.is_symlink() or p.resolve() == state_file or p.name.endswith(".tmp"):
            continue
        low = p.name.lower()
        if not any(fnmatch.fnmatchcase(low, pat) for pat in pats):
            continue
        if data_root and data_root in p.resolve().parents and any(fnmatch.fnmatchcase(low, d) for d in dpats):
            continue  # a data file in the output folder: the normal sweep owns it
        rel = "/".join(p.relative_to(root).parts)
        if _is_sensitive_name(p.name):
            counts["skipped_sensitive"] += 1
            files.append({"file": rel, "action": "skipped_sensitive_name"})
            continue
        try:
            size = p.stat().st_size
            if size > max_bytes:
                counts["skipped_large"] += 1
                files.append({"file": rel, "action": "skipped_large", "size_bytes": size})
                continue
            data = p.read_bytes()  # read once: the hash and the upload use the same bytes
        except OSError as exc:
            counts["scanned"] += 1
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        counts["scanned"] += 1
        sha = hashlib.sha256(data).hexdigest()
        if state.get(rel) == sha:
            counts["unchanged"] += 1
            continue  # unchanged files are counted, not listed (a workspace can have hundreds)
        if not apply:
            counts["would_upload"] += 1
            files.append({"file": rel, "action": "would_upload", "size_bytes": len(data)})
            continue
        try:
            client = client or boto3.client("s3")
            if not resolved:
                panel_folder, warning, user_folder = _panel_folder(
                    panel_name, client, destination_bucket, destination_prefix, user)
                run_id = today  # ONE folder per (UTC) day: same-day re-runs reuse it
                resolved = True
            key = f"{_clean_prefix(destination_prefix)}{user_folder}/{panel_folder}/{CODE_FOLDER}/{run_id}/{rel}"
            ok = _put_verified(client, destination_bucket, key, data, sha)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sweep
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if ok:
            state[rel] = sha
            counts["uploaded"] += 1
            uploaded_meta.append({"path": rel, "sha256": sha, "size_bytes": len(data)})
            files.append({"file": rel, "action": "uploaded", "size_bytes": len(data)})
        else:
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": "upload not verified"})

    result: dict[str, Any] = {"ok": counts["failed"] == 0, "action": "code-sweep", "applied": apply, **counts}
    if apply and uploaded_meta:
        run_prefix = f"{_clean_prefix(destination_prefix)}{user_folder}/{panel_folder}/{CODE_FOLDER}/{run_id}/"
        # the day folder can already hold a manifest from an earlier run today: merge, never drop its entries
        merged: dict[str, dict[str, Any]] = {}
        try:
            old = json.loads(client.get_object(
                Bucket=destination_bucket, Key=run_prefix + CODE_MANIFEST_NAME)["Body"].read())
            for f in old.get("files", []):
                if isinstance(f, dict) and isinstance(f.get("path"), str):
                    merged[f["path"]] = f
        except Exception:  # noqa: BLE001 - no manifest yet (first run of the day) or unreadable: start fresh
            pass
        merged.update({f["path"]: f for f in uploaded_meta})
        manifest = json.dumps({
            "fathom_recovery_version": __version__, "kind": "code-day", "panel": panel_folder,
            "user": user_folder, "run_id": run_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),  # time of the last update that day
            "files": [merged[k] for k in sorted(merged)],
        }, indent=2, sort_keys=True).encode("utf-8")
        try:
            mok = _put_verified(client, destination_bucket, run_prefix + CODE_MANIFEST_NAME, manifest,
                                hashlib.sha256(manifest).hexdigest(), "application/json")
        except Exception as exc:  # noqa: BLE001
            mok = False
            result["manifest_error"] = f"{type(exc).__name__}: {exc}"
        result["manifest_ok"] = mok
        if not mok:
            result["ok"] = False  # the files are safe in S3 and recorded in state; restore does not need the manifest
        state[CODE_DAY_KEY] = today
        state_file.parent.mkdir(parents=True, exist_ok=True)
        _save_state(state_file, state)
    if run_id:
        result["run_id"] = run_id
        result["recovery_folder"] = f"{user_folder}/{panel_folder}/{CODE_FOLDER}/{run_id}/"
    if warning:
        result["warning"] = warning
    result["files"] = files
    return result


def list_code(
    panel_name: str,
    *,
    user: str | None = None,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> list[dict[str, Any]]:
    """Every code/knowledge object backed up for this user's panel (read-only): run_id, path, size, key."""
    _require_panel(panel_name)
    client = s3 or boto3.client("s3")
    root = _clean_prefix(destination_prefix)
    who = resolve_user_folder(user, destination_bucket=destination_bucket,
                              destination_prefix=destination_prefix, s3=client)
    panel = resolve_panel_folder(panel_name, user=who, destination_bucket=destination_bucket,
                                 destination_prefix=destination_prefix, s3=client)
    prefix = f"{root}{who}/{panel}/{CODE_FOLDER}/"
    out: list[dict[str, Any]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=destination_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            run_id, _, path = key[len(prefix):].partition("/")
            if not run_id or not path or path == CODE_MANIFEST_NAME:
                continue
            lm = obj.get("LastModified")
            out.append({"run_id": run_id, "path": path, "size_bytes": obj.get("Size"), "key": key,
                        "recovery_path": key[len(root):], "user": who, "panel": panel,
                        "last_modified_utc": lm.astimezone(timezone.utc).isoformat() if lm else None})
    out.sort(key=lambda r: (_run_sort_key(r["run_id"]), r["path"]), reverse=True)
    return out


def _run_sort_key(run_id: str) -> str:
    """Sortable UTC time for a code folder name: new day folders 'YYYY-MM-DD' and old '<YYYYMMDDTHHMMSSZ>-<hex>' runs."""
    for fmt, part in (("%Y-%m-%d", run_id), ("%Y%m%dT%H%M%SZ", run_id.split("-")[0])):
        try:
            return datetime.strptime(part, fmt).replace(tzinfo=timezone.utc).isoformat() + "|" + run_id
        except ValueError:
            continue
    return "|" + run_id


def _run_time(client: Any, bucket: str, run_prefix: str, run_id: str) -> str:
    """ISO time of a code folder: the manifest's created_utc, else the time/date inside the folder name."""
    try:
        body = client.get_object(Bucket=bucket, Key=run_prefix + CODE_MANIFEST_NAME)["Body"].read()
        created = json.loads(body).get("created_utc")
        if isinstance(created, str) and created:
            return created
    except Exception:  # noqa: BLE001 - a missing/unreadable manifest only weakens the ordering
        pass
    return _run_sort_key(run_id).split("|")[0]


def summarize_code_runs(objs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group list_code() rows by code folder (one per day; newest first) with file count and total size."""
    runs: dict[str, dict[str, Any]] = {}
    for o in objs:
        r = runs.setdefault(o["run_id"], {"run_id": o["run_id"], "files": 0, "size_bytes": 0})
        r["files"] += 1
        r["size_bytes"] += o.get("size_bytes") or 0
    return sorted(runs.values(), key=lambda r: _run_sort_key(r["run_id"]), reverse=True)



def restore_code(
    panel_name: str,
    target_dir: str | os.PathLike,
    *,
    user: str | None = None,
    run: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """Download backed-up code/knowledge files into a LOCAL folder (S3 is only read).

    run=None  -> the newest backed-up version of every file (a full snapshot of the latest state).
    run="R"   -> exactly the files uploaded in run R.
    Files are written under target_dir with their original relative paths. An existing local file is never
    replaced unless overwrite=True. Every file's sha256 is re-checked against the value stored at backup time.
    Point target_dir at a NEW folder; this never touches the live panel files by itself.
    """
    client = s3 or boto3.client("s3")
    objs = list_code(panel_name, user=user, destination_bucket=destination_bucket,
                     destination_prefix=destination_prefix, s3=client)
    if run:
        chosen = [o for o in objs if o["run_id"] == run]
        if not chosen:
            raise ValueError(f"no code files found for run {run!r}")
    else:
        by_path: dict[str, list[dict[str, Any]]] = {}
        for o in objs:
            by_path.setdefault(o["path"], []).append(o)
        # run ids only have 1-second resolution (+ random hex), so runs that both contain the same file are
        # ordered by the manifest's created_utc (microseconds); no manifest -> the time inside the run id
        contested = {o["run_id"]: o["key"][: -len(o["path"])]
                     for rows in by_path.values() if len(rows) > 1 for o in rows}
        when = {rid: _run_time(client, destination_bucket, pfx, rid) for rid, pfx in contested.items()}
        chosen = sorted((max(rows, key=lambda o: (when.get(o["run_id"], ""), _run_sort_key(o["run_id"])))
                         for rows in by_path.values()), key=lambda o: o["path"])
    target = Path(target_dir).resolve()
    counts = {"restored": 0, "would_restore": 0, "refused_exists": 0, "failed": 0}
    files: list[dict[str, Any]] = []
    for o in chosen:
        rel = o["path"]
        segs = rel.split("/")
        dest = (target / rel).resolve()
        if any(s in {"", ".", ".."} for s in segs) or target not in dest.parents:
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": "unsafe path"})
            continue
        if dest.exists() and not overwrite:
            counts["refused_exists"] += 1
            files.append({"file": rel, "action": "refused_exists", "run_id": o["run_id"]})
            continue
        if dry_run:
            counts["would_restore"] += 1
            files.append({"file": rel, "action": "would_restore", "run_id": o["run_id"],
                          "size_bytes": o["size_bytes"]})
            continue
        try:
            expected = _head_object(client, destination_bucket, o["key"])["metadata"].get("sha256")
            body = client.get_object(Bucket=destination_bucket, Key=o["key"])["Body"].read()
            if expected and hashlib.sha256(body).hexdigest() != expected:
                raise RuntimeError("sha256 mismatch after download")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the restore
            counts["failed"] += 1
            files.append({"file": rel, "action": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        counts["restored"] += 1
        files.append({"file": rel, "action": "restored", "run_id": o["run_id"], "size_bytes": len(body)})
    return {"ok": counts["failed"] == 0, "action": "restore-code", "dry_run": dry_run,
            "run": run or "latest-per-file", "target_dir": str(target), **counts, "files": files}


# ------------------------------------------------- panel config + instruction
CONFIG_NAME = "recovery_config.json"
INSTRUCTIONS_NAME = "RECOVERY_INSTRUCTIONS.md"


def load_config(path: str | os.PathLike) -> dict[str, Any]:
    """Read recovery_config.json: {"panel_name": ..., "output_dir": ..., "patterns": [...] (optional)}.

    A relative output_dir is resolved against the config file's own folder, so the same config
    works no matter which folder the command is run from.
    """
    p = Path(path)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read config {str(p)!r}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    _require_panel(cfg.get("panel_name"))
    out = Path(str(cfg.get("output_dir") or "outputs"))
    cfg["output_dir_resolved"] = str(out if out.is_absolute() else (p.resolve().parent / out))
    pats = cfg.get("patterns")
    if pats is not None and not (isinstance(pats, list) and all(isinstance(x, str) for x in pats)):
        raise ValueError("config 'patterns' must be a list of glob strings")
    for key in ("code_patterns", "code_exclude_dirs"):
        val = cfg.get(key)
        if val is not None and not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
            raise ValueError(f"config '{key}' must be a list of strings")
    return cfg


def render_instructions(panel_name: str, output_dir: str = "outputs", config_path: str = CONFIG_NAME) -> str:
    """The standing instruction for a panel's agent. Uses the config file, so it never hard-codes the panel."""
    panel = " ".join(str(panel_name).split())
    return f"""# Recovery backup for this panel

Panel name: "{panel}"   (set in {config_path}; case and extra spaces do not matter)
Data folder: {output_dir}

Setup (once per panel; skip it if scripts/fathom_recovery.py already exists):

       [ -d fathom-recovery-v{__version__} ] || git clone --depth 1 --branch v{__version__} {REPO_URL} fathom-recovery-v{__version__}
       python3 fathom-recovery-v{__version__}/setup_panel.py --panel-name "{panel}" --output-dir {output_dir} --apply

   Show me every PASS/FAIL line. A FAIL that mentions AccessDenied or NoCredentialsError means this
   panel has no S3 permission on 6_Recovery/: tell me and stop, nothing else will work until it is fixed.

Rule (follow it every time, without being asked):

1. Whenever you create or change a data file in this panel (csv, xlsx, xls, json, parquet, txt),
   as the LAST step of that task run:

       python3 scripts/fathom_recovery.py sweep --config {config_path} --apply

   then tell me how many files were uploaded, unchanged, or failed.
2. Whenever you create or change code, notebooks, docs or knowledge files in this panel
   (py, ipynb, md, js, html, sql, yaml, ... and everything under knowledge/), as the LAST step of that
   task run:

       python3 scripts/fathom_recovery.py code-sweep --config {config_path} --apply

   then tell me how many files were uploaded, unchanged, or failed.
3. At the start of every session, run BOTH commands once. They upload anything a previous run missed
   and skip files that are already uploaded. If scripts/fathom_recovery.py is missing, do the Setup first.
4. If either reports a failure or AccessDenied, tell me. Do not retry silently or skip it.
5. Never delete anything in S3 and never edit {config_path} yourself unless I ask.

Data files land in S3 under 6_Recovery/<user>/<panel folder>/<timestamp>-<8 hex>/<file>.
Code and knowledge files land under 6_Recovery/<user>/<panel folder>/code/<YYYY-MM-DD>/<folder>/<file> (one folder per day).
Nothing uploads by itself: these rules are what trigger the upload.
"""


def init_panel(
    panel_name: str,
    output_dir: str = "outputs",
    *,
    directory: str | os.PathLike = ".",
    patterns: list[str] | None = None,
    force: bool = False,
    write_instructions: bool = True,
) -> dict[str, Any]:
    """Write recovery_config.json (and, if write_instructions, RECOVERY_INSTRUCTIONS.md) into `directory`.

    Local files only, no S3. Refuses to overwrite an existing config unless force=True.
    The CLI `init` passes write_instructions=False unless --with-instructions is given, because the
    instructions file is not one of the files a recovery job is allowed to create in a panel.
    """
    panel = " ".join(_require_panel(panel_name).split())
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cfg_path, ins_path = d / CONFIG_NAME, d / INSTRUCTIONS_NAME
    if cfg_path.exists() and not force:
        raise ValueError(f"{CONFIG_NAME} already exists in {str(d)!r}; use --force to replace it")
    cfg: dict[str, Any] = {"panel_name": panel, "output_dir": output_dir}
    if patterns:
        cfg["patterns"] = list(patterns)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    result: dict[str, Any] = {
        "ok": True, "action": "init", "panel_name": panel, "output_dir": output_dir,
        "config_file": str(cfg_path), "instructions_file": None,
    }
    if write_instructions:
        ins_path.write_text(render_instructions(panel, output_dir, CONFIG_NAME), encoding="utf-8")
        result["instructions_file"] = str(ins_path)
        result["next"] = f"give the contents of {INSTRUCTIONS_NAME} to this panel's agent as a standing instruction"
    return result


def setup_panel(
    panel_name: str,
    output_dir: str = "outputs",
    *,
    directory: str | os.PathLike = ".",
    user: str | None = None,
    apply: bool = False,
    destination_bucket: str = DESTINATION_BUCKET,
    destination_prefix: str = DESTINATION_PREFIX,
    s3: Any | None = None,
) -> dict[str, Any]:
    """One-command setup check for a panel, matching the v1.7.0 prompt. Dry run unless apply=True.

    Local writes are limited to the recovery scope: recovery_config.json (only if it does not exist yet),
    the output folder (only if missing) and, with apply, .fathom_recovery/state.json + code_state.json.
    It never writes RECOVERY_INSTRUCTIONS.md and never deletes anything.
    Steps: panel name -> config -> output folder -> resolve user/panel folders (read-only listing)
    -> selfcheck -> data sweep -> code sweep. Every step is PASS / FAIL / SKIP.
    """
    steps: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> bool:
        steps.append({"step": name, "status": status, "detail": detail})
        return status != "FAIL"

    d = Path(directory).resolve()
    result: dict[str, Any] = {"ok": False, "action": "setup", "applied": apply,
                              "layout": LAYOUT, "code_backup": CODE_BACKUP, "version": __version__,
                              "steps": steps}

    def finish() -> dict[str, Any]:
        result["ok"] = not any(s["status"] == "FAIL" for s in steps)
        result["lines"] = [f"[{s['status']}] {s['step']}: {s['detail']}" for s in steps]
        return result

    try:
        panel = " ".join(_require_panel(panel_name).split())
        rec("1 panel name", "PASS", repr(panel))
    except ValueError as exc:
        rec("1 panel name", "FAIL", str(exc))
        return finish()

    cfg_path = d / CONFIG_NAME
    try:
        if cfg_path.exists():
            rec("2 config", "PASS", f"{CONFIG_NAME} already exists - kept as is")
        else:
            init_panel(panel, output_dir, directory=d, write_instructions=False)
            rec("2 config", "PASS", f"wrote {CONFIG_NAME}")
        cfg = load_config(cfg_path)
    except ValueError as exc:
        rec("2 config", "FAIL", str(exc))
        return finish()

    panel = cfg["panel_name"]
    out_dir = Path(cfg["output_dir_resolved"])
    existed = out_dir.is_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    rec("3 output folder", "PASS", f"{cfg['output_dir']} ({'exists' if existed else 'created, empty for now'}); "
        "make sure your panel really writes its data files there")

    client = s3
    kw = {"destination_bucket": destination_bucket, "destination_prefix": destination_prefix}
    try:
        client = client or boto3.client("s3")
        who = resolve_user_folder(user, s3=client, **kw)
        folder = resolve_panel_folder(panel, user=who, s3=client, **kw)
        result["user_folder"], result["panel_folder"] = who, folder
        rec("4 folders", "PASS", f"user folder {who!r}, panel folder {folder!r}")
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        rec("4 folders", "FAIL", f"{type(exc).__name__}: {exc}")
        return finish()

    sc = selfcheck(panel, user=who, apply=apply, s3=client, **kw)
    detail = "; ".join(f"{s['step']}={'ok' if s['ok'] else 'FAILED: ' + str(s['detail'])[:160]}" for s in sc["steps"])
    rec("5 selfcheck" + (" --apply" if apply else " (dry run)"), "PASS" if sc["ok"] else "FAIL", detail)

    state_dir = d / CODE_STATE_DIR
    if apply:
        state_dir.mkdir(exist_ok=True)

    def summarize(r: dict[str, Any], extra: tuple[str, ...] = ()) -> str:
        keys = ("scanned", "uploaded", "would_upload", "unchanged", "failed") + extra
        text = " ".join(f"{k}={r.get(k)}" for k in keys)
        errs = [f.get("error", "") for f in r.get("files", []) if f.get("action") == "failed"]
        return text + (" | " + errs[0][:160] if errs else "")

    try:
        sw = sweep(out_dir, panel, patterns=cfg.get("patterns") or SWEEP_DEFAULT_PATTERNS,
                   state_path=state_dir / "state.json", user=who, apply=apply, s3=client, **kw)
        rec("6 sweep" + (" --apply" if apply else " (dry run)"), "PASS" if sw["ok"] else "FAIL", summarize(sw))
    except Exception as exc:  # noqa: BLE001
        rec("6 sweep", "FAIL", f"{type(exc).__name__}: {exc}")
    try:
        cs = code_sweep(d, panel, patterns=cfg.get("code_patterns"),
                        exclude_dirs=cfg.get("code_exclude_dirs") or (), data_dir=out_dir,
                        data_patterns=cfg.get("patterns") or SWEEP_DEFAULT_PATTERNS,
                        state_path=state_dir / CODE_STATE_NAME, user=who, apply=apply, s3=client, **kw)
        rec("7 code-sweep" + (" --apply" if apply else " (dry run)"), "PASS" if cs["ok"] else "FAIL",
            summarize(cs, ("skipped_sensitive", "skipped_large")))
    except Exception as exc:  # noqa: BLE001
        rec("7 code-sweep", "FAIL", f"{type(exc).__name__}: {exc}")
    if not apply:
        result["note"] = "dry run: nothing was written to S3; add --apply to check write access and upload"
    return finish()


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
    ls.add_argument("--legacy", action="store_true",
                    help="list objects in the OLD <panel>/<user>/<run>/<file> layout (written before v1.6.0)")
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
    sw.add_argument("--panel-name", default=None, help="required unless --config is given")
    sw.add_argument("--config", default=None,
                    help="recovery_config.json (panel_name, output_dir, optional patterns); "
                         "explicit --panel-name/--dir/--pattern override it")
    sw.add_argument("--dir", default=None, help="folder to scan (default: output_dir from --config, else .)")
    sw.add_argument("--pattern", action="append", default=None,
                    help="glob such as *.csv (repeatable; default: csv, xlsx, xls, json, parquet, txt, pdf)")
    sw.add_argument("--no-recursive", action="store_true")
    sw.add_argument("--state-file", default=None, help="where to remember uploaded files")
    sw.add_argument("--user", default=None)
    sw.add_argument("--apply", action="store_true", help="actually upload (default is dry run)")
    common(sw)

    cs = sub.add_parser("code-sweep", help="back up code, notebooks, docs and knowledge/ (dry run unless --apply)")
    cs.add_argument("--panel-name", default=None, help="required unless --config is given")
    cs.add_argument("--config", default=None,
                    help="recovery_config.json (panel_name, output_dir, optional code_patterns / code_exclude_dirs)")
    cs.add_argument("--dir", default=None, help="folder to scan (default: the config file's folder, else .)")
    cs.add_argument("--pattern", action="append", default=None,
                    help="glob such as *.py (repeatable; default: py, ipynb, md, json, js, html, sql, yaml, ...)")
    cs.add_argument("--exclude-dir", action="append", default=None,
                    help="top-level or nested folder name to skip, e.g. uploads (repeatable)")
    cs.add_argument("--max-mb", type=float, default=CODE_MAX_BYTES / (1024 * 1024),
                    help="skip files larger than this (default 10)")
    cs.add_argument("--state-file", default=None, help="default: <dir>/.fathom_recovery/code_state.json")
    cs.add_argument("--user", default=None)
    cs.add_argument("--apply", action="store_true", help="actually upload (default is dry run)")
    common(cs)

    lc = sub.add_parser("list-code", help="list backed-up code/knowledge runs for a panel (read-only)")
    lc.add_argument("--panel-name", default=None, help="required unless --config is given")
    lc.add_argument("--config", default=None)
    lc.add_argument("--user", default=None)
    lc.add_argument("--run", default=None, help="show the files of one run instead of the run summary")
    lc.add_argument("--limit", type=int, default=50)
    common(lc)

    rc_ = sub.add_parser("restore-code", help="download backed-up code/knowledge into a LOCAL folder")
    rc_.add_argument("--panel-name", default=None, help="required unless --config is given")
    rc_.add_argument("--config", default=None)
    rc_.add_argument("--run", default=None, help="one run id (default: newest version of every file)")
    rc_.add_argument("--target-dir", default="restored_code", help="local folder to write into (default restored_code)")
    rc_.add_argument("--overwrite", action="store_true", help="replace existing local files (default: refuse)")
    rc_.add_argument("--user", default=None)
    rc_.add_argument("--apply", action="store_true", help="actually write files (default is dry run)")
    common(rc_)

    st = sub.add_parser("setup", help="one-command setup check: config, output folder, selfcheck, sweep, "
                                      "code-sweep (dry run unless --apply)")
    st.add_argument("--panel-name", required=True)
    st.add_argument("--output-dir", default="outputs", help="folder this panel writes data files to")
    st.add_argument("--directory", default=".", help="panel workspace root (default: current folder)")
    st.add_argument("--user", default=None)
    st.add_argument("--apply", action="store_true", help="write to S3 (selfcheck probe + uploads); default is dry run")
    common(st)

    ini = sub.add_parser("init", help="write recovery_config.json (local file only; instructions file only with "
                                      "--with-instructions)")
    ini.add_argument("--with-instructions", action="store_true",
                     help="also write RECOVERY_INSTRUCTIONS.md (off by default: it is not a recovery-scope file)")
    ini.add_argument("--panel-name", required=True)
    ini.add_argument("--output-dir", default="outputs", help="folder this panel writes data files to")
    ini.add_argument("--directory", default=".", help="where to write the two files (default: current folder)")
    ini.add_argument("--pattern", action="append", default=None, help="optional glob; repeatable")
    ini.add_argument("--force", action="store_true", help="replace an existing recovery_config.json")

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
            lister = list_legacy_recoveries if args.legacy else list_recoveries
            rows = lister(
                args.panel_name, user=args.user,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
            )
            result = {"ok": True, "panel": args.panel_name, "layout": "legacy" if args.legacy else "user/panel",
                      "total": len(rows), "shown": rows[: args.limit]}
        elif args.cmd == "upload":
            result = upload_to_recovery(
                Path(args.file), args.filename, panel_name=args.panel_name, user=args.user,
                dry_run=not args.apply,
                destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
            )
        elif args.cmd == "sweep":
            cfg = load_config(args.config) if args.config else {}
            panel = args.panel_name or cfg.get("panel_name")
            if not panel:
                raise ValueError("sweep needs --panel-name or --config")
            scan_dir = args.dir or cfg.get("output_dir_resolved") or "."
            if not args.dir and cfg.get("output_dir_resolved"):
                os.makedirs(scan_dir, exist_ok=True)  # panel may not have generated anything yet
            result = sweep(
                scan_dir, panel,
                patterns=args.pattern or cfg.get("patterns") or SWEEP_DEFAULT_PATTERNS,
                recursive=not args.no_recursive, state_path=args.state_file, user=args.user,
                apply=args.apply, destination_bucket=args.destination_bucket,
                destination_prefix=args.destination_prefix,
            )
        elif args.cmd in ("code-sweep", "list-code", "restore-code"):
            cfg = load_config(args.config) if args.config else {}
            panel = args.panel_name or cfg.get("panel_name")
            if not panel:
                raise ValueError(f"{args.cmd} needs --panel-name or --config")
            cfg_dir = Path(args.config).resolve().parent if args.config else Path(".").resolve()
            if args.cmd == "code-sweep":
                scan_dir = args.dir or str(cfg_dir)
                out_cfg = cfg.get("output_dir_resolved")
                result = code_sweep(
                    scan_dir, panel,
                    patterns=args.pattern or cfg.get("code_patterns"),
                    exclude_dirs=args.exclude_dir or cfg.get("code_exclude_dirs") or (),
                    data_dir=out_cfg if out_cfg and not args.dir else None,
                    data_patterns=cfg.get("patterns") or SWEEP_DEFAULT_PATTERNS,
                    state_path=args.state_file, user=args.user, apply=args.apply,
                    max_bytes=int(args.max_mb * 1024 * 1024),
                    destination_bucket=args.destination_bucket, destination_prefix=args.destination_prefix,
                )
            elif args.cmd == "list-code":
                rows = list_code(panel, user=args.user, destination_bucket=args.destination_bucket,
                                 destination_prefix=args.destination_prefix)
                if args.run:
                    rows = [r for r in rows if r["run_id"] == args.run]
                    result = {"ok": True, "panel": panel, "run": args.run, "total_files": len(rows),
                              "files": [{"path": r["path"], "size_bytes": r["size_bytes"]} for r in rows[: args.limit]]}
                else:
                    runs = summarize_code_runs(rows)
                    result = {"ok": True, "panel": panel, "total_runs": len(runs), "total_files": len(rows),
                              "runs": runs[: args.limit]}
            else:
                result = restore_code(
                    panel, args.target_dir, user=args.user, run=args.run, overwrite=args.overwrite,
                    dry_run=not args.apply, destination_bucket=args.destination_bucket,
                    destination_prefix=args.destination_prefix,
                )
        elif args.cmd == "init":
            result = init_panel(args.panel_name, args.output_dir, directory=args.directory,
                                patterns=args.pattern, force=args.force,
                                write_instructions=args.with_instructions)
        elif args.cmd == "setup":
            result = setup_panel(
                args.panel_name, args.output_dir, directory=args.directory, user=args.user,
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
        if hasattr(args, "destination_bucket"):  # `init` is local-only and has no bucket option
            result["destination_bucket_redacted"] = _redact(args.destination_bucket)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    except (ClientError, BotoCoreError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
