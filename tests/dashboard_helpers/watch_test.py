"""End-to-end test of recovery_watch.py in a TEMP workspace with a FAKE S3 (a real bucket is never touched)."""
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.dirname(os.path.dirname(HERE))   # repo root (flat layout)
ws = tempfile.mkdtemp(prefix="watch_ws_")
store = os.path.join(tempfile.mkdtemp(prefix="fake_s3_"), "s3.pkl")
os.makedirs(os.path.join(ws, "scripts"))
for f in ("fathom_recovery.py", "recovery_sync.py", "recovery_watch.py", "recovery_hook.js", "install_dashboard_hook.py"):
    shutil.copy(os.path.join(REAL, f), os.path.join(ws, "scripts", f))
open(os.path.join(ws, "recovery_config.json"), "w").write('{"panel_name": "Watch Panel", "output_dir": "outputs"}')
os.makedirs(os.path.join(ws, "outputs"))
open(os.path.join(ws, "app.py"), "w").write("print('v1')\n")
env = dict(os.environ, PYTHONPATH=HERE, FAKE_S3_FILE=store, RECOVERY_USER="tester",
           RECOVERY_DESTINATION_BUCKET="fake", RECOVERY_DESTINATION_PREFIX="6_Recovery", PYTHONDONTWRITEBYTECODE="1")
checks = []


def check(name, cond):
    checks.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def s3():
    for _ in range(5):
        try:
            return pickle.load(open(store, "rb")) if os.path.exists(store) else {}
        except Exception:
            time.sleep(0.1)
    return {}


def get(key):
    v = s3().get(("fake", "6_Recovery/tester/Watch Panel/" + key))
    return v[0].decode() if v else None


def has(key):
    return ("fake", "6_Recovery/tester/Watch Panel/" + key) in s3()


