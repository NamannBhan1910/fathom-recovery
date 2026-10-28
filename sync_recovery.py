#!/usr/bin/env python3
"""Pull fathom_recovery.py from the shared git repo into THIS panel's workspace.

    python3 sync_recovery.py --repo <git url or path> --ref v1.1.0 [--dest scripts] [--run-tests]

* --ref should be a tag (pinned version) so later changes can't alter a panel silently.
* Private HTTPS repos: set env FATHOM_RECOVERY_GIT_TOKEN. The token is sent as an
  HTTP header and is never put in the URL or printed.
* Copies fathom_recovery.py (and, with --run-tests, runs the offline test-suite from
  the fetched checkout, which touches no S3).
* Prints the installed version and sha256 so a panel owner can confirm what they got.
* With --panel-name "<Panel>" it also writes recovery_config.json and RECOVERY_INSTRUCTIONS.md
  (the standing instruction for the panel's agent) in the current folder. Local files only.
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


def sync(repo: str, ref: str, dest: str, run_tests: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="fathom_recovery_sync_")
    try:
        checkout = os.path.join(tmp, "repo")
        _git(["clone", "--quiet", "--depth", "1", "--branch", ref, repo, checkout])
        commit = _git(["rev-parse", "HEAD"], cwd=checkout)
        src = os.path.join(checkout, "fathom_recovery.py")
        if not os.path.isfile(src):
            raise RuntimeError("fathom_recovery.py not found in the repo at that ref")
        text = open(src, encoding="utf-8").read()
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        tests = None
        if run_tests:
            t = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               cwd=checkout, capture_output=True, text=True, timeout=300)
            tests = {"ok": t.returncode == 0, "summary": (t.stderr.strip().splitlines() or [""])[-1]}
            if not tests["ok"]:
                raise RuntimeError("tests failed in fetched checkout: " + tests["summary"])
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, "fathom_recovery.py")
        shutil.copyfile(src, target)
        return {
            "ok": True,
            "ref": ref,
            "commit": commit[:12],
            "version": m.group(1) if m else None,
            "installed_to": target,
            "sha256": hashlib.sha256(open(target, "rb").read()).hexdigest(),
            "tests": tests,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def init_panel_files(installed: str, panel_name: str, output_dir: str, force: bool = False) -> dict:
    """Run the installed copy's `init` to write recovery_config.json + RECOVERY_INSTRUCTIONS.md.

    Both files go in the current folder (the panel's workspace root). Local files only; no S3.
    """
    cmd = [sys.executable, installed, "init", "--panel-name", panel_name, "--output-dir", output_dir]
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}
    if not out.get("ok") and "already exists" in str(out.get("error", "")):
        # upgrade of a panel that is already set up: keep its config, do not fail the install
        return {"ok": True, "action": "kept_existing_config", "note": out["error"]}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Install fathom_recovery.py from the shared repo")
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True, help="tag (recommended) or branch")
    p.add_argument("--dest", default="scripts")
    p.add_argument("--run-tests", action="store_true")
    p.add_argument("--panel-name", default=None,
                   help="also write recovery_config.json + RECOVERY_INSTRUCTIONS.md for this panel (local files only)")
    p.add_argument("--output-dir", default="outputs", help="folder the panel writes data files to (with --panel-name)")
    p.add_argument("--force", action="store_true", help="replace an existing recovery_config.json (with --panel-name)")
    a = p.parse_args(argv)
    try:
        res = sync(a.repo, a.ref, a.dest, a.run_tests)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    if a.panel_name:
        res["init"] = init_panel_files(res["installed_to"], a.panel_name, a.output_dir, a.force)
        res["ok"] = bool(res["init"].get("ok"))
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
