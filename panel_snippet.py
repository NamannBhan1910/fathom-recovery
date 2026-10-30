"""Paste-in block for a panel's generation code (fathom_recovery v1.9.x).

v1.9.x is a command-line tool: it has no importable upload functions (that API belonged to v1.7.x and older).
This snippet runs the installed CLI for you and returns what it printed, so a panel's generation code can
back up what it just wrote without knowing any S3 details.

Layout in S3:   6_Recovery/<user>/<Panel>/
                    <YYYYMMDD>_current_data/<path inside outputs/>   data files, one folder per UTC day
                    code/<path>                                       whole workspace, ONE live copy

Nothing here runs by itself; the panel's code (or its agent) has to call one of:
  sweep_now()       data files of outputs/  -> today's <YYYYMMDD>_current_data/   (call after the files are written)
  code_sweep_now()  whole workspace         -> code/                              (call after code/knowledge changed)

Both only upload NEW or CHANGED files, so calling them after every run is safe.
The panel name, output folder and state files come from recovery_config.json (written by setup_panel.py or
`fathom_recovery.py setup`), so there is nothing to configure in this file.
"""
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "recovery_config.json"
_SUMMARY = re.compile(r"SUMMARY uploaded=(\d+) unchanged=(\d+) failed=(\d+)"
                      r"(?: skipped_sensitive=(\d+) skipped_large=(\d+))?")


def _root() -> str:
    """Workspace root = the folder that holds recovery_config.json (env FATHOM_PANEL_ROOT, cwd, then next to this file)."""
    for cand in (os.environ.get("FATHOM_PANEL_ROOT"), os.getcwd(), _HERE, os.path.dirname(_HERE)):
        if cand and os.path.isfile(os.path.join(cand, CONFIG_NAME)):
            return cand
    raise FileNotFoundError(f"{CONFIG_NAME} not found (looked in the cwd and next to this file); "
                            "run setup_panel.py or `fathom_recovery.py setup --panel-name ...` first")


def _script(root: str) -> str:
    for cand in (os.path.join(root, "scripts", "fathom_recovery.py"), os.path.join(root, "fathom_recovery.py"),
                 os.path.join(_HERE, "fathom_recovery.py")):
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError("fathom_recovery.py not found (expected scripts/fathom_recovery.py)")


def _run(command: str, state_name: str, timeout: int = 900) -> dict:
    root = _root()
    os.makedirs(os.path.join(root, ".fathom_recovery"), exist_ok=True)
    cmd = [sys.executable, "-B", _script(root), command, "--config", CONFIG_NAME,
           "--state-file", os.path.join(".fathom_recovery", state_name), "--apply"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    m = _SUMMARY.search(out)
    res = {
        "command": command,
        "exit_code": r.returncode,                       # 0 ok, 1 some file failed, 2 STOP (credentials / AccessDenied / bucket)
        "uploaded": int(m.group(1)) if m else None,
        "unchanged": int(m.group(2)) if m else None,
        "failed": int(m.group(3)) if m else None,
        "skipped_sensitive": int(m.group(4)) if m and m.group(4) is not None else None,
        "skipped_large": int(m.group(5)) if m and m.group(5) is not None else None,
        "output": out,
    }
    res["ok"] = r.returncode == 0 and m is not None and res["failed"] == 0
    return res


def _report(res: dict) -> dict:
    if res["ok"]:
        print(f"Recovery {res['command']}: uploaded={res['uploaded']} unchanged={res['unchanged']} failed=0")
    else:
        print(f"RECOVERY {res['command'].upper()} PROBLEM (exit {res['exit_code']}):\n{res['output'][-1500:]}")
    return res


def sweep_now() -> dict:
    """Upload every NEW or CHANGED data file of outputs/ into today's <YYYYMMDD>_current_data/ folder."""
    return _report(_run("sweep", "state.json"))


def code_sweep_now() -> dict:
    """Upload every NEW or CHANGED file of the whole workspace (code, knowledge/, hidden files) into code/."""
    return _report(_run("code-sweep", "code_state.json"))


# Examples (put them LAST in the generation code, after the files are fully written):
#   sweep_now()
#   code_sweep_now()