def wait(cond, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


def w(rel, text):
    p = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def cmd(*a):
    r = subprocess.run([sys.executable, "-B", "scripts/recovery_watch.py"] + list(a), cwd=ws, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout.strip() + r.stderr.strip()


# --log inside the workspace is refused (it would be swept and re-trigger sweeps); nothing is started or created
rc, out = cmd("--log", os.path.join(ws, "watch.log"))
check("--log inside the workspace is refused", rc == 1 and "OUTSIDE the workspace" in out
      and not os.path.exists(os.path.join(ws, "watch.log")))
rc, out = cmd("--log", os.path.join(ws, "sub", "..", "watch.log"))
check("--log with .. that resolves inside the workspace is refused", rc == 1 and "OUTSIDE the workspace" in out)

# No way to tell the user folder (no RECOVERY_USER, no bridge): refuse instead of uploading to "unknown-user".
env_nouser = {k: v for k, v in env.items() if k != "RECOVERY_USER"}
env_nouser["PYTHONPATH"] = ""                       # nothing that could provide bridge.whoami()
for extra, label in (([], "watch"), (["--once"], "--once")):
    r = subprocess.run([sys.executable, "-B", "scripts/recovery_watch.py"] + extra, cwd=ws, env=env_nouser,
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    check("%s without a resolvable user refuses (exit 1, says unknown-user)" % label,
          r.returncode == 1 and "unknown-user" in out and "Refusing to run" in out)
check("refusing wrote nothing to the fake S3", not s3())
r = subprocess.run([sys.executable, "-B", "scripts/recovery_watch.py", "--status"], cwd=ws, env=env_nouser,
                   capture_output=True, text=True, timeout=60)
check("--status and --stop never need a user", r.returncode == 0 and '"running": false' in r.stdout)

logdir = tempfile.mkdtemp(prefix="watch_log_")
logpath = os.path.join(logdir, "watch.log")
proc = subprocess.Popen([sys.executable, "-B", "scripts/recovery_watch.py", "--interval", "0.5", "--quiet", "1",
                         "--full-every", "0", "--retry-min", "2", "--log", logpath],
                        cwd=ws, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def log():
    try:
        return open(logpath).read()
    except OSError:
        return ""


try:
    check("baseline: existing file uploaded at start", wait(lambda: get("code/app.py") == "print('v1')\n"))
    check("--log: output goes to the file outside the workspace", wait(lambda: "watching" in log() and "sweep ok" in log()))
    rc, out = cmd("--status")
    check("--status shows running watcher", '"running": true' in out)
    check("--status and the log show the pinned user folder", '"user_folder": "tester"' in out
          and "user folder pinned to 'tester'" in log())
    check("every uploaded key is under the pinned user folder",
          s3() and all(k[1].startswith("6_Recovery/tester/Watch Panel/") for k in s3()))
    rc2, out2 = cmd("--log", os.path.join(logdir, "second.log"))   # second watcher must refuse
    check("second watcher refuses to start", rc2 == 3)

    w("app.py", "print('v2')\n")
    check("edit reflected at SAME s3 key", wait(lambda: get("code/app.py") == "print('v2')\n"))

    w("lib/sub/new_mod.py", "X = 1\n")
    check("new nested file uploaded to code/lib/sub/new_mod.py", wait(lambda: get("code/lib/sub/new_mod.py") == "X = 1\n"))

    w("notes.md", "hello\n")
    check("non-python file (md) uploaded", wait(lambda: get("code/notes.md") == "hello\n"))

    w("outputs/result.csv", "a,b\n1,2\n")
    check("data file goes to <day>_current_data/", wait(lambda: any(k[1].endswith("_current_data/result.csv") for k in s3())))

    # an EMPTY folder created later is noticed and backed up as a zero-byte marker
    os.makedirs(os.path.join(ws, "empty_dir_x"))
    check("new empty folder uploaded as a marker", wait(lambda: has("code/empty_dir_x/")))

    for i in range(5):                       # burst of rapid edits -> final content wins, one settled sweep
        w("app.py", "print('burst %d')\n" % i)
        time.sleep(0.2)
    check("burst of edits: final content in S3", wait(lambda: get("code/app.py") == "print('burst 4')\n"))

    w("my_password.txt", "hunter2")
    w(".env", "KEY=1")
    time.sleep(4)
    check("sensitive files never uploaded", get("code/my_password.txt") is None and get("code/.env") is None)

    w("app.py", "print('after-sensitive')\n")
    check("watcher still works after sensitive files", wait(lambda: get("code/app.py") == "print('after-sensitive')\n"))

    # an idle period must not keep re-uploading (no sweep loop from the sweeps' own state files or from the log)
    time.sleep(4)
    n0 = log().count("sweep ok")
    time.sleep(4)
    n1 = log().count("sweep ok")
    check("idle: no sweep loop", n0 == n1)

    # deletion: S3 copy is kept (storage only)
    os.remove(os.path.join(ws, "notes.md"))
    time.sleep(4)
    check("deleted local file: S3 copy kept", get("code/notes.md") == "hello\n")

    # S3 failure then recovery: make the fake store unreadable
    good = store + ".good"
    os.replace(store, good)
    os.makedirs(store)                       # a directory where the pickle should be -> every S3 call fails
    w("app.py", "print('during-outage')\n")
    check("outage: watcher survives and reports problem", wait(lambda: "sweep PROBLEM" in log(), 30) and proc.poll() is None)
    os.rmdir(store)
    os.replace(good, store)
    check("recovery: change uploaded once S3 is back", wait(lambda: get("code/app.py") == "print('during-outage')\n", 40))

    rc, out = cmd("--stop")
    check("--stop makes watcher exit", wait(lambda: proc.poll() is not None, 10) and proc.returncode == 0)
finally:
    if proc.poll() is None:
        proc.terminate()

print("\n--- watcher log tail ---")
print("\n".join(log().splitlines()[-12:]))
print("\nRESULT: %d checks, %d failed" % (len(checks), checks.count(False)))
sys.exit(0 if all(checks) else 1)
