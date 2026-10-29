#!/usr/bin/env python3
"""Fathom panel recovery backup: copies panel files to S3. STORAGE ONLY (no deletes, ever)."""
LAYOUT = "user/panel"
CODE_BACKUP = "1"
CODE_FLAT = "1"
CODE_SCOPE = "all"
PROBE_DIR = "data-day"
__version__ = "1.9.1"

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import datetime as dt

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

DEFAULT_BUCKET = "mosaic-derivatives-margining"
DEFAULT_PREFIX = "6_Recovery"
DATA_EXT = {"csv", "xlsx", "xls", "json", "parquet", "txt", "pdf"}
SWEEP_SKIP_DIRS = {".git", ".fathom_runtime", "__pycache__", "knowledge", "node_modules", ".venv", "tests"}
CODE_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "site-packages"}
CODE_SKIP_ROOT_DIRS = {"restored_code"}
MAX_CODE_BYTES = 10 * 1024 * 1024
SENSITIVE_EXT = {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore"}
SENSITIVE_DOC_EXT = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt"}
SENSITIVE_WORDS = ["secret", "credential", "password", "passwd", "token", "private_key",
                   "service_account", "id_rsa", "apikey", "api_key"]
OLD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{8}T\d{6}Z-[0-9a-f]{8})$")
MANIFEST = ".fathom_manifest.json"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
STATE_DIR = ".fathom_recovery"
STOP_CODES = {"AccessDenied", "NoSuchBucket", "InvalidAccessKeyId", "SignatureDoesNotMatch",
              "ExpiredToken", "InvalidToken", "AllAccessDisabled"}


class StopError(Exception):
    pass


def is_stop(e):
    if isinstance(e, (NoCredentialsError, PartialCredentialsError, StopError)):
        return True
    if isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") in STOP_CODES:
        return True
    return "Unable to locate credentials" in str(e)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- names
def safe_segment(s):
    s = re.sub(r"[\x00-\x1f\x7f/\\]", " ", str(s)).strip()
    if not s or s in (".", ".."):
        raise ValueError("invalid name segment: %r" % s)
    return s


def check_panel_name(n):
    if n is None or not str(n).strip() or str(n).strip() in (".", "..") or "<" in n or ">" in n:
        raise ValueError("invalid panel name: %r" % n)
    return str(n)


def norm(s):
    return " ".join(str(s).split()).casefold()


