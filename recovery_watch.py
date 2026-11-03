#!/usr/bin/env python3
"""Fathom -> S3 change watcher: when any workspace file is created/edited, the S3 copy is refreshed automatically.

Polls file (mtime, size) of everything the code sweep would upload (cheap: stat only, no hashing). When something
changed and the writes have settled (--quiet seconds without further changes), it runs the SAME two sweeps the
dashboard sync uses (data sweep + code sweep, serialized by the same lock as scripts/recovery_sync.py). The sweeps
hash the files and upload only NEW/CHANGED ones to the SAME S3 key (code/<path> or <YYYYMMDD>_current_data/<path>).

  python3 scripts/recovery_watch.py                 watch forever (one baseline sweep at start)
  python3 scripts/recovery_watch.py --once          one sweep now, then exit (for a cron/schedule)
  python3 scripts/recovery_watch.py --dry-run       sweeps run without --apply (nothing uploaded)
  python3 scripts/recovery_watch.py --status        is a watcher running, what did it do last
  python3 scripts/recovery_watch.py --stop          ask the running watcher to exit
  python3 scripts/recovery_watch.py --log /tmp/x.log   append the watcher's output to a file OUTSIDE the workspace
                                                    (a log inside the workspace would itself be swept and re-trigger sweeps,
                                                    so such a path is refused)

The S3 <user> folder is resolved once at start and pinned (RECOVERY_USER); if it would be "unknown-user" the watcher
refuses to run instead of uploading to the wrong folder.
Needs Linux/macOS (fcntl). It lives only as long as the runtime process: a runtime restart stops it, start it again.
Safety: storage only (never deletes anything in S3; a file deleted/renamed in Fathom leaves the old S3 copy).
It ignores .fathom_recovery/, .fathom_runtime/ and the sweeps' own skip rules, so a sweep can never trigger a sweep.
Failures (no credentials, S3 down) are retried with backoff and never crash the watcher.
Only one watcher runs per workspace (lock file in the temp dir).
"""
import argparse
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import datetime

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WATCH_SKIP_DIRS = {".fathom_recovery", ".fathom_runtime"}      # state / runtime files must never trigger a sweep
TAG = hashlib.sha256(ROOT.encode()).hexdigest()[:12]
TMP = tempfile.gettempdir()
LOCK_PATH = os.path.join(TMP, "fathom_recovery_watch_%s.lock" % TAG)
STATUS_PATH = os.path.join(TMP, "fathom_recovery_watch_%s.status.json" % TAG)
STOP_PATH = os.path.join(TMP, "fathom_recovery_watch_%s.stop" % TAG)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = _load("fathom_recovery")      # skip rules / sensitive-name rule: identical to what the sweeps upload
rs = _load("recovery_sync")        # sweep() runner + the shared lock


def log(*a):
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *a, flush=True)


