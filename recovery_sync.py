#!/usr/bin/env python3
"""Dashboard -> S3 sync. Called by the browser hook (scripts/recovery_hook.js) after a dashboard refresh,
or by hand.

Steps (serialized by a lock, so two refreshes never run sweeps at the same time):
  1. Captured data in .fathom_recovery/inbox/:
       <name>__<ms>.<json|txt>          -> moved (renamed) to outputs/dashboard_data/<name>.<ext>, newest wins
       dashboard_responses__<ms>.json   -> MERGED into outputs/dashboard_data/dashboard_responses.json
                                           (every Panel response the dashboard displayed; newest data per call wins;
                                           the consumed inbox copy is removed, it is only a hand-off file)
  2. Dashboard snapshot: the dashboard HTML (hook stripped) + a small shim that replays those responses is written to
     outputs/dashboard_snapshot/<dashboard file name>. Opened from S3 (outside Fathom) it shows the data of the
     last refresh. Rewritten only when the data changed.
  3. Data sweep  -> <YYYYMMDD>_current_data/   (outputs/: the captured data files and dashboard_responses.json)
  4. Code sweep  -> code/                      (whole workspace: the dashboard HTML itself and the snapshot HTML)

Only new/changed files are uploaded. Prints ONE json line, exit 0 only if everything succeeded.
  --dry-run   do steps 1-2 locally but run the sweeps without --apply (nothing is uploaded)
  --no-code   skip step 4
"""
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "fathom_recovery.py")
INBOX = os.path.join(ROOT, ".fathom_recovery", "inbox")
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
INBOX_NAME = re.compile(r"^(.+)__(\d+)\.(json|txt)$")
SUMMARY = re.compile(r"SUMMARY (?:uploaded|would_upload)=(\d+) unchanged=(\d+)(?: failed=(\d+))?")
RESP_NAME = "dashboard_responses"


def iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def output_dir():
    try:
        with open(os.path.join(ROOT, "recovery_config.json")) as f:
            return json.load(f).get("output_dir") or "outputs"
    except Exception:
        return "outputs"


def atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def acquire_lock(timeout=120):
    path = os.path.join(tempfile.gettempdir(), "fathom_recovery_sync_%s.lock" % hashlib.sha256(ROOT.encode()).hexdigest()[:12])
    fh = open(path, "w")
    end = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > end:
                raise SystemExit("another sync is still running (waited %ds)" % timeout)
            time.sleep(0.5)


def merge_responses(paths, rejected, dest):
    """Merge captured Panel responses ({key: {t, r}}) into dest; a call's entry is replaced only when its data changed."""
    if not paths:
        return None
    cur = {"responses": {}, "updated_utc": None}
    try:
        with open(dest) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("responses"), dict):
            cur = d
    except Exception:
        pass
    changed = False
    for p in paths:
        try:
            with open(p) as f:
                new = json.load(f)
            if not isinstance(new, dict):
                raise ValueError("not an object")
        except Exception:
            rejected.append(os.path.basename(p))
            continue
        for k, v in new.items():
            if not isinstance(v, dict) or "r" not in v:
                continue
            old = cur["responses"].get(k)
            if old is None or old.get("r") != v["r"]:
                cur["responses"][k] = v
                changed = True
        os.remove(p)                                   # consumed hand-off file (not a user file, nothing in S3)
    if changed or not cur.get("updated_utc"):
        cur["updated_utc"] = iso_now()
        atomic_write(dest, json.dumps(cur, sort_keys=True).encode("utf-8"))
        return dest
    return None


def process_inbox():
    moved, rejected = [], []
    if not os.path.isdir(INBOX):
        return moved, rejected
    items = []
    for name in os.listdir(INBOX):
        p = os.path.join(INBOX, name)
        m = INBOX_NAME.match(name)
        if not os.path.isfile(p) or not m or not SAFE.match(m.group(1)) or os.path.getsize(p) == 0:
            rejected.append(name)
            continue
        items.append((m.group(1), int(m.group(2)), m.group(3), p))
    dest_dir = os.path.join(ROOT, output_dir(), "dashboard_data")
    resp_files = []
    for stable, _ts, ext, p in sorted(items):          # oldest first, so the newest capture is written last
        if stable == RESP_NAME and ext == "json":
            resp_files.append(p)
            continue
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, stable + "." + ext)
        os.replace(p, dest)
        moved.append(os.path.relpath(dest, ROOT).replace(os.sep, "/"))
    rdest = os.path.join(dest_dir, RESP_NAME + ".json")
    if merge_responses(resp_files, rejected, rdest):
        moved.append(os.path.relpath(rdest, ROOT).replace(os.sep, "/"))
    return moved, rejected


