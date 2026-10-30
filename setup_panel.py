#!/usr/bin/env python3
"""One-command setup of fathom-recovery v1.9.x in a panel (standard library only; needs boto3 to run the script).

Run it from the panel's workspace root, using the copy inside the cloned repo:

    python3 fathom-recovery-v1.9.2/setup_panel.py --panel-name "My Panel" --output-dir outputs [--apply]

Steps (each prints PASS / FAIL / SKIP):
  1. panel name is valid (empty, '.', '..', '<' and '>' are rejected)
  2. install scripts/fathom_recovery.py from the clone this file sits in (exact tag, no network needed)
  3. run the installed script's own `setup` command. It writes recovery_config.json if missing (an
     existing config is kept), creates the output folder if missing, runs selfcheck (read-only; with --apply it
     writes one selfcheck_probe.txt into today's <YYYYMMDD>_current_data/ folder), and then the first
     data sweep (outputs/ -> <YYYYMMDD>_current_data/) and the first code-sweep (whole workspace -> code/).

Dry run by default: nothing is written to S3 unless --apply is given. Safe to run again at any time.
Exit code 0 only if no step failed.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> bool:
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")
    return status != "FAIL"


def check_panel_name(name: str) -> str:
    n = (name or "").strip()
    if not n or n in (".", "..") or "<" in name or ">" in name:
        raise ValueError(f"invalid panel name: {name!r}")
    return name


def script_version(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.M)
    return m.group(1) if m else "unknown"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Set up and check fathom-recovery in this panel")
    p.add_argument("--panel-name", required=True)
    p.add_argument("--output-dir", default="outputs", help="folder this panel writes data files to")
    p.add_argument("--dest", default="scripts", help="where to install fathom_recovery.py")
    p.add_argument("--apply", action="store_true", help="write to S3 (selfcheck probe, data sweep, code sweep)")
    p.add_argument("--with-snippet", action="store_true", help="also copy panel_snippet.py into the panel")
    p.add_argument("--user", default=None, help="override the user folder (default: RECOVERY_USER, whoami, unknown-user)")
    a = p.parse_args(argv)

    src = os.path.join(HERE, "fathom_recovery.py")
    if not os.path.isfile(src):
        record("2 install", "FAIL", f"fathom_recovery.py not found next to setup_panel.py ({HERE})")
        return 1

    # 1. name (the script validates it too; this gives an early, clear message)
    try:
        check_panel_name(a.panel_name)
        record("1 panel name", "PASS", repr(a.panel_name))
    except ValueError as exc:
        record("1 panel name", "FAIL", str(exc))
        return 1

    # 2. install
    os.makedirs(a.dest, exist_ok=True)
    target = os.path.join(a.dest, "fathom_recovery.py")
    with open(src, "rb") as fh:
        new = fh.read()
    same = os.path.isfile(target) and open(target, "rb").read() == new
    if not same:
        shutil.copyfile(src, target)
    record("2 install", "PASS", f"{target} version {script_version(target)}" + (" (already up to date)" if same else " (installed)"))
    if a.with_snippet:
        snip = os.path.join(HERE, "panel_snippet.py")
        if os.path.isfile(snip):
            shutil.copyfile(snip, "panel_snippet.py")
            record("2b snippet", "PASS", "copied panel_snippet.py (reads recovery_config.json; nothing to edit)")
        else:
            record("2b snippet", "FAIL", "panel_snippet.py not found in the clone")

    # 3. the installed script's own setup (config, output folder, selfcheck, sweep, code-sweep)
    cmd = [sys.executable, "-B", target, "setup", "--panel-name", a.panel_name, "--output-dir", a.output_dir]
    if a.user:
        cmd += ["--user", a.user]
    if a.apply:
        cmd.append("--apply")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
    out = (r.stdout + r.stderr).strip()
    print(out)
    summary = [ln for ln in out.splitlines() if ln.startswith("SUMMARY")]
    detail = " | ".join(summary) if summary else (out.splitlines() or [""])[-1][:300]
    if r.returncode == 0:
        record("3 setup" + (" --apply" if a.apply else " (dry run)"), "PASS", detail or "ok")
    else:
        record("3 setup" + (" --apply" if a.apply else " (dry run)"), "FAIL", f"exit {r.returncode}: {detail}")

    failed = [x for x in results if x[1] == "FAIL"]
    print("\nALL STEPS PASSED" if not failed else "\nSOME STEPS FAILED - fix the first FAIL above")
    if any(k in out for k in ("AccessDenied", "NoCredentialsError", "Unable to locate credentials", "STOP:")):
        print("(AccessDenied / credentials errors: this panel's runtime has no S3 permission on 6_Recovery/.)")
    if not a.apply:
        print("(dry run: nothing was written to S3; add --apply to check write access and upload)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
