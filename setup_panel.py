#!/usr/bin/env python3
"""One-command setup / health check of fathom-recovery in a panel (standard library + git-free).

Run it from the panel's workspace root, using the copy inside the cloned repo:

    python3 fathom-recovery-v1.6.0/setup_panel.py --panel-name "My Panel" --output-dir outputs [--apply]

Steps (each prints PASS / FAIL / SKIP):
  1. panel name is valid
  2. install scripts/fathom_recovery.py (copied from the clone this file sits in, so the version is
     exactly the tag that was cloned; no network needed)
  3. write recovery_config.json + RECOVERY_INSTRUCTIONS.md (an existing config is kept, never overwritten)
  4. selfcheck (read-only; with --apply it writes and verifies one small probe file in S3)
  5. sweep of the output folder (dry run; with --apply it uploads new/changed files)
  6. the output folder named in the config exists (created if missing)

Dry run by default: nothing is written to S3 unless --apply is given. Safe to run again at any time.
Exit code 0 only if no step failed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> bool:
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")
    return status != "FAIL"


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr).strip()


def parse(out: str):
    try:
        return json.loads(out)
    except ValueError:
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Set up and check fathom-recovery in this panel")
    p.add_argument("--panel-name", required=True)
    p.add_argument("--output-dir", default="outputs", help="folder this panel writes data files to")
    p.add_argument("--dest", default="scripts", help="where to install fathom_recovery.py")
    p.add_argument("--apply", action="store_true", help="write to S3 (selfcheck probe + sweep upload)")
    p.add_argument("--with-snippet", action="store_true", help="also copy panel_snippet.py into the panel")
    p.add_argument("--skip-selfcheck", action="store_true")
    a = p.parse_args(argv)

    src = os.path.join(HERE, "fathom_recovery.py")
    if not os.path.isfile(src):
        record("2 install", "FAIL", f"fathom_recovery.py not found next to setup_panel.py ({HERE})")
        return 1

    # 1. name (the library validates it too; this gives an early, clear message)
    sys.path.insert(0, HERE)
    try:
        import fathom_recovery as fr
        fr._require_panel(a.panel_name)
        panel = " ".join(a.panel_name.split())
        record("1 panel name", "PASS", repr(panel))
    except ValueError as exc:
        record("1 panel name", "FAIL", str(exc))
        return 1
    except ImportError as exc:  # boto3 missing
        record("1 panel name", "FAIL", f"cannot import fathom_recovery ({exc}); is boto3 installed for {sys.executable}?")
        return 1

    # 2. install
    os.makedirs(a.dest, exist_ok=True)
    target = os.path.join(a.dest, "fathom_recovery.py")
    same = os.path.isfile(target) and open(target, "rb").read() == open(src, "rb").read()
    if not same:
        shutil.copyfile(src, target)
    record("2 install", "PASS", f"{target} version {fr.__version__}" + (" (already up to date)" if same else " (installed)"))
    if a.with_snippet:
        snip = os.path.join(HERE, "panel_snippet.py")
        if os.path.isfile(snip):
            shutil.copyfile(snip, "panel_snippet.py")
            record("2b snippet", "PASS", "copied panel_snippet.py (set PANEL_NAME in it before use)")
        else:
            record("2b snippet", "FAIL", "panel_snippet.py not found in the clone")

    # 3. config + instructions (never overwrite an existing config)
    rc, out = run([sys.executable, target, "init", "--panel-name", panel, "--output-dir", a.output_dir])
    d = parse(out) or {}
    if rc == 0 and d.get("ok"):
        record("3 config", "PASS", f"wrote {fr.CONFIG_NAME} and {fr.INSTRUCTIONS_NAME}")
    elif "already exists" in str(d.get("error", "")):
        record("3 config", "PASS", f"{fr.CONFIG_NAME} already exists - kept as is")
        if not os.path.isfile(fr.INSTRUCTIONS_NAME):
            with open(fr.INSTRUCTIONS_NAME, "w", encoding="utf-8") as fh:
                fh.write(fr.render_instructions(panel, a.output_dir, fr.CONFIG_NAME))
            record("3b instructions", "PASS", f"wrote missing {fr.INSTRUCTIONS_NAME}")
    else:
        record("3 config", "FAIL", (d.get("error") or out)[-300:])
        return 1

    # 6 (done early so sweep has a folder). Uses the config that is really in effect.
    try:
        cfg = fr.load_config(fr.CONFIG_NAME)
        out_dir = cfg["output_dir_resolved"]
        existed = os.path.isdir(out_dir)
        os.makedirs(out_dir, exist_ok=True)
        record("6 output folder", "PASS", f"{cfg['output_dir']} ({'exists' if existed else 'created, empty for now'}); "
               "make sure your panel really writes its data files there")
    except ValueError as exc:
        record("6 output folder", "FAIL", str(exc))
        return 1

    # 4. selfcheck
    if a.skip_selfcheck:
        record("4 selfcheck", "SKIP", "--skip-selfcheck")
    else:
        cmd = [sys.executable, target, "selfcheck", "--panel-name", cfg["panel_name"]] + (["--apply"] if a.apply else [])
        rc, out = run(cmd)
        d = parse(out)
        if d is None:
            record("4 selfcheck", "FAIL", out[-300:])
        else:
            detail = "; ".join(f"{s['step']}={'ok' if s['ok'] else 'FAILED: ' + str(s['detail'])[:160]}" for s in d["steps"])
            record("4 selfcheck" + (" --apply" if a.apply else " (dry run)"), "PASS" if d["ok"] else "FAIL", detail)

    # 5. sweep
    cmd = [sys.executable, target, "sweep", "--config", fr.CONFIG_NAME] + (["--apply"] if a.apply else [])
    rc, out = run(cmd)
    d = parse(out)
    if d is None:
        record("5 sweep", "FAIL", out[-300:])
    else:
        detail = (f"scanned={d['scanned']} uploaded={d['uploaded']} would_upload={d['would_upload']} "
                  f"unchanged={d['unchanged']} failed={d['failed']}")
        errs = [f.get("error", "") for f in d.get("files", []) if f.get("action") == "failed"]
        if errs:
            detail += " | " + errs[0][:160]
        record("5 sweep" + (" --apply" if a.apply else " (dry run)"), "PASS" if d["ok"] else "FAIL", detail)

    failed = [r for r in results if r[1] == "FAIL"]
    print("\nALL STEPS PASSED" if not failed else "\nSOME STEPS FAILED - fix the first FAIL above")
    if any("AccessDenied" in r[2] or "NoCredentialsError" in r[2] or "Unable to locate credentials" in r[2] for r in failed):
        print("(AccessDenied / credentials errors: this panel's runtime has no S3 permission on 6_Recovery/.)")
    if not a.apply:
        print("(dry run: nothing was written to S3; add --apply to check write access and upload)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
