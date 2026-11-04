#!/usr/bin/env python3
"""Pull fathom_recovery.py from the shared git repo into THIS panel's workspace.

    python3 sync_recovery.py --repo <git url or path> --ref v1.9.5 [--dest scripts] [--run-tests]
                             [--panel-name "<Panel>" --output-dir outputs [--apply]]

* --ref should be a tag (pinned version) so later changes can't alter a panel silently.
* Private HTTPS repos: set env FATHOM_RECOVERY_GIT_TOKEN. The token is sent as an
  HTTP header and is never put in the URL or printed.
* Copies fathom_recovery.py (and, with --run-tests, runs the offline test-suite from
  the fetched checkout, which touches no S3).
* --with-dashboard also copies the three dashboard-sync scripts (recovery_sync.py, recovery_hook.js,
  install_dashboard_hook.py) into --dest. It never edits a dashboard HTML.
* --with-watcher also copies recovery_watch.py (+ recovery_sync.py, which it imports). It never starts it.
* Prints the installed version and sha256 so a panel owner can confirm what they got.
* With --panel-name "<Panel>" it also runs the installed script's `setup` command in the current folder
  (config if missing, output folder, selfcheck, sweep, code-sweep). That is a dry run unless --apply.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


def _git(args: list[str], cwd: str | None = None) -> str:
    cmd = ["git"]
    token = os.environ.get("FATHOM_RECOVERY_GIT_TOKEN")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    r = subprocess.run(cmd + args, cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if token:
            msg = msg.replace(token, "***")
        raise RuntimeError(f"git {args[0]} failed: {msg}")
    return r.stdout.strip()


DASHBOARD_FILES = ("recovery_sync.py", "recovery_hook.js", "install_dashboard_hook.py")
WATCHER_FILE = "recovery_watch.py"


def sync(repo: str, ref: str, dest: str, run_tests: bool = False, with_dashboard: bool = False,
         with_watcher: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="fathom_recovery_sync_")
    try:
        checkout = os.path.join(tmp, "repo")
        _git(["clone", "--quiet", "--depth", "1", "--branch", ref, repo, checkout])
        commit = _git(["rev-parse", "HEAD"], cwd=checkout)
        src = os.path.join(checkout, "fathom_recovery.py")
        if not os.path.isfile(src):
            raise RuntimeError("fathom_recovery.py not found in the repo at that ref")
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        tests = None
        if run_tests:
            t = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                               cwd=checkout, capture_output=True, text=True, timeout=300,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            tests = {"ok": t.returncode == 0, "summary": (t.stderr.strip().splitlines() or [""])[-1]}
            if not tests["ok"]:
                raise RuntimeError("tests failed in fetched checkout: " + tests["summary"])
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, "fathom_recovery.py")
        shutil.copyfile(src, target)
        with open(target, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        dashboard = None
        if with_dashboard:
            missing = [n for n in DASHBOARD_FILES if not os.path.isfile(os.path.join(checkout, n))]
            if missing:
                raise RuntimeError("dashboard scripts not in the repo at that ref: " + ", ".join(missing))
            dashboard = {}
            for n in DASHBOARD_FILES:
                shutil.copyfile(os.path.join(checkout, n), os.path.join(dest, n))
                with open(os.path.join(dest, n), "rb") as fh:
                    dashboard[n] = hashlib.sha256(fh.read()).hexdigest()[:12]
        watcher = None
        if with_watcher:
            need = (WATCHER_FILE,) + tuple(n for n in DASHBOARD_FILES if n == "recovery_sync.py")
            missing = [n for n in need if not os.path.isfile(os.path.join(checkout, n))]
            if missing:
                raise RuntimeError("watcher files not in the repo at that ref: " + ", ".join(missing))
            watcher = {}
            for n in need:                                  # the watcher imports recovery_sync.py
                shutil.copyfile(os.path.join(checkout, n), os.path.join(dest, n))
                with open(os.path.join(dest, n), "rb") as fh:
                    watcher[n] = hashlib.sha256(fh.read()).hexdigest()[:12]
        return {
            "ok": True,
            "ref": ref,
            "commit": commit[:12],
            "version": m.group(1) if m else None,
            "installed_to": target,
            "sha256": sha,
            "tests": tests,
            "dashboard_scripts": dashboard,
            "watcher_scripts": watcher,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_setup(installed: str, panel_name: str, output_dir: str, apply: bool = False) -> dict:
    """Run the installed copy's `setup` (config if missing, output folder, selfcheck, sweep, code-sweep).

    Dry run unless apply=True. Local writes are limited to recovery_config.json (if missing), the output
    folder (if missing) and, with apply, .fathom_recovery/ state files.
    """
    cmd = [sys.executable, "-B", installed, "setup", "--panel-name", panel_name, "--output-dir", output_dir]
    if apply:
        cmd.append("--apply")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    out = (r.stdout + r.stderr).strip()
    return {"ok": r.returncode == 0, "exit_code": r.returncode, "applied": apply,
            "summary": [ln for ln in out.splitlines() if ln.startswith("SUMMARY")],
            "tail": out.splitlines()[-12:]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Install fathom_recovery.py from the shared repo")
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True, help="tag (recommended) or branch")
    p.add_argument("--dest", default="scripts")
    p.add_argument("--run-tests", action="store_true")
    p.add_argument("--with-dashboard", action="store_true",
                   help="also install recovery_sync.py, recovery_hook.js and install_dashboard_hook.py into --dest "
                        "(does NOT edit any dashboard HTML; run install_dashboard_hook.py --apply for that)")
    p.add_argument("--with-watcher", action="store_true",
                   help="also install recovery_watch.py (and recovery_sync.py, which it imports) into --dest. "
                        "It is NEVER started by this script")
    p.add_argument("--panel-name", default=None,
                   help="also run the installed script's `setup` for this panel (dry run unless --apply)")
    p.add_argument("--output-dir", default="outputs", help="folder the panel writes data files to (with --panel-name)")
    p.add_argument("--apply", action="store_true", help="with --panel-name: really write to S3")
    a = p.parse_args(argv)
    try:
        res = sync(a.repo, a.ref, a.dest, a.run_tests, a.with_dashboard, a.with_watcher)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    if a.panel_name:
        res["setup"] = run_setup(res["installed_to"], a.panel_name, a.output_dir, a.apply)
        res["ok"] = bool(res["setup"]["ok"])
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