def load_installer():
    spec = importlib.util.spec_from_file_location("install_dashboard_hook", os.path.join(ROOT, "scripts", "install_dashboard_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_snapshot():
    resp = os.path.join(ROOT, output_dir(), "dashboard_data", RESP_NAME + ".json")
    if not os.path.isfile(resp):
        return {"skipped": "no dashboard responses captured yet"}
    inst = load_installer()
    chosen, found, why = inst.discover(ROOT)
    if not chosen:
        return {"skipped": why, "candidates": found}
    with open(resp) as f:
        data = json.load(f)
    try:
        with open(os.path.join(ROOT, chosen), "rb") as f:
            html = f.read().decode("utf-8")
    except UnicodeDecodeError:
        return {"error": "dashboard is not utf-8: " + chosen}
    base = inst.BLOCK_RE.sub("", html)                  # the snapshot must not carry the live hook
    # Fathom renders dashboard fragments inside .panel-body. Supply that
    # container when the snapshot is opened as a standalone HTML document.
    if not re.match(r"\s*(?:<!doctype[^>]*>\s*)?<html(?:\s|>)", base, re.I):
        base = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '</head><body><div class="panel-body">' + base +
                '</div></body></html>')
    out = inst.insert_block(base, inst.snapshot_block(data)).encode("utf-8")
    dest = os.path.join(ROOT, output_dir(), "dashboard_snapshot", os.path.basename(chosen))
    try:
        with open(dest, "rb") as f:
            old = f.read()
    except OSError:
        old = None
    written = old != out
    if written:
        atomic_write(dest, out)
    return {"source": chosen, "file": os.path.relpath(dest, ROOT).replace(os.sep, "/"), "written": written,
            "responses": len(data.get("responses", {})), "data_as_of": data.get("updated_utc")}


def sweep(command, state_name, apply):
    cmd = [sys.executable, "-B", SCRIPT, command, "--config", "recovery_config.json",
           "--state-file", os.path.join(".fathom_recovery", state_name)]
    if apply:
        cmd.append("--apply")
    r = subprocess.run(cmd, cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                       capture_output=True, text=True, timeout=840)
    out = (r.stdout + r.stderr).strip()
    m = SUMMARY.search(out)
    res = {"command": command, "exit_code": r.returncode, "ok": r.returncode == 0 and m is not None}
    if m:
        res.update(uploaded=int(m.group(1)), unchanged=int(m.group(2)), failed=int(m.group(3) or 0))
        res["ok"] = res["ok"] and res["failed"] == 0
    if not res["ok"]:
        res["output_tail"] = out[-800:]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-code", action="store_true", help="skip the whole-workspace code sweep")
    ap.add_argument("--dry-run", action="store_true", help="sweeps run without --apply (nothing uploaded)")
    a = ap.parse_args()
    result = {"ok": False, "dry_run": a.dry_run}
    try:
        if not os.path.isfile(SCRIPT) or not os.path.isfile(os.path.join(ROOT, "recovery_config.json")):
            raise SystemExit("scripts/fathom_recovery.py or recovery_config.json missing: run the backup setup first")
        lock = acquire_lock()
        os.makedirs(os.path.join(ROOT, ".fathom_recovery"), exist_ok=True)
        moved, rejected = process_inbox()
        result["captured_files"] = moved
        if rejected:
            result["inbox_rejected"] = rejected
        result["snapshot"] = build_snapshot()
        sweeps = [sweep("sweep", "state.json", not a.dry_run)]
        if not a.no_code:
            sweeps.append(sweep("code-sweep", "code_state.json", not a.dry_run))
        result["sweeps"] = sweeps
        result["ok"] = all(s["ok"] for s in sweeps) and "error" not in result["snapshot"]
        lock.close()
    except SystemExit as e:
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(e).__name__, e)
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
