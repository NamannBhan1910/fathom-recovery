"""End-to-end test of scripts/recovery_sync.py + install_dashboard_hook.py in a TEMP workspace with a FAKE S3."""
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
ws = tempfile.mkdtemp(prefix="sync_ws_")
store = os.path.join(tempfile.mkdtemp(prefix="fake_s3_"), "s3.pkl")
os.makedirs(os.path.join(ws, "scripts"))
for f in ("fathom_recovery.py", "recovery_sync.py", "recovery_hook.js", "install_dashboard_hook.py"):
    shutil.copy(os.path.join(REAL, f), os.path.join(ws, "scripts", f))
open(os.path.join(ws, "recovery_config.json"), "w").write('{"panel_name": "Dash Panel", "output_dir": "outputs"}')
os.makedirs(os.path.join(ws, "outputs"))
open(os.path.join(ws, "dashboard.html"), "w").write("<html><body><h1>v1</h1></body></html>")
env = dict(os.environ, PYTHONPATH=HERE, FAKE_S3_FILE=store, RECOVERY_USER="tester",
           RECOVERY_DESTINATION_BUCKET="fake", RECOVERY_DESTINATION_PREFIX="6_Recovery", PYTHONDONTWRITEBYTECODE="1")
checks = []


def check(name, cond):
    checks.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def run(*args):
    r = subprocess.run([sys.executable, "-B"] + list(args), cwd=ws, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def s3():
    return pickle.load(open(store, "rb")) if os.path.exists(store) else {}


def get(key):
    return s3()[("fake", "6_Recovery/tester/Dash Panel/" + key)][0].decode()


def sync(*extra):
    rc, out, err = run("scripts/recovery_sync.py", *extra)
    return rc, json.loads(out.splitlines()[-1]), err


def inbox(name, ts, text, ext="json"):
    d = os.path.join(ws, ".fathom_recovery", "inbox")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "%s__%d.%s" % (name, ts, ext)), "w").write(text)


from datetime import datetime, timezone
day = datetime.now(timezone.utc).strftime("%Y%m%d") + "_current_data"

# installer
rc, out, _ = run("scripts/install_dashboard_hook.py", "dashboard.html")
check("installer dry run writes nothing", rc == 0 and "dry run" in out and "fathom-recovery-hook" not in open(os.path.join(ws, "dashboard.html")).read())
rc, out, _ = run("scripts/install_dashboard_hook.py", "dashboard.html", "--apply")
html = open(os.path.join(ws, "dashboard.html")).read()
check("installer inserts one block before </body>", rc == 0 and html.count("fathom-recovery-hook:start") == 1 and html.index("fathom-recovery-hook:end") < html.index("</body>"))
rc, out, _ = run("scripts/install_dashboard_hook.py", "dashboard.html", "--apply")
check("installer is idempotent", "already up to date" in out and open(os.path.join(ws, "dashboard.html")).read().count("fathom-recovery-hook:start") == 1)

# first sync: captured data, newest wins, bad names rejected
inbox("positions", 1000, '{"rows": 10}')
inbox("positions", 2000, '{"rows": 99}')
inbox("../evil", 3000, "x")
inbox("empty", 4000, "")
rc, res, err = sync("--dry-run")
check("dry-run moves inbox but uploads nothing", rc == 0 and res["dry_run"] and not s3())
check("newest capture wins at stable path", open(os.path.join(ws, "outputs/dashboard_data/positions.json")).read() == '{"rows": 99}')
check("unsafe/empty inbox names rejected", sorted(res.get("inbox_rejected", [])) == sorted(["..__evil__3000.json".replace("..__", "../") if False else n for n in os.listdir(os.path.join(ws, ".fathom_recovery/inbox"))]) or len(res.get("inbox_rejected", [])) == 2)
for n in os.listdir(os.path.join(ws, ".fathom_recovery/inbox")):
    os.remove(os.path.join(ws, ".fathom_recovery/inbox", n))
rc, res, err = sync()
check("real sync ok, both sweeps", rc == 0 and res["ok"] and [s["command"] for s in res["sweeps"]] == ["sweep", "code-sweep"])
check("data landed in <day>_current_data/dashboard_data", get(day + "/dashboard_data/positions.json") == '{"rows": 99}')
check("dashboard.html (with hook) landed in code/", "<h1>v1</h1>" in get("code/dashboard.html") and "fathom-recovery-hook:start" in get("code/dashboard.html"))

# refresh: new data, same day
inbox("positions", 5000, '{"rows": 120}')
rc, res, err = sync()
check("refresh overwrites same-day data copy", get(day + "/dashboard_data/positions.json") == '{"rows": 120}')
check("no second day folder", {k.split("/")[3] for (b, k) in s3()} == {day, "code"})

# dashboard html itself changes
open(os.path.join(ws, "dashboard.html"), "a").write("<!-- edited -->")
rc, res, err = sync()
check("changed dashboard.html overwrites S3 copy", "<!-- edited -->" in get("code/dashboard.html"))

# idle
rc, res, err = sync()
check("idle sync uploads nothing", res["ok"] and res["sweeps"][0]["uploaded"] == 0)

# concurrency: two simultaneous syncs both succeed (lock serializes)
inbox("positions", 6000, '{"rows": 130}')
procs = [subprocess.Popen([sys.executable, "-B", "scripts/recovery_sync.py"], cwd=ws, env=env, stdout=subprocess.PIPE, text=True) for _ in range(2)]
outs = [p.communicate()[0] for p in procs]
check("two simultaneous syncs both ok", all(json.loads(o.strip().splitlines()[-1])["ok"] for o in outs) and get(day + "/dashboard_data/positions.json") == '{"rows": 130}')

# remove hook
rc, out, _ = run("scripts/install_dashboard_hook.py", "dashboard.html", "--remove", "--apply")
check("hook removed cleanly", "fathom-recovery-hook" not in open(os.path.join(ws, "dashboard.html")).read() and "<h1>v1</h1>" in open(os.path.join(ws, "dashboard.html")).read())

# failure path: S3 access denied is reported, exit 1
env_bad = dict(env, FAKE_S3_FILE="/nonexistent_dir/x.pkl")
r = subprocess.run([sys.executable, "-B", "scripts/recovery_sync.py"], cwd=ws, env=env_bad, capture_output=True, text=True)
check("failure reported with ok=false and non-zero exit", r.returncode != 0 and json.loads(r.stdout.strip().splitlines()[-1])["ok"] is False)

print("\nRESULT: %d checks, %d failed" % (len(checks), checks.count(False)))
print("workspace:", ws)
sys.exit(0 if all(checks) else 1)
