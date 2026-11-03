#!/usr/bin/env python3
"""Add (or remove) the recovery hook in a dashboard HTML file. Dry run unless --apply.

    python3 scripts/install_dashboard_hook.py --apply                   # auto-detects the dashboard
    python3 scripts/install_dashboard_hook.py dashboard.html --apply    # or name it
    python3 scripts/install_dashboard_hook.py --remove --apply

The hook is ONE block between <!-- fathom-recovery-hook:start --> and <!-- fathom-recovery-hook:end -->,
inserted at the TOP (right after <head>, else after <html>, else after a doctype, else at the start of a
fragment) so it is active before the dashboard's own scripts run. Running it again replaces that block;
nothing else in the file is touched, and --remove restores the original bytes. Written atomically.

This module is also imported by recovery_sync.py (discover, insert_block, snapshot_block).
"""
import argparse
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True
START = "<!-- fathom-recovery-hook:start -->"
END = "<!-- fathom-recovery-hook:end -->"
SNAP_START = "<!-- fathom-snapshot:start -->"
SNAP_END = "<!-- fathom-snapshot:end -->"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.S)
HEAD_RE = re.compile(r"<head(?:\s[^>]*)?>", re.I)
HTML_RE = re.compile(r"<html(?:\s[^>]*)?>", re.I)
DOCTYPE_RE = re.compile(r"^\s*<!doctype[^>]*>", re.I)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages", "knowledge",
             ".fathom_runtime", ".fathom_recovery", "restored_code", "dashboard_snapshot"}

# Replays what the dashboard displayed at its last refresh. Only defines window.Panel when none exists
# (i.e. the page was opened from S3, outside Fathom), or when window.FATHOM_SNAPSHOT_FORCE is set.
SHIM = r"""(function () {
  var D = __DATA__;
  if (window.Panel && !window.FATHOM_SNAPSHOT_FORCE) { return; }
  function argKey(a) { return a === undefined || a === null ? "" : (typeof a === "string" ? a : JSON.stringify(a)); }
  function find(m, p, a) {
    var R = D.responses, k = m + "|" + p + "|" + argKey(a);
    if (Object.prototype.hasOwnProperty.call(R, k)) { return R[k].r; }
    var pre = m + "|" + p + "|", best = null;
    Object.keys(R).forEach(function (x) { if (x.indexOf(pre) === 0 && (!best || R[x].t > best.t)) { best = R[x]; } });
    return best ? best.r : undefined;
  }
  function make(m) {
    return async function (p, o) {
      var r = find(m, p, m === "runFile" ? (o && o.args) : undefined);
      return r !== undefined ? r : { success: false, error: "not captured in this snapshot (" + m + " " + p + ")" };
    };
  }
  window.Panel = {
    runFile: make("runFile"), readFile: make("readFile"), readData: make("readData"),
    writeFile: async function () { return { success: true, snapshot: true }; }
  };
  window.FATHOM_SNAPSHOT = { updated_utc: D.updated_utc, responses: Object.keys(D.responses).length };
})();"""


def sha(b):
    return hashlib.sha256(b).hexdigest()[:12]


def insert_block(base, block):
    """Insert a block at the top: after <head>, else <html>, else a doctype, else at the very start."""
    for rx in (HEAD_RE, HTML_RE, DOCTYPE_RE):
        m = rx.search(base)
        if m:
            return base[:m.end()] + block + base[m.end():]
    return block + base


def hook_block(js):
    return START + "\n<script>\n" + js.strip() + "\n</script>\n" + END + "\n"


def snapshot_block(data):
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")
    return SNAP_START + "\n<script>\n" + SHIM.replace("__DATA__", blob, 1) + "\n</script>\n" + SNAP_END + "\n"


def discover(root=ROOT):
    """Find the dashboard HTML. Returns (chosen_path_or_None, all_candidates, reason)."""
    found = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not os.path.islink(os.path.join(dp, d))]
        for f in fns:
            if f.lower().endswith((".html", ".htm")):
                found.append(os.path.relpath(os.path.join(dp, f), root))
    found.sort()
    if not found:
        return None, found, "no .html/.htm file in the workspace"
    if len(found) == 1:
        return found[0], found, "the only HTML file"
    named = [p for p in found if "dashboard" in os.path.basename(p).lower()]
    if len(named) == 1:
        return named[0], found, "the only file named *dashboard*"
    uses_panel = []
    for p in found:
        try:
            with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                if "window.Panel" in fh.read():
                    uses_panel.append(p)
        except OSError:
            pass
    if len(uses_panel) == 1:
        return uses_panel[0], found, "the only file that uses window.Panel"
    return None, found, "several HTML files and none stands out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html", nargs="?", help="dashboard file; omitted = auto-detect the single HTML file in the workspace")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()
    if not a.html:
        chosen, found, why = discover()
        if not chosen:
            print("dashboard not identified: %s" % why)
            for p in found:
                print("  candidate: " + p)
            print("pass the file name explicitly, or ask for it to be created first. Nothing was changed.")
            return 2
        print("dashboard identified: %s (%s)" % (chosen, why))
        a.html = os.path.join(ROOT, chosen)
    if not os.path.isfile(a.html):
        print("not a file: " + a.html)
        return 1
    with open(a.html, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8")
    base = BLOCK_RE.sub("", text)
    if a.remove:
        new = base
        action = "remove hook" if base != text else "no hook present"
    else:
        with open(os.path.join(HERE, "recovery_hook.js"), encoding="utf-8") as f:
            js = f.read().strip()
        if "</script" in js.lower():
            print("hook JS contains a closing script tag; refusing")
            return 1
        new = insert_block(base, hook_block(js))
        action = "update hook" if base != text else "insert hook"
    out = new.encode("utf-8")
    print("%s: %s (%d -> %d bytes, sha256 %s -> %s)" % (a.html, action, len(raw), len(out), sha(raw), sha(out)))
    if out == raw:
        print("already up to date")
        return 0
    if not a.apply:
        print("dry run: nothing written (add --apply)")
        return 0
    tmp = a.html + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, a.html)
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