def load_cfg():
    try:
        with open(os.path.join(ROOT, "recovery_config.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def scan(cfg):
    """{rel path: (mtime_ns, size)} for every file a sweep could upload, plus "<dir>/": (0, 0) for each empty folder."""
    skip = fr.CODE_SKIP_DIRS | WATCH_SKIP_DIRS | set(cfg.get("code_exclude_dirs") or [])
    patterns = cfg.get("code_patterns") or ["*"]
    out = {}
    for dp, dn, fn in os.walk(ROOT, followlinks=False):
        was_empty = not dn and not fn
        dn[:] = [d for d in dn if d not in skip and not os.path.islink(os.path.join(dp, d))
                 and not (dp == ROOT and d in fr.CODE_SKIP_ROOT_DIRS)]
        if was_empty and dp != ROOT:                     # an EMPTY folder is backed up too (zero-byte marker)
            out[os.path.relpath(dp, ROOT).replace(os.sep, "/") + "/"] = (0, 0)
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p) or f.lower().endswith(".tmp") or (f.startswith(".tmp_") and f.endswith(".json")):
                continue
            if not any(fnmatch.fnmatch(f, pat) for pat in patterns) or fr.sensitive_name(f):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size > fr.MAX_CODE_BYTES:
                continue
            out[os.path.relpath(p, ROOT).replace(os.sep, "/")] = (st.st_mtime_ns, st.st_size)
    return out


def new_or_modified(old, cur):
    return sorted(r for r, v in cur.items() if old.get(r) != v)


def pin_user():
    """Resolve the <user> S3 folder ONCE, now, and pin it in RECOVERY_USER so every later sweep (a subprocess) uses it.

    Why: the user comes from env RECOVERY_USER, else bridge.whoami(). A watcher that outlives the process that started it
    can lose the bridge module; every later sweep would then resolve "unknown-user" and upload to the WRONG S3 folder
    (and record those files as uploaded). Returns the user, or None when it would be "unknown-user" (refuse to run)."""
    u = fr.resolve_user(None)
    if u == "unknown-user" and not os.environ.get("RECOVERY_USER"):
        return None
    os.environ["RECOVERY_USER"] = u
    return u


def run_sweeps(dry):
    lock = rs.acquire_lock(timeout=120)
    try:
        return [rs.sweep("sweep", "state.json", not dry), rs.sweep("code-sweep", "code_state.json", not dry)]
    finally:
        lock.close()


def write_status(d):
    try:
        rs.atomic_write(STATUS_PATH, json.dumps(d, sort_keys=True).encode())
    except Exception:
        pass


def read_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def lock_held():
    """True if another watcher holds the lock (probe without keeping it)."""
    fh = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def summarize(results):
    return [{k: r.get(k) for k in ("command", "ok", "uploaded", "unchanged", "failed", "exit_code")} for r in results]


def watch(a):
    fh = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another watcher is already running for this workspace; exiting")
        return 3
    try:
        os.remove(STOP_PATH)
    except OSError:
        pass
    cfg = load_cfg()
    started = time.time()
    st = {"pid": os.getpid(), "root": ROOT, "started_utc": iso(started), "sweeps": 0, "failures": 0,
          "last_sweep_utc": None, "last_ok": None, "last_result": None, "tracked_files": 0, "dry_run": a.dry_run}
    sig = scan(cfg)
    dirty, last_change = True, time.time() - a.quiet           # baseline sweep right away
    last_full, backoff, retry_at = time.time(), 0, 0
    log("watching %d files (interval=%ss quiet=%ss full_every=%ss%s) user folder pinned to '%s'" % (
        len(sig), a.interval, a.quiet, a.full_every, ", DRY RUN" if a.dry_run else "", os.environ.get("RECOVERY_USER")))
    st["user_folder"] = os.environ.get("RECOVERY_USER")
    while True:
        if os.path.exists(STOP_PATH):
            log("stop requested; exiting")
            break
        if a.max_runtime and time.time() - started > a.max_runtime:
            log("max runtime reached; exiting")
            break
        now = time.time()
        cur = scan(cfg)
        if cur != sig:
            ch = new_or_modified(sig, cur)
            if ch:
                dirty = True
                log("changed: %s%s" % (", ".join(ch[:5]), " (+%d more)" % (len(ch) - 5) if len(ch) > 5 else ""))
            last_change = now
            sig = cur
        if not dirty and a.full_every and now - last_full >= a.full_every:
            dirty = True
            log("periodic full sweep")
        if dirty and now - last_change >= a.quiet and now >= retry_at:
            try:
                res = run_sweeps(a.dry_run)
                ok = all(r["ok"] for r in res)
            except SystemExit as e:                      # could not get the lock
                res, ok = [{"command": "lock", "ok": False, "error": str(e)}], False
            except Exception as e:  # noqa: BLE001
                res, ok = [{"command": "sweeps", "ok": False, "error": "%s: %s" % (type(e).__name__, e)}], False
            st["sweeps"] += 1
            st["last_sweep_utc"], st["last_ok"], st["last_result"] = iso(), ok, summarize(res) if ok else res
            if ok:
                dirty, backoff, last_full = False, 0, time.time()
                log("sweep ok: " + json.dumps(summarize(res)))
            else:
                st["failures"] += 1
                backoff = min(max(a.retry_min, backoff * 2), 600)
                retry_at = time.time() + backoff
                log("sweep PROBLEM (retry in %ds): %s" % (backoff, json.dumps(res)[:600]))
            sig = scan(cfg)                              # edits made while sweeping are noticed on the next pass
            sig_after = sig
            # anything written during the sweep differs from `cur`, so compare to the pre-sweep scan
            if new_or_modified(cur, sig_after):
                dirty, last_change = True, time.time()
        st["tracked_files"] = len(sig)
        st["heartbeat_utc"] = iso()
        write_status(st)
        time.sleep(a.interval)
    st["stopped_utc"] = iso()
    write_status(st)
    return 0


def iso(t=None):
    return datetime.datetime.fromtimestamp(t or time.time(), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between scans (default 5)")
    ap.add_argument("--quiet", type=float, default=3.0, help="seconds without further changes before sweeping (default 3)")
    ap.add_argument("--full-every", type=float, default=900.0, help="forced sweep every N seconds as a safety net (0=off, default 900)")
    ap.add_argument("--retry-min", type=float, default=30.0, help="first retry delay after a failed sweep (default 30, max 600)")
    ap.add_argument("--max-runtime", type=float, default=0, help="exit after N seconds (0 = run until stopped)")
    ap.add_argument("--once", action="store_true", help="run the sweeps once now and exit")
    ap.add_argument("--dry-run", action="store_true", help="sweeps run without --apply")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="append output to PATH instead of stdout; PATH must be outside the workspace")
    a = ap.parse_args()
    if not os.path.isfile(os.path.join(ROOT, "recovery_config.json")):
        print("recovery_config.json missing: run the backup setup first")
        return 1
    if a.status:
        s = read_status()
        print(json.dumps({"running": lock_held(), "status": s}, indent=1))
        return 0
    if a.stop:
        if lock_held():
            open(STOP_PATH, "w").close()
            print("stop requested; the watcher exits within one scan interval")
        else:
            print("no watcher is running")
        return 0
    if (a.once or not (a.status or a.stop)) and pin_user() is None:
        print("cannot tell which user folder to use (RECOVERY_USER is not set and bridge.whoami() is not available here), "
              "so the S3 folder would be 'unknown-user'. Refusing to run. Set RECOVERY_USER=<your user folder> or start "
              "the watcher from a session where the bridge is available.")
        return 1
    if a.once:
        try:
            res = run_sweeps(a.dry_run)
        except SystemExit as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        ok = all(r["ok"] for r in res)
        print(json.dumps({"ok": ok, "sweeps": res if not ok else summarize(res)}))
        return 0 if ok else 1
    if a.log:
        root_real = os.path.realpath(ROOT)
        p = os.path.realpath(os.path.expanduser(a.log))
        if p == root_real or p.startswith(root_real + os.sep):
            print("--log must be OUTSIDE the workspace (a log inside it would be swept and re-trigger sweeps): " + p)
            return 1
        try:
            sys.stdout = open(p, "a", buffering=1, encoding="utf-8")
        except OSError as e:
            print("cannot open the log file: %s" % e)
            return 1
        sys.stderr = sys.stdout
    return watch(a)


if __name__ == "__main__":
    sys.exit(main())