def rel_ok(rel):
    parts = rel.rstrip("/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


def resolve_user(arg_user):
    u = arg_user or os.environ.get("RECOVERY_USER")
    if not u:
        try:
            from bridge import whoami
            w = whoami()
            if isinstance(w, str):
                try:
                    w = json.loads(w)
                except Exception:
                    w = {"username": w}
            if isinstance(w, dict):
                w = w.get("result", w) if isinstance(w.get("result"), dict) else w
                u = w.get("username") or (w.get("email") or "").split("@")[0]
        except Exception:
            u = None
    return safe_segment(u or "unknown-user")


# ---------------------------------------------------------------- s3 helpers
def list_subfolders(s3, bucket, prefix):
    names = []
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            names.append(cp["Prefix"][len(prefix):].rstrip("/"))
    return names


def list_keys(s3, bucket, prefix):
    out = []
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out.append((o["Key"], o["Size"]))
    return out


def match_folder(names, given, level):
    if given in names:
        return given
    m = [n for n in names if norm(n) == norm(given)]
    if len(m) == 1:
        return m[0]
    if len(m) > 1:
        raise SystemExit("AMBIGUOUS %s folder: several case-insensitive matches %s for %r. "
                         "The exact spelling is needed." % (level, m, given))
    return given


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def put_verified(s3, bucket, key, data, sha):
    s3.put_object(Bucket=bucket, Key=key, Body=data, Metadata={"sha256": sha})
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != len(data):
        raise RuntimeError("size mismatch after upload: %s" % key)
    if head.get("Metadata", {}).get("sha256") != sha:
        raise RuntimeError("metadata sha256 mismatch after upload: %s" % key)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if hashlib.sha256(body).hexdigest() != sha:
        raise RuntimeError("content hash mismatch after upload: %s" % key)


def get_json(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    base = os.path.basename(path).replace(".json", "")
    tmp = os.path.join(d, ".tmp_%s_%d.json" % (base, os.getpid()))
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- context
class Ctx:
    pass


def build_ctx(args, create_cfg=False):
    c = Ctx()
    cfg_path = getattr(args, "config", None)
    pn = getattr(args, "panel_name", None)
    c.cfg_path = None
    if cfg_path:
        c.cfg_path = os.path.abspath(cfg_path)
        c.root = os.path.dirname(c.cfg_path)
        if os.path.exists(c.cfg_path):
            with open(c.cfg_path) as f:
                c.cfg = json.load(f)
        elif pn:
            c.cfg = {"panel_name": pn, "output_dir": getattr(args, "output_dir", None) or "outputs"}
        else:
            raise SystemExit("config not found: %s" % cfg_path)
    elif pn:
        c.root = os.getcwd()
        c.cfg = {"panel_name": pn, "output_dir": getattr(args, "output_dir", None) or "outputs"}
    else:
        raise SystemExit("need --config or --panel-name")
    c.panel_given = check_panel_name(c.cfg.get("panel_name"))
    c.output_dir = c.cfg.get("output_dir", "outputs")
    c.bucket = os.environ.get("RECOVERY_DESTINATION_BUCKET") or DEFAULT_BUCKET
    c.prefix = (os.environ.get("RECOVERY_DESTINATION_PREFIX") or DEFAULT_PREFIX).strip("/")
    c.s3 = boto3.client("s3")
    c.warn = []
    user_given = resolve_user(getattr(args, "user", None))
    panel_given = safe_segment(c.panel_given)
    try:
        users = list_subfolders(c.s3, c.bucket, c.prefix + "/")
        c.user = match_folder(users, user_given, "user")
        panels = list_subfolders(c.s3, c.bucket, "%s/%s/" % (c.prefix, c.user)) if c.user in users else []
        c.panel = match_folder(panels, panel_given, "panel")
    except SystemExit:
        raise
    except Exception as e:
        if is_stop(e):
            raise
        c.user, c.panel = user_given, panel_given
        print("WARN: folder listing failed (%s); using given spelling" % type(e).__name__)
    c.base = "%s/%s/%s/" % (c.prefix, c.user, c.panel)
    print("RESOLVED user folder '%s', panel folder '%s'" % (c.user, c.panel))
    return c


def abs_path(c, p):
    return p if os.path.isabs(p) else os.path.join(c.root, p)


# ---------------------------------------------------------------- sweep
def cmd_sweep(args):
    c = build_ctx(args)
    state_file = abs_path(c, args.state_file or os.path.join(STATE_DIR, "state.json"))
    out_root = abs_path(c, c.output_dir)
    state = load_state(state_file)
    day = now_utc().strftime("%Y%m%d")
    folder = "%s_current_data" % day
    files = []
    if os.path.isdir(out_root):
        for dp, dn, fn in os.walk(out_root):
            dn[:] = [d for d in dn if d not in SWEEP_SKIP_DIRS and not os.path.islink(os.path.join(dp, d))]
            for f in fn:
                p = os.path.join(dp, f)
                if os.path.islink(p) or f.lower().endswith(".tmp"):
                    continue
                if f.rsplit(".", 1)[-1].lower() not in DATA_EXT or "." not in f:
                    continue
                files.append((os.path.relpath(p, out_root).replace(os.sep, "/"), p))
    up = unch = fail = 0
    done = []
    for rel, p in sorted(files):
        if not rel_ok(rel):
            print("failed: bad path %s" % rel); fail += 1; continue
        try:
            sha = sha_file(p)
            if state.get(rel) == sha:
                unch += 1; continue
            if not args.apply:
                print("would_upload: %s%s/%s" % ("", folder, rel)); up += 1; continue
            with open(p, "rb") as f:
                data = f.read()
            put_verified(c.s3, c.bucket, c.base + folder + "/" + rel, data, sha)
            state[rel] = sha
            save_state(state_file, state)
            done.append({"path": rel, "sha256": sha, "size_bytes": len(data)})
            up += 1
            print("uploaded: %s/%s" % (folder, rel))
        except Exception as e:
            if is_stop(e):
                raise
            fail += 1
            print("failed: %s (%s: %s)" % (rel, type(e).__name__, e))
    if args.apply and done:
        mkey = c.base + folder + "/" + MANIFEST
        try:
            m = get_json(c.s3, c.bucket, mkey) or {"run_id": folder, "created_utc": iso(),
                                                  "panel": c.panel, "user": c.user, "files": []}
            by = {f["path"]: f for f in m.get("files", [])}
            for f in done:
                by[f["path"]] = f
            m["files"] = sorted(by.values(), key=lambda x: x["path"])
            data = json.dumps(m, indent=1).encode()
            put_verified(c.s3, c.bucket, mkey, data, hashlib.sha256(data).hexdigest())
            print("manifest written: %s/%s" % (folder, MANIFEST))
        except Exception as e:
            if is_stop(e):
                raise
            fail += 1
            print("failed: manifest (%s: %s)" % (type(e).__name__, e))
    if up == 0 and fail == 0:
        print("nothing to upload")
    print("data_folder=%s" % folder)
    print("SUMMARY uploaded=%d unchanged=%d failed=%d" % (up, unch, fail))
    return 1 if fail else 0


# ---------------------------------------------------------------- code-sweep
def sensitive_name(name):
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext in SENSITIVE_EXT:
        return True
    if low == ".env" or low.startswith(".env."):
        return True
    if ext in SENSITIVE_DOC_EXT and any(w in low for w in SENSITIVE_WORDS):
        return True
    return False


def top_folder(rel):
    return rel.split("/")[0] + "/" if "/" in rel.rstrip("/") or rel.endswith("/") else "(root)"


def cmd_code_sweep(args):
    c = build_ctx(args)
    state_file = abs_path(c, args.state_file or os.path.join(STATE_DIR, "code_state.json"))
    root = c.root
    out_abs = os.path.abspath(abs_path(c, c.output_dir))
    patterns = c.cfg.get("code_patterns") or ["*"]
    skip_dirs = CODE_SKIP_DIRS | set(c.cfg.get("code_exclude_dirs") or [])
    state = load_state(state_file)
    if state.get(".fathom_layout") != "1":
        state = {}
    entries = []      # (rel, abs, kind) kind: file|dir
    state_files = []
    sk_sens, sk_large, sk_link = [], [], []
    for dp, dn, fn in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dn):
            p = os.path.join(dp, d)
            rel_d = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.islink(p):
                sk_link.append(rel_d); continue
            if d in skip_dirs or (dp == root and d in CODE_SKIP_ROOT_DIRS):
                continue
            keep.append(d)
        dn[:] = keep
        if dp != root and not os.listdir(dp):
            entries.append((os.path.relpath(dp, root).replace(os.sep, "/") + "/", dp, "dir"))
        for f in sorted(fn):
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.islink(p):
                sk_link.append(rel); continue
            if f.lower().endswith(".tmp") or (f.startswith(".tmp_") and f.endswith(".json")):
                continue
            if not any(fnmatch.fnmatch(f, pat) for pat in patterns):
                continue
            ap = os.path.abspath(p)
            if os.path.dirname(ap).startswith(out_abs) and (ap == out_abs or ap.startswith(out_abs + os.sep)) \
                    and f.rsplit(".", 1)[-1].lower() in DATA_EXT and "." in f:
                continue
            if sensitive_name(f):
                sk_sens.append(rel); continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > MAX_CODE_BYTES:
                sk_large.append(rel); continue
            if rel.startswith(STATE_DIR + "/"):
                state_files.append((rel, p, "file"))
            else:
                entries.append((rel, p, "file"))
    up = unch = fail = 0
    done = []
    by_folder = {}

    def do_upload(rel, p, kind, record_state):
        nonlocal up, fail
        key = c.base + "code/" + rel
        try:
            if kind == "dir":
                data, sha = b"", EMPTY_SHA
            else:
                with open(p, "rb") as f:
                    data = f.read()
                sha = hashlib.sha256(data).hexdigest()
            put_verified(c.s3, c.bucket, key, data, sha)
            ent = {"path": rel, "sha256": sha, "size_bytes": len(data)}
            if kind == "dir":
                ent["type"] = "dir"
            done.append(ent)
            by_folder[top_folder(rel)] = by_folder.get(top_folder(rel), 0) + 1
            up += 1
            return sha
        except Exception as e:
            if is_stop(e):
                raise
            fail += 1
            print("failed: %s (%s: %s)" % (rel, type(e).__name__, e))
            return None

    changed = []
    for rel, p, kind in sorted(entries):
        if not rel_ok(rel):
            print("failed: bad path %s" % rel); fail += 1; continue
        try:
            sha = EMPTY_SHA if kind == "dir" else sha_file(p)
        except OSError as e:
            fail += 1; print("failed: %s (%s)" % (rel, e)); continue
        if state.get(rel) == sha:
            unch += 1
        else:
            changed.append((rel, p, kind, sha))
    if not args.apply:
        for rel, p, kind, sha in changed:
            print("would_upload: code/%s" % rel)
        if changed or state.get(".fathom_state_synced") is not True:
            for rel, p, kind in sorted(state_files):
                print("would_upload: code/%s" % rel)
        print("SUMMARY would_upload=%d unchanged=%d" % (len(changed), unch))
        return 0
    for rel, p, kind, sha in changed:
        r = do_upload(rel, p, kind, True)
        if r:
            state[rel] = r
            save_state(state_file, state)
    uploaded_other = up
    if uploaded_other > 0 or state.get(".fathom_state_synced") is not True:
        state[".fathom_layout"] = "1"
        state[".fathom_state_synced"] = True
        save_state(state_file, state)
        # make sure the (new) state file is in the list
        known = {r for r, _, _ in state_files}
        for sf in (state_file,):
            rel = os.path.relpath(sf, root).replace(os.sep, "/")
            if rel.startswith(STATE_DIR + "/") and rel not in known and os.path.isfile(sf):
                state_files.append((rel, sf, "file"))
        bad = 0
        for rel, p, kind in sorted(state_files):
            if rel_ok(rel) and do_upload(rel, p, kind, False) is None:
                bad += 1
        if bad:
            state[".fathom_state_synced"] = False
            save_state(state_file, state)
    else:
        state[".fathom_layout"] = "1"
        state.setdefault(".fathom_state_synced", True)
    if uploaded_other == 0 and up == 0:
        pass
    if done:
        mkey = c.base + "code/" + MANIFEST
        try:
            m = get_json(c.s3, c.bucket, mkey) or {"run_id": "live", "created_utc": iso(),
                                                  "panel": c.panel, "user": c.user, "files": []}
            m["run_id"] = "live"
            m["updated_utc"] = iso()
            by = {f["path"]: f for f in m.get("files", [])}
            for f in done:
                by[f["path"]] = f
            m["files"] = sorted(by.values(), key=lambda x: x["path"])
            data = json.dumps(m, indent=1).encode()
            put_verified(c.s3, c.bucket, mkey, data, hashlib.sha256(data).hexdigest())
            print("manifest written: code/%s (%d entries)" % (MANIFEST, len(m["files"])))
        except Exception as e:
            if is_stop(e):
                raise
            fail += 1
            print("failed: manifest (%s: %s)" % (type(e).__name__, e))
    for r in sk_sens:
        print("skipped_sensitive_name: %s" % r)
    for r in sk_large:
        print("skipped_large: %s" % r)
    for r in sk_link:
        print("skipped_symlink: %s" % r)
    print("uploaded_by_folder: %s" % (", ".join("%s %d" % (k, v) for k, v in sorted(by_folder.items())) or "(none)"))
    print("knowledge_dir_present=%s scripts_dir_present=%s" % (
        os.path.isdir(os.path.join(root, "knowledge")), os.path.isdir(os.path.join(root, "scripts"))))
    print("SUMMARY uploaded=%d unchanged=%d failed=%d skipped_sensitive=%d skipped_large=%d" % (
        up, unch, fail, len(sk_sens), len(sk_large)))
    return 1 if fail else 0


# ---------------------------------------------------------------- selfcheck
def cmd_selfcheck(args):
    ok = True
    def res(name, good, extra=""):
        nonlocal ok
        ok = ok and good
        print("%s: %s %s" % ("PASS" if good else "FAIL", name, extra))
    try:
        c = build_ctx(args)
    except SystemExit as e:
        res("folder resolution", False, str(e)); return 1
    res("config", True, "panel_name=%r output_dir=%r" % (c.panel_given, c.output_dir))
    res("output_dir exists", os.path.isdir(abs_path(c, c.output_dir)), c.output_dir)
    try:
        c.s3.list_objects_v2(Bucket=c.bucket, Prefix=c.prefix + "/", MaxKeys=1)
        res("destination readable", True)
    except Exception as e:
        if is_stop(e):
            raise
        res("destination readable", False, type(e).__name__)
    res("folders resolved", True, "user '%s', panel '%s'" % (c.user, c.panel))
    if args.apply:
        # probe goes into today's <YYYYMMDD>_current_data/ folder (no separate timestamp run folder)
        run = "%s_current_data" % now_utc().strftime("%Y%m%d")
        data = ("selfcheck probe %s\n" % iso()).encode()
        try:
            put_verified(c.s3, c.bucket, c.base + run + "/selfcheck_probe.txt", data, hashlib.sha256(data).hexdigest())
            res("write probe + verify", True, run + "/selfcheck_probe.txt")
        except Exception as e:
            if is_stop(e):
                raise
            res("write probe + verify", False, "%s: %s" % (type(e).__name__, e))
    else:
        print("dry run: probe not written")
    return 0 if ok else 1


# ---------------------------------------------------------------- setup
def cmd_setup(args):
    check_panel_name(args.panel_name)
    cfg_path = os.path.abspath(args.config or "recovery_config.json")
    root = os.path.dirname(cfg_path)
    od = args.output_dir or "outputs"
    print("LAYOUT = %s" % LAYOUT)
    print("CODE_BACKUP = %s" % CODE_BACKUP)
    print("version %s" % __version__)
    if not os.path.exists(cfg_path):
        if args.apply:
            with open(cfg_path, "w") as f:
                json.dump({"panel_name": args.panel_name, "output_dir": od}, f, indent=2)
            print("config written: recovery_config.json")
        else:
            print("would write config")
    else:
        try:
            ex = json.load(open(cfg_path)).get("panel_name")
            if ex != args.panel_name:
                print("WARN: existing config panel_name %r differs from given %r; config kept" % (ex, args.panel_name))
        except Exception:
            pass
    odp = os.path.join(root, od)
    if not os.path.isdir(odp):
        if args.apply:
            os.makedirs(odp)
            print("created %s/" % od)
        else:
            print("would create %s/" % od)
    ns = argparse.Namespace(config=cfg_path if os.path.exists(cfg_path) else None, panel_name=args.panel_name,
                            output_dir=od, user=args.user, apply=args.apply, state_file=None)
    if not os.path.exists(cfg_path):
        ns.config = None
    rc = cmd_selfcheck(ns)
    if rc != 0:
        print("selfcheck failed; stopping")
        return rc
    if not args.apply:
        return 0
    os.makedirs(os.path.join(root, STATE_DIR), exist_ok=True)
    print("--- sweep ---")
    rc1 = cmd_sweep(ns)
    print("--- code-sweep ---")
    rc2 = cmd_code_sweep(ns)
    return rc1 or rc2


# ---------------------------------------------------------------- list
def fmt_size(n):
    return "%d bytes" % n


def cmd_list(args):
    c = build_ctx(args)
    if args.legacy:
        # <prefix>/<Panel>/<user>/<run>/<file>
        panels = list_subfolders(c.s3, c.bucket, c.prefix + "/")
        lp = match_folder(panels, safe_segment(c.panel_given), "legacy panel")
        users = list_subfolders(c.s3, c.bucket, "%s/%s/" % (c.prefix, lp))
        lu = match_folder(users, c.user, "legacy user")
        base = "%s/%s/%s/" % (c.prefix, lp, lu)
        runs = sorted(list_subfolders(c.s3, c.bucket, base), reverse=True)
        for r in runs:
            ks = list_keys(c.s3, c.bucket, base + r + "/")
            print("%s  files=%d size=%s" % (r, len(ks), fmt_size(sum(s for _, s in ks))))
        print("LEGACY_FOLDERS=%d" % len(runs))
        return 0
    folders = [f for f in list_subfolders(c.s3, c.bucket, c.base) if f != "code"]
    folders.sort(reverse=True)
    for f in folders:
        ks = list_keys(c.s3, c.bucket, c.base + f + "/")
        print("%s  files=%d size=%s" % (f, len(ks), fmt_size(sum(s for _, s in ks))))
    print("FOLDERS=%d" % len(folders))
    return 0


def live_keys(c):
    """live code/ keys -> rel, excluding manifest and old folders"""
    cp = c.base + "code/"
    out = []
    for k, s in list_keys(c.s3, c.bucket, cp):
        rel = k[len(cp):]
        if rel == MANIFEST or not rel or OLD_RE.match(rel.split("/")[0]):
            continue
        out.append((rel, k, s))
    return out


def cmd_list_code(args):
    c = build_ctx(args)
    cp = c.base + "code/"
    run = args.run
    if run == "live":
        lk = live_keys(c)
        for rel, k, s in lk:
            print("%s  %d" % (rel, s))
        m = get_json(c.s3, c.bucket, cp + MANIFEST)
        mc = len(m.get("files", [])) if m else 0
        print("S3_FILE_COUNT=%d" % len(lk))
        print("MANIFEST_FILE_COUNT=%d" % mc)
        return 0
    if run:
        ks = list_keys(c.s3, c.bucket, cp + run + "/")
        for k, s in ks:
            print("%s  %d" % (k[len(cp + run + "/"):], s))
        print("ENTRIES=%d size=%s" % (len(ks), fmt_size(sum(s for _, s in ks))))
        return 0
    lk = live_keys(c)
    print("live code/: entries=%d size=%s" % (len(lk), fmt_size(sum(s for _, _, s in lk))))
    for f in sorted(x for x in list_subfolders(c.s3, c.bucket, cp) if OLD_RE.match(x)):
        ks = list_keys(c.s3, c.bucket, cp + f + "/")
        print("old folder %s  files=%d size=%s" % (f, len(ks), fmt_size(sum(s for _, s in ks))))
    return 0


# ---------------------------------------------------------------- restore
def folder_date_key(name):
    d = re.sub(r"\D", "", name.split("-")[0] if "T" in name else name)[:14]
    return d.ljust(14, "0")


def cmd_restore_code(args):
    c = build_ctx(args)
    cp = c.base + "code/"
    sel = {}  # rel -> key
    if args.run and args.run != "live":
        pre = cp + args.run + "/"
        for k, s in list_keys(c.s3, c.bucket, pre):
            rel = k[len(pre):]
            if rel and rel != MANIFEST:
                sel[rel] = k
    else:
        for rel, k, s in live_keys(c):
            sel[rel] = k
        if not args.run:
            olds = [x for x in list_subfolders(c.s3, c.bucket, cp) if OLD_RE.match(x)]
            order = []
            for f in olds:
                m = get_json(c.s3, c.bucket, cp + f + "/" + MANIFEST)
                key = re.sub(r"\D", "", m.get("created_utc", ""))[:14].ljust(14, "0") if m and m.get("created_utc") \
                    else folder_date_key(f)
                order.append((key, f))
            for _, f in sorted(order, reverse=True):
                pre = cp + f + "/"
                for k, s in list_keys(c.s3, c.bucket, pre):
                    rel = k[len(pre):]
                    if rel and rel != MANIFEST and rel not in sel:
                        sel[rel] = k
    target = os.path.abspath(abs_path(c, args.target_dir or "restored_code"))
    files = dirs = refused = failed = 0
    for rel, k in sorted(sel.items()):
        if not rel_ok(rel):
            print("rejected key: %s" % rel); failed += 1; continue
        dest = os.path.abspath(os.path.join(target, *rel.rstrip("/").split("/")))
        if not (dest == target or dest.startswith(target + os.sep)):
            print("rejected (outside target): %s" % rel); failed += 1; continue
        if rel.endswith("/"):
            print("%s: empty folder %s" % ("restored" if args.apply else "would_restore", rel))
            if args.apply:
                os.makedirs(dest, exist_ok=True)
            dirs += 1
            continue
        if os.path.exists(dest) and not args.overwrite:
            print("refused (exists, use --overwrite): %s" % rel); refused += 1; continue
        if not args.apply:
            print("would_restore: %s" % rel); files += 1; continue
        try:
            o = c.s3.get_object(Bucket=c.bucket, Key=k)
            data = o["Body"].read()
            want = o.get("Metadata", {}).get("sha256")
            if want and hashlib.sha256(data).hexdigest() != want:
                raise RuntimeError("sha256 mismatch")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            files += 1
            print("restored: %s" % rel)
        except Exception as e:
            if is_stop(e):
                raise
            failed += 1
            print("failed: %s (%s: %s)" % (rel, type(e).__name__, e))
    print("SUMMARY %s files=%d empty_folders=%d refused=%d failed=%d target=%s" % (
        "restored" if args.apply else "would_restore", files, dirs, refused, failed,
        os.path.relpath(target, c.root)))
    return 1 if failed else 0


def cmd_restore(args):
    path = args.recovery_path.strip("/")
    pre = (os.environ.get("RECOVERY_DESTINATION_PREFIX") or DEFAULT_PREFIX).strip("/")
    if path.startswith(pre + "/"):
        path = path[len(pre) + 1:]
    if not rel_ok(path):
        raise SystemExit("invalid recovery path")
    src_bucket = os.environ.get("RECOVERY_DESTINATION_BUCKET") or DEFAULT_BUCKET
    s3 = boto3.client("s3")
    src_key = "%s/%s" % (pre, path)
    o = s3.get_object(Bucket=src_bucket, Key=src_key)
    data = o["Body"].read()
    sha = hashlib.sha256(data).hexdigest()
    want = o.get("Metadata", {}).get("sha256")
    if want and want != sha:
        raise SystemExit("source sha256 mismatch")
    try:
        s3.head_object(Bucket=args.target_bucket, Key=args.target_key)
        if not args.overwrite:
            print("refused: target exists (use --overwrite)")
            return 1
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
    put_verified(s3, args.target_bucket, args.target_key, data, sha)
    print("restored %d bytes, sha256 verified" % len(data))
    return 0

def cmd_verify_code(args):
    """READ-ONLY: re-download every live code/ object, hash the bytes, compare with the local file."""
    c = build_ctx(args)
    root = c.root
    out_abs = os.path.abspath(abs_path(c, c.output_dir))
    skip_dirs = CODE_SKIP_DIRS | set(c.cfg.get("code_exclude_dirs") or [])
    local = {}      # rel -> abs path (files) ; dirs tracked separately
    local_dirs = set()
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = [d for d in sorted(dn) if not os.path.islink(os.path.join(dp, d)) and d not in skip_dirs
                 and not (dp == root and d in CODE_SKIP_ROOT_DIRS)]
        if dp != root and not os.listdir(dp):
            local_dirs.add(os.path.relpath(dp, root).replace(os.sep, "/") + "/")
        for f in sorted(fn):
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.islink(p) or f.lower().endswith(".tmp") or (f.startswith(".tmp_") and f.endswith(".json")):
                continue
            if os.path.dirname(os.path.abspath(p)).startswith(out_abs) and f.rsplit(".", 1)[-1].lower() in DATA_EXT and "." in f:
                continue
            if sensitive_name(f) or os.path.getsize(p) > MAX_CODE_BYTES:
                continue
            local[rel] = p
    s3map = {rel: k for rel, k, s in live_keys(c)}
    ok = mism = missing_s3 = 0
    for rel in sorted(local):
        k = s3map.get(rel)
        if k is None:
            print("MISSING_IN_S3: %s" % rel); missing_s3 += 1; continue
        o = c.s3.get_object(Bucket=c.bucket, Key=k)
        body = o["Body"].read()
        s3sha = hashlib.sha256(body).hexdigest()
        meta = o.get("Metadata", {}).get("sha256")
        lsha = sha_file(local[rel])
        if s3sha == lsha and meta == lsha:
            print("OK: %s  %d bytes  sha256=%s" % (rel, len(body), lsha[:12])); ok += 1
        else:
            print("MISMATCH: %s  local=%s s3=%s meta=%s" % (rel, lsha[:12], s3sha[:12], (meta or "none")[:12])); mism += 1
    for d in sorted(local_dirs):
        print("%s: %s (empty-folder marker)" % ("OK" if d in s3map else "MISSING_IN_S3", d))
        if d in s3map:
            ok += 1
        else:
            missing_s3 += 1
    extra = sorted(r for r in s3map if r not in local and r not in local_dirs)
    for r in extra:
        print("S3_ONLY: %s (not in local workspace; kept in S3)" % r)
    print("SUMMARY verified_ok=%d mismatch=%d missing_in_s3=%d s3_only=%d" % (ok, mism, missing_s3, len(extra)))
    return 1 if (mism or missing_s3) else 0




# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Fathom recovery backup v" + __version__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    def common(p, state=False, panel_name=False, user=False):
        p.add_argument("--config")
        if panel_name:
            p.add_argument("--panel-name")
        if user:
            p.add_argument("--user")
        if state:
            p.add_argument("--state-file")

    p = sp.add_parser("setup")
    p.add_argument("--panel-name", required=True); p.add_argument("--output-dir", default="outputs")
    p.add_argument("--config"); p.add_argument("--user"); p.add_argument("--apply", action="store_true")
    p = sp.add_parser("sweep"); common(p, state=True, user=True); p.add_argument("--apply", action="store_true")
    p = sp.add_parser("code-sweep"); common(p, state=True, user=True); p.add_argument("--apply", action="store_true")
    p = sp.add_parser("selfcheck"); common(p, panel_name=True, user=True); p.add_argument("--apply", action="store_true")
    p = sp.add_parser("list"); common(p, panel_name=True, user=True); p.add_argument("--legacy", action="store_true")
    p = sp.add_parser("list-code"); common(p, panel_name=True, user=True); p.add_argument("--run")
    p = sp.add_parser("restore-code"); common(p, user=True)
    p.add_argument("--run"); p.add_argument("--target-dir"); p.add_argument("--overwrite", action="store_true")
    p.add_argument("--apply", action="store_true")
    p = sp.add_parser("verify-code"); common(p, user=True)

    p = sp.add_parser("restore")
    p.add_argument("--recovery-path", required=True); p.add_argument("--target-bucket", required=True)
    p.add_argument("--target-key", required=True); p.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    fn = {"setup": cmd_setup, "sweep": cmd_sweep, "code-sweep": cmd_code_sweep, "selfcheck": cmd_selfcheck,
          "list": cmd_list, "list-code": cmd_list_code, "restore-code": cmd_restore_code,
          "verify-code": cmd_verify_code, "restore": cmd_restore}[args.cmd]
    try:
        sys.exit(fn(args) or 0)
    except SystemExit:
        raise
    except Exception as e:
        if is_stop(e):
            print("STOP: %s: %s" % (type(e).__name__, e))
            sys.exit(2)
        print("ERROR: %s: %s" % (type(e).__name__, e))
        sys.exit(1)


if __name__ == "__main__":
    main()
