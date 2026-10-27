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

Set RECOVERY_DESTINATION_BUCKET for each panel (or pass destination_bucket).
The default prefix is 6_Recovery/; override with RECOVERY_DESTINATION_PREFIX.

Library use
-----------
    from fathom_recovery import put_object_and_copy_to_recovery, copy_created_object_to_recovery

    put_object_and_copy_to_recovery(bucket, key, csv_bytes,
                                    panel_name="Daily Checklist Brokerage",
                                    content_type="text/csv")
    copy_created_object_to_recovery(bucket, key, panel_name="Daily Checklist Brokerage")

CLI (dry run unless --apply is given)
-------------------------------------
    python3 fathom_recovery.py copy    --panel-name "<Panel>" --source-bucket B --source-key K [--user U] [--apply]
    python3 fathom_recovery.py list    --panel-name "<Panel>" [--user U]
    python3 fathom_recovery.py restore --recovery-path "<Panel>/<user>/<run>/<file>" \
                                       --target-bucket B --target-key K [--overwrite] [--apply]

Output never prints bucket names (they are redacted).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

__version__ = "1.2.0"

DESTINATION_BUCKET = os.environ.get("RECOVERY_DESTINATION_BUCKET", "").strip()
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


def _require_destination_bucket(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("Set RECOVERY_DESTINATION_BUCKET or pass --destination-bucket / destination_bucket")
    return value.strip()


def _safe_segment(value: Any, what: str) -> str:
    """Make a value safe to use as ONE S3 path segment (keeps spaces and case).

    Slashes / backslashes / control characters become spaces, so a name can
    never create extra folders or escape its own. Blank, '.' and '..' are rejected.
    """
    seg = re.sub(r"[\\/\x00-\x1f]+", " ", str(value or "")).strip()
    seg = re.sub(r"\s+", " ", seg).strip(" .")
    if not seg or seg in {".", ".."}:
        raise ValueError(f"{what} is required")
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
    destination_bucket = _require_destination_bucket(destination_bucket)
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
    content_type = source_meta.get("content_type") or mimetypes.guess_type(dest_key)[0] or "application/octet-stream"
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
    _require_destination_bucket(destination_bucket)
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    client = s3 or boto3.client("s3")
    client.put_object(
        Bucket=source_bucket,
        Key=source_key,
        Body=body_bytes,
        ContentType=content_type or ("text/csv" if source_key.lower().endswith(".csv") else mimetypes.guess_type(source_key)[0]) or "application/octet-stream",
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
    destination_bucket = _require_destination_bucket(destination_bucket)
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
    destination_bucket = _require_destination_bucket(destination_bucket)
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
        _require_destination_bucket(destination_bucket)
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
