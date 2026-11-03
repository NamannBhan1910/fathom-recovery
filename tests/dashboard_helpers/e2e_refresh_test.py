"""End-to-end: dashboard with the hook installed -> three 'refreshes' (new data, newer data, same data) -> what ends up in a
FAKE S3. Real scripts, real python subprocesses, Node plays the browser. No real bucket is touched."""
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.dirname(os.path.dirname(HERE))   # repo root (flat layout)
ws = tempfile.mkdtemp(prefix="e2e_ws_")
store = os.path.join(tempfile.mkdtemp(prefix="e2e_s3_"), "s3.pkl")
os.makedirs(ws + "/scripts"); os.makedirs(ws + "/outputs"); os.makedirs(ws + "/app")
for f in ("fathom_recovery.py", "recovery_sync.py", "recovery_hook.js", "install_dashboard_hook.py"):
    shutil.copy(os.path.join(REAL, f), ws + "/scripts/" + f)
open(ws + "/recovery_config.json", "w").write('{"panel_name": "Dash Panel", "output_dir": "outputs"}')
# a FRAGMENT dashboard (no <html>/<head>/<body>), whose own script calls Panel straight away - like the real ones
DASH = """<style>.k{color:#222}</style>
<div id="app" class="k">positions</div>
<script>
(async function () {
  var r = await window.Panel.runFile('scripts/get_positions.py', { args: '--book A' });
  await window.Panel.readFile('app/notes.txt');
  window.__render = JSON.parse(r.result.stdout).rows;
})();
</script>
"""
open(ws + "/app/dashboard.html", "w").write(DASH)
env = dict(os.environ, PYTHONPATH=HERE, FAKE_S3_FILE=store, RECOVERY_USER="tester", RECOVERY_DESTINATION_BUCKET="fake",
           RECOVERY_DESTINATION_PREFIX="6_Recovery", PYTHONDONTWRITEBYTECODE="1")
checks = []
BASE = "6_Recovery/tester/Dash Panel/"


def check(n, c):
    checks.append(bool(c))
    print(("PASS " if c else "FAIL ") + n)


def s3():
    return pickle.load(open(store, "rb")) if os.path.exists(store) else {}


def get(key):
    return s3()[("fake", BASE + key)][0].decode()


def page(rows):
    r = subprocess.run(["node", os.path.join(HERE, "page_sim.js"), ws, str(rows)], cwd=ws, env=env, capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr


def snap():
    p = os.path.join(tempfile.mkdtemp(prefix="snap_"), "snapshot.html")
    open(p, "w").write(get("code/outputs/dashboard_snapshot/dashboard.html"))
    r = subprocess.run(["node", os.path.join(HERE, "snapshot_check.js"), p], capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


from datetime import datetime, timezone
day = datetime.now(timezone.utc).strftime("%Y%m%d") + "_current_data"

r = subprocess.run([sys.executable, "-B", "scripts/install_dashboard_hook.py", "--apply"], cwd=ws, env=env, capture_output=True, text=True)
t = open(ws + "/app/dashboard.html").read()
check("installer auto-detects the fragment dashboard and puts the hook on top",
      r.returncode == 0 and "app/dashboard.html" in r.stdout and t.index("fathom-recovery-hook:start") < t.index('<div id="app"'))

res, err = page(10)
lr = res["lastResult"] or {}
check("refresh 1: page rendered and sync ok (hook was active before the dashboard's own call)", res["rendered_rows"] == 10 and lr.get("ok"))
check("refresh 1: snapshot built", (lr.get("snapshot") or {}).get("written") is True and lr["snapshot"]["source"] == "app/dashboard.html")
orig1 = get("code/app/dashboard.html")
check("S3 has the live dashboard (with hook) under code/", "fathom-recovery-hook:start" in orig1 and 'id="app"' in orig1)
check("S3 has the day-folder data file with the responses", "get_positions" in get(day + "/dashboard_data/dashboard_responses.json"))
sn = get("code/outputs/dashboard_snapshot/dashboard.html")
check("S3 snapshot carries the shim and not the live hook", "fathom-snapshot:start" in sn and "fathom-recovery-hook" not in sn)
c = snap()
check("snapshot opened outside Fathom returns refresh-1 data (exact args)", json.loads(c["exact"]["result"]["stdout"])["rows"] == 10)
check("other args fall back to the newest capture; uncaptured call reports clearly; readFile replayed",
      json.loads(c["otherArgs"]["result"]["stdout"])["rows"] == 10 and c["missing"]["success"] is False and c["read"]["success"] is True)

res, err = page(25)
lr = res["lastResult"] or {}
check("refresh 2 (new records): sync ok", lr.get("ok") and res["rendered_rows"] == 25)
c = snap()
check("snapshot in S3 now returns the NEW records (25), not the old ones", json.loads(c["exact"]["result"]["stdout"])["rows"] == 25)
check("same-day data file overwritten, still ONE day folder",
      json.loads(json.loads(get(day + "/dashboard_data/dashboard_responses.json"))["responses"]["runFile|scripts/get_positions.py|--book A"]["r"]["result"]["stdout"])["rows"] == 25
      and {k.split("/")[3] for (b, k) in s3()} == {day, "code"})
check("live dashboard copy in S3 untouched by a data-only refresh", get("code/app/dashboard.html") == orig1)

sn2 = get("code/outputs/dashboard_snapshot/dashboard.html")
res, err = page(25)
lr = res["lastResult"] or {}
ups = [s.get("uploaded") for s in lr.get("sweeps", [])]
check("refresh 3 (same data): nothing uploaded, snapshot unchanged", lr.get("ok") and ups == [0, 0] and lr["snapshot"]["written"] is False
      and get("code/outputs/dashboard_snapshot/dashboard.html") == sn2)

d2 = DASH.replace(">positions<", ">positions v2<")
open(ws + "/app/dashboard.html", "w").write(d2)
subprocess.run([sys.executable, "-B", "scripts/install_dashboard_hook.py", "--apply"], cwd=ws, env=env, capture_output=True, text=True)
res, err = page(25)
check("editing the dashboard itself updates BOTH the live copy and the snapshot in S3",
      "positions v2" in get("code/app/dashboard.html") and "positions v2" in get("code/outputs/dashboard_snapshot/dashboard.html"))

r = subprocess.run([sys.executable, "-B", "scripts/install_dashboard_hook.py", "--remove", "--apply"], cwd=ws, env=env, capture_output=True, text=True)
check("--remove restores the dashboard byte for byte", open(ws + "/app/dashboard.html").read() == d2)

print("\nRESULT: %d checks, %d failed" % (len(checks), checks.count(False)))
print("workspace:", ws)
sys.exit(0 if all(checks) else 1)
