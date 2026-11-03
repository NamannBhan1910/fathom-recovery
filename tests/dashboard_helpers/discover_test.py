"""Test install_dashboard_hook.discover() in temp workspaces (no S3, nothing real touched)."""
import importlib.util
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(os.path.dirname(HERE))   # repo root (flat layout)
checks = []


def check(n, c):
    checks.append(bool(c))
    print(("PASS " if c else "FAIL ") + n)


def make(files):
    ws = tempfile.mkdtemp(prefix="disc_")
    os.makedirs(os.path.join(ws, "scripts"))
    for f in ("install_dashboard_hook.py", "recovery_hook.js"):
        shutil.copy(os.path.join(SRC, f), os.path.join(ws, "scripts", f))
    for rel, txt in files.items():
        p = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(txt)
    spec = importlib.util.spec_from_file_location("inst", os.path.join(ws, "scripts", "install_dashboard_hook.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return ws, m


ws, m = make({"x.py": "1"})
check("no html -> not identified", m.discover(ws)[0] is None)
ws, m = make({"outputs/report.html": "<html><body></body></html>"})
check("single html (in a sub-folder) is chosen", m.discover(ws)[0] == "outputs/report.html")
ws, m = make({"a.html": "x", "dashboard.html": "y", "knowledge/notes.html": "z"})
check("one *dashboard* name wins; knowledge/ ignored", m.discover(ws)[0] == "dashboard.html")
ws, m = make({"a.html": "window.Panel.runFile()", "b.html": "plain"})
check("only file using window.Panel wins", m.discover(ws)[0] == "a.html")
ws, m = make({"a.html": "plain", "b.html": "plain"})
c, found, why = m.discover(ws)
check("ambiguous -> not identified, both listed", c is None and found == ["a.html", "b.html"])
ws, m = make({"dashboard.html": "<html><body><p>hi</p></body></html>"})
import subprocess
r = subprocess.run([sys.executable, "-B", "scripts/install_dashboard_hook.py", "--apply"], cwd=ws, capture_output=True, text=True)
t = open(os.path.join(ws, "dashboard.html")).read()
check("no-argument --apply installs into the detected file", r.returncode == 0 and "dashboard identified" in r.stdout and "fathom-recovery-hook:start" in t)
r = subprocess.run([sys.executable, "-B", "scripts/install_dashboard_hook.py"], cwd=tempfile.mkdtemp(), capture_output=True, text=True)
print("RESULT: %d checks, %d failed" % (len(checks), checks.count(False)))
sys.exit(0 if all(checks) else 1)
