"""Offline tests for fathom_recovery v1.9.x (fake S3, no network, nothing real is written).

Run:  python3 -B -m unittest discover -s tests -v
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ["RECOVERY_USER"] = "tester"
os.environ["RECOVERY_DESTINATION_BUCKET"] = "rec"
os.environ["RECOVERY_DESTINATION_PREFIX"] = "6_Recovery"
sys.dont_write_bytecode = True
import fathom_recovery as fr  # noqa: E402


def _no_real_s3(*a, **kw):
    raise RuntimeError("test tried to create a real S3 client; patch fr.boto3.client with FakeS3")


fr.boto3.client = _no_real_s3      # safety net: Base.setUp replaces it with a FakeS3 for the tests that need S3

BASE = "6_Recovery/tester/Test Panel/"


class FakeS3:
    """Minimal S3 stand-in. It has NO delete_object: any delete call would raise AttributeError."""

    def __init__(self):
        self.objs = {}      # (bucket, key) -> {"data": bytes, "meta": dict}
        self.puts = []

    def seed(self, b, k, data, meta=None):
        self.objs[(b, k)] = {"data": data, "meta": dict(meta or {})}

    def _get(self, b, k, op):
        if (b, k) not in self.objs:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, op)
        return self.objs[(b, k)]

    def put_object(self, Bucket, Key, Body, Metadata=None, **kw):
        self.puts.append(Key)
        self.seed(Bucket, Key, Body, Metadata)

    def head_object(self, Bucket, Key):
        o = self._get(Bucket, Key, "HeadObject")
        return {"ContentLength": len(o["data"]), "Metadata": o["meta"]}

    def get_object(self, Bucket, Key):
        o = self._get(Bucket, Key, "GetObject")
        return {"Body": io.BytesIO(o["data"]), "Metadata": o["meta"]}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000):
        keys = [k for (b, k) in self.objs if b == Bucket and k.startswith(Prefix)][:MaxKeys]
        return {"Contents": [{"Key": k} for k in keys], "KeyCount": len(keys)}

    def get_paginator(self, name):
        fs = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = [(k, o) for (b, k), o in fs.objs.items() if b == Bucket and k.startswith(Prefix)]
                if Delimiter:
                    cps = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                                  for k, _ in keys if Delimiter in k[len(Prefix):]})
                    direct = [{"Key": k, "Size": len(o["data"])} for k, o in keys if Delimiter not in k[len(Prefix):]]
                    yield {"Contents": direct, "CommonPrefixes": [{"Prefix": c} for c in cps]}
                else:
                    yield {"Contents": [{"Key": k, "Size": len(o["data"])} for k, o in keys]}
        return P()

    def keys(self, prefix=""):
        return sorted(k for (b, k) in self.objs if k.startswith(prefix))


class Base(unittest.TestCase):
    PANEL = "Test Panel"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "recovery_config.json").write_text(
            json.dumps({"panel_name": self.PANEL, "output_dir": "outputs"}))
        self.s3 = FakeS3()
        p = mock.patch.object(fr.boto3, "client", return_value=self.s3)
        p.start()
        self.addCleanup(p.stop)
        self.day = "20260101"
        d = mock.patch.object(fr, "now_utc", lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        d.start()
        self.addCleanup(d.stop)

    # helpers
    def w(self, rel, data="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def ns(self, **kw):
        base = dict(config=str(self.root / "recovery_config.json"), panel_name=None, user=None, apply=True,
                    state_file=None, legacy=False, run=None, target_dir=None, overwrite=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def call(self, fn, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(self.ns(**kw))
        return rc, buf.getvalue()

    @staticmethod
    def summary(out):
        m = re.search(r"SUMMARY (.*)", out)
        return dict(kv.split("=") for kv in m.group(1).split()) if m else {}

    def sweep(self, **kw):
        return self.call(fr.cmd_sweep, **kw)

    def code(self, **kw):
        return self.call(fr.cmd_code_sweep, **kw)

    def folder(self):
        return BASE + self.day + "_current_data/"


class Markers(unittest.TestCase):
    def test_header_markers_and_version(self):
        self.assertEqual(fr.LAYOUT, "user/panel")
        self.assertEqual(fr.CODE_BACKUP, "1")
        self.assertEqual(fr.CODE_FLAT, "1")
        self.assertEqual(fr.CODE_SCOPE, "all")
        self.assertEqual(fr.PROBE_DIR, "data-day")
        self.assertEqual(fr.__version__, "1.9.1")

    def test_grep_style_marker_lines_exist_once(self):
        text = Path(ROOT, "fathom_recovery.py").read_text()
        for line in ('LAYOUT = "user/panel"', 'CODE_BACKUP = "1"', 'CODE_FLAT = "1"',
                     'CODE_SCOPE = "all"', 'PROBE_DIR = "data-day"'):
            self.assertEqual(text.count(line), 1, line)

    def test_no_delete_or_lifecycle_calls_in_script(self):
        text = Path(ROOT, "fathom_recovery.py").read_text()
        for bad in ("delete_object", "delete_objects", "lifecycle", "delete_bucket", "os.remove", "shutil.rmtree"):
            self.assertNotIn(bad, text, bad)

    def test_cli_commands(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "fathom_recovery.py"), "--help"],
                           capture_output=True, text=True)
        for cmd in ("setup", "sweep", "code-sweep", "selfcheck", "list", "list-code", "restore-code",
                    "verify-code", "restore"):
            self.assertIn(cmd, r.stdout)
        self.assertNotIn("init,", r.stdout)


class Names(unittest.TestCase):
    def test_bad_panel_names(self):
        for bad in ("", "  ", ".", "..", "a<b", "a>b", "<Panel Name>"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fr.check_panel_name(bad)
        self.assertEqual(fr.check_panel_name("Daily Checklist"), "Daily Checklist")

    def test_safe_segment_and_rel_ok(self):
        self.assertEqual(fr.safe_segment("a/b\\c"), "a b c")
        with self.assertRaises(ValueError):
            fr.safe_segment("..")
        self.assertTrue(fr.rel_ok("a/b/c.py"))
        self.assertTrue(fr.rel_ok("outputs/"))
        for bad in ("a/../b", "./a", "a//b", ""):
            self.assertFalse(fr.rel_ok(bad), bad)

    def test_user_resolution_env(self):
        self.assertEqual(fr.resolve_user(None), "tester")
        self.assertEqual(fr.resolve_user("Other"), "Other")


class FolderMatching(Base):
    def test_exact_wins_and_case_insensitive_match(self):
        self.s3.seed("rec", "6_Recovery/TESTER/test  panel/x.txt", b"1")
        self.sweep()          # resolves user 'tester' -> existing 'TESTER'; panel -> existing 'test  panel'
        rc, out = self.sweep()
        self.assertIn("RESOLVED user folder 'TESTER', panel folder 'test  panel'", out)

    def test_ambiguous_folder_needs_exact_spelling(self):
        self.s3.seed("rec", "6_Recovery/NBHAN/a", b"1")
        self.s3.seed("rec", "6_Recovery/NbHan/a", b"1")
        with self.assertRaises(SystemExit) as cm:
            fr.build_ctx(self.ns(user="nbhan"))
        self.assertIn("exact spelling", str(cm.exception))
        # exact spelling wins even when several differ only by case
        c = fr.build_ctx(self.ns(user="NBHAN"))
        self.assertEqual(c.user, "NBHAN")

    def test_new_names_use_given_spelling(self):
        c = fr.build_ctx(self.ns())
        self.assertEqual((c.user, c.panel), ("tester", "Test Panel"))
        self.assertEqual(c.base, BASE)


class Sweep(Base):
    def test_dry_run_uploads_nothing_and_writes_no_state(self):
        self.w("outputs/a.csv", "1,2")
        rc, out = self.sweep(apply=False)
        self.assertEqual(rc, 0)
        self.assertIn("would_upload: " + self.day + "_current_data/a.csv", out)
        self.assertEqual(self.s3.puts, [])
        self.assertFalse((self.root / ".fathom_recovery").exists())

    def test_upload_goes_to_day_folder_with_sha_manifest_and_subfolders(self):
        self.w("outputs/a.csv", "1,2")
        self.w("outputs/reports/q1.json", "{}")
        self.w("outputs/SCAN.PDF", b"%PDF-1.4")
        self.w("outputs/notes.py", "print(1)")          # not a data extension
        self.w("outputs/noext", "x")
        rc, out = self.sweep()
        self.assertEqual(rc, 0)
        self.assertEqual(self.summary(out), {"uploaded": "3", "unchanged": "0", "failed": "0"})
        keys = self.s3.keys(self.folder())
        self.assertEqual([k[len(self.folder()):] for k in keys],
                         [".fathom_manifest.json", "SCAN.PDF", "a.csv", "reports/q1.json"])
        o = self.s3.objs[("rec", self.folder() + "a.csv")]
        self.assertEqual(o["meta"]["sha256"], hashlib.sha256(b"1,2").hexdigest())
        m = json.loads(self.s3.objs[("rec", self.folder() + ".fathom_manifest.json")]["data"])
        self.assertEqual(m["run_id"], self.day + "_current_data")
        self.assertEqual(sorted(f["path"] for f in m["files"]), ["SCAN.PDF", "a.csv", "reports/q1.json"])
        self.assertIn("data_folder=" + self.day + "_current_data", out)
        # no timestamp run folder anywhere
        self.assertFalse([k for k in self.s3.keys() if re.search(r"/\d{8}T\d{6}Z-[0-9a-f]{8}/", k)])

    def test_second_sweep_unchanged_then_changed_file_overwrites_same_day_folder(self):
        self.w("outputs/a.csv", "1")
        self.w("outputs/b.csv", "2")
        self.sweep()
        rc, out = self.sweep()
        self.assertEqual(self.summary(out), {"uploaded": "0", "unchanged": "2", "failed": "0"})
        self.assertIn("nothing to upload", out)
        self.w("outputs/b.csv", "22")
        rc, out = self.sweep()
        self.assertEqual(self.summary(out), {"uploaded": "1", "unchanged": "1", "failed": "0"})
        self.assertEqual(self.s3.objs[("rec", self.folder() + "b.csv")]["data"], b"22")
        days = {k.split("/")[3] for k in self.s3.keys(BASE)}
        self.assertEqual(days, {self.day + "_current_data"})
        m = json.loads(self.s3.objs[("rec", self.folder() + ".fathom_manifest.json")]["data"])
        self.assertEqual(sorted(f["path"] for f in m["files"]), ["a.csv", "b.csv"])   # merged, none dropped

    def test_nothing_new_creates_no_folder(self):
        (self.root / "outputs").mkdir()
        rc, out = self.sweep()
        self.assertEqual(self.summary(out)["uploaded"], "0")
        self.assertEqual(self.s3.keys(), [])

    def test_new_utc_day_creates_a_new_folder_and_leaves_yesterday_alone(self):
        self.w("outputs/a.csv", "1")
        self.sweep()
        self.w("outputs/new.csv", "n")
        with mock.patch.object(fr, "now_utc", lambda: datetime(2026, 1, 2, 0, 5, tzinfo=timezone.utc)):
            rc, out = self.sweep()
        self.assertEqual(self.summary(out)["uploaded"], "1")
        self.assertIn(BASE + "20260102_current_data/new.csv", self.s3.keys())
        self.assertNotIn(BASE + "20260102_current_data/a.csv", self.s3.keys())    # unchanged, not re-copied
        self.assertNotIn(BASE + "20260101_current_data/new.csv", self.s3.keys())  # yesterday untouched

    def test_failed_upload_is_not_recorded_and_retried(self):
        self.w("outputs/a.csv", "1")
        real = self.s3.put_object
        self.s3.put_object = mock.Mock(side_effect=RuntimeError("boom"))
        rc, out = self.sweep()
        self.assertEqual(rc, 1)
        self.assertEqual(self.summary(out)["failed"], "1")
        self.s3.put_object = real
        rc, out = self.sweep()
        self.assertEqual(self.summary(out), {"uploaded": "1", "unchanged": "0", "failed": "0"})

    def test_access_denied_stops_with_exit_2(self):
        self.w("outputs/a.csv", "1")
        err = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self.s3.put_object = mock.Mock(side_effect=err)
        with self.assertRaises(ClientError):
            self.sweep()          # cmd_sweep re-raises; main() turns it into 'STOP:' + exit 2
        self.assertTrue(fr.is_stop(err))
        self.assertTrue(fr.is_stop(fr.NoCredentialsError()))


class CodeSweep(Base):
    def populate(self):
        self.w("scripts/a.py", "print(1)")
        self.w("knowledge/_index.md", "k")
        self.w("knowledge/technical/apis.md", "api")
        self.w(".workspace_meta.json", "{}")
        self.w(".fathom_runtime/workspace_state.json", "{}")
        self.w(".fathom_runtime/workspace.lock", b"")            # empty file
        self.w("tokenizer.py", "# code files are never judged by name")
        (self.root / "outputs").mkdir()                          # empty folder -> marker
        (self.root / "empty_dir").mkdir()

    def test_whole_workspace_goes_to_code_with_paths_hidden_empty_files_and_dir_markers(self):
        self.populate()
        self.w("outputs/data.csv", "1")                           # data in outputs -> sweep owns it
        self.w("outputs/report.py", "x")                          # other file in outputs -> backed up
        rc, out = self.code()
        self.assertEqual(rc, 0, out)
        keys = {k[len(BASE + "code/"):] for k in self.s3.keys(BASE + "code/")}
        for expect in ("scripts/a.py", "knowledge/_index.md", "knowledge/technical/apis.md", ".workspace_meta.json",
                       ".fathom_runtime/workspace_state.json", ".fathom_runtime/workspace.lock", "tokenizer.py",
                       "recovery_config.json", "empty_dir/", "outputs/report.py", ".fathom_manifest.json",
                       ".fathom_recovery/code_state.json"):
            self.assertIn(expect, keys, expect)
        self.assertNotIn("outputs/data.csv", keys)
        self.assertEqual(self.s3.objs[("rec", BASE + "code/.fathom_runtime/workspace.lock")]["data"], b"")
        self.assertEqual(self.s3.objs[("rec", BASE + "code/empty_dir/")]["data"], b"")
        m = json.loads(self.s3.objs[("rec", BASE + "code/.fathom_manifest.json")]["data"])
        self.assertEqual(m["run_id"], "live")
        dirs = [f for f in m["files"] if f.get("type") == "dir"]
        self.assertTrue(all(f["path"].endswith("/") for f in dirs))
        self.assertIn("empty_dir/", [f["path"] for f in dirs])
        self.assertIn("knowledge_dir_present=True scripts_dir_present=True", out)
        self.assertIn("uploaded_by_folder:", out)
        # code lives under ONE live copy, never a date folder
        self.assertFalse([k for k in keys if re.match(r"\d{4}-\d{2}-\d{2}/|\d{8}T\d{6}Z-", k)])

    def test_sensitive_names_skipped_and_reported_never_uploaded(self):
        self.populate()
        for name in ("my_secret.json", "prod_credentials.yaml", "password.txt", "api_key.toml", ".env", ".env.local",
                     "id.pem", "cert.key", "bundle.pfx", "a.p12", "s.jks", "k.keystore", "service_account.json"):
            self.w(name, "s")
        rc, out = self.code()
        for name in ("my_secret.json", ".env", ".env.local", "cert.key", "service_account.json", "password.txt"):
            self.assertIn("skipped_sensitive_name: " + name, out)
            self.assertNotIn(BASE + "code/" + name, self.s3.keys())
        self.assertIn(BASE + "code/tokenizer.py", self.s3.keys())
        self.assertEqual(self.summary(out)["skipped_sensitive"], "13")

    def test_large_files_and_symlinks_are_skipped_and_reported(self):
        self.populate()
        self.w("big.bin", b"x" * 64)
        link = self.root / "lnk.py"
        try:
            os.symlink(self.root / "tokenizer.py", link)
            have_link = True
        except (OSError, NotImplementedError):      # Windows without the symlink privilege: skip only that part
            have_link = False
        with mock.patch.object(fr, "MAX_CODE_BYTES", 10):
            rc, out = self.code()
        self.assertIn("skipped_large: big.bin", out)
        self.assertNotIn(BASE + "code/big.bin", self.s3.keys())
        if have_link:
            self.assertIn("skipped_symlink: lnk.py", out)
            self.assertNotIn(BASE + "code/lnk.py", self.s3.keys())


    def test_idle_second_sweep_uploads_nothing_and_state_files_do_not_trigger(self):
        self.populate()
        self.code()
        puts = len(self.s3.puts)
        rc, out = self.code()
        self.assertEqual(self.summary(out)["uploaded"], "0", out)
        self.assertEqual(len(self.s3.puts), puts)

    def test_changed_file_overwrites_live_copy_and_state_files_follow(self):
        self.populate()
        self.code()
        self.w("scripts/a.py", "print(2)")
        rc, out = self.code()
        self.assertEqual(self.s3.objs[("rec", BASE + "code/scripts/a.py")]["data"], b"print(2)")
        self.assertGreaterEqual(int(self.summary(out)["uploaded"]), 2)       # file + the state file(s)
        state = json.loads((self.root / ".fathom_recovery/code_state.json").read_text())
        self.assertEqual(state[".fathom_layout"], "1")
        self.assertIs(state[".fathom_state_synced"], True)
        self.assertEqual(state["scripts/a.py"], hashlib.sha256(b"print(2)").hexdigest())
        s3_state = json.loads(self.s3.objs[("rec", BASE + "code/.fathom_recovery/code_state.json")]["data"])
        self.assertEqual(s3_state["scripts/a.py"], state["scripts/a.py"])      # final state uploaded AFTER it was written

    def test_old_state_without_layout_marker_uploads_everything_once(self):
        self.populate()
        (self.root / ".fathom_recovery").mkdir()
        (self.root / ".fathom_recovery/code_state.json").write_text(
            json.dumps({"scripts/a.py": hashlib.sha256(b"print(1)").hexdigest()}))
        rc, out = self.code()
        self.assertIn(BASE + "code/scripts/a.py", self.s3.keys())

    def test_dry_run_writes_nothing(self):
        self.populate()
        rc, out = self.code(apply=False)
        self.assertEqual(self.s3.puts, [])
        self.assertIn("would_upload: code/scripts/a.py", out)
        self.assertFalse((self.root / ".fathom_recovery").exists())

    def test_restored_code_folder_and_pycache_are_not_swept(self):
        self.populate()
        self.w("restored_code/old.py", "x")
        self.w("pkg/__pycache__/x.pyc", "x")
        self.w("node_modules/m.js", "x")
        self.code()
        keys = self.s3.keys(BASE + "code/")
        self.assertFalse([k for k in keys if "restored_code" in k or "__pycache__" in k or "node_modules" in k])

    def test_existing_old_day_folders_inside_code_are_left_alone_and_not_counted(self):
        self.populate()
        self.s3.seed("rec", BASE + "code/2025-12-31/old.py", b"o", {"sha256": hashlib.sha256(b"o").hexdigest()})
        self.s3.seed("rec", BASE + "code/20251231T120000Z-abcdef12/older.py", b"o")
        self.code()
        rc, out = self.call(fr.cmd_list_code, run="live")
        live = [ln for ln in out.splitlines() if "old" not in ln.split("  ")[0]]
        self.assertNotIn("2025-12-31", out)
        c = self.summary_counts(out)
        self.assertEqual(c["S3_FILE_COUNT"], c["MANIFEST_FILE_COUNT"])
        self.assertEqual(self.s3.objs[("rec", BASE + "code/2025-12-31/old.py")]["data"], b"o")

    @staticmethod
    def summary_counts(out):
        return {k: int(v) for k, v in re.findall(r"(S3_FILE_COUNT|MANIFEST_FILE_COUNT)=(\d+)", out)}


class SelfCheckAndList(Base):
    def test_probe_goes_into_todays_current_data_folder_not_a_timestamp_folder(self):
        (self.root / "outputs").mkdir()
        rc, out = self.call(fr.cmd_selfcheck)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS: write probe + verify " + self.day + "_current_data/selfcheck_probe.txt", out)
        self.assertEqual(self.s3.keys(), [self.folder() + "selfcheck_probe.txt"])
        self.assertFalse(re.search(r"\d{8}T\d{6}Z-[0-9a-f]{8}", " ".join(self.s3.keys())))

    def test_selfcheck_dry_run_writes_nothing_and_rerun_overwrites_one_probe(self):
        (self.root / "outputs").mkdir()
        rc, out = self.call(fr.cmd_selfcheck, apply=False)
        self.assertIn("dry run: probe not written", out)
        self.assertEqual(self.s3.keys(), [])
        self.call(fr.cmd_selfcheck)
        self.call(fr.cmd_selfcheck)
        self.assertEqual(len(self.s3.keys()), 1)

    def test_probe_is_not_in_the_sweep_manifest(self):
        self.w("outputs/a.csv", "1")
        self.call(fr.cmd_selfcheck)
        self.sweep()
        m = json.loads(self.s3.objs[("rec", self.folder() + ".fathom_manifest.json")]["data"])
        self.assertEqual([f["path"] for f in m["files"]], ["a.csv"])
        self.assertIn(self.folder() + "selfcheck_probe.txt", self.s3.keys())

    def test_selfcheck_fails_when_output_dir_missing(self):
        rc, out = self.call(fr.cmd_selfcheck, apply=False)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: output_dir exists", out)

    def test_list_shows_day_folders_newest_first_and_never_code(self):
        self.w("outputs/a.csv", "1")
        self.sweep()
        self.s3.seed("rec", BASE + "20251231_current_data/x.csv", b"x")
        self.s3.seed("rec", BASE + "code/a.py", b"x")
        self.s3.seed("rec", BASE + "20251230T010101Z-abcdef12/old.txt", b"x")
        rc, out = self.call(fr.cmd_list)
        names = [ln.split()[0] for ln in out.splitlines() if "files=" in ln]
        self.assertEqual(names, [self.day + "_current_data", "20251231_current_data", "20251230T010101Z-abcdef12"])
        self.assertNotIn("code", names)
        self.assertIn("FOLDERS=3", out)

    def test_list_code_counts_match_manifest(self):
        self.w("scripts/a.py", "1")
        (self.root / "outputs").mkdir()
        self.code()
        rc, out = self.call(fr.cmd_list_code, run="live")
        c = CodeSweep.summary_counts(out)
        self.assertGreater(c["S3_FILE_COUNT"], 0)
        self.assertEqual(c["S3_FILE_COUNT"], c["MANIFEST_FILE_COUNT"])
        rc, out = self.call(fr.cmd_list_code)
        self.assertIn("live code/: entries=", out)


class RestoreAndVerify(Base):
    def seed_workspace(self):
        self.w("scripts/a.py", "print(1)")
        self.w("knowledge/k.md", "k")
        self.w(".hidden", "h")
        self.w("empty.txt", b"")
        (self.root / "emptydir").mkdir()
        (self.root / "outputs").mkdir()
        self.code()

    def restore(self, **kw):
        return self.call(fr.cmd_restore_code, **kw)

    def test_restore_code_roundtrip_dry_run_then_apply(self):
        self.seed_workspace()
        rc, out = self.restore(apply=False)
        self.assertIn("would_restore: scripts/a.py", out)
        self.assertFalse((self.root / "restored_code").exists())
        rc, out = self.restore()
        self.assertEqual(rc, 0, out)
        t = self.root / "restored_code"
        self.assertEqual((t / "scripts/a.py").read_text(), "print(1)")
        self.assertEqual((t / ".hidden").read_text(), "h")
        self.assertEqual((t / "empty.txt").read_bytes(), b"")
        self.assertTrue((t / "emptydir").is_dir())
        self.assertTrue((t / "outputs").is_dir())
        self.assertEqual(sorted(p.name for p in (t / "emptydir").iterdir()), [])

    def test_restore_refuses_existing_file_without_overwrite_and_never_touches_live_files(self):
        self.seed_workspace()
        self.restore()
        (self.root / "restored_code/scripts/a.py").write_text("local edit")
        rc, out = self.restore()
        self.assertIn("refused (exists, use --overwrite): scripts/a.py", out)
        self.assertEqual((self.root / "restored_code/scripts/a.py").read_text(), "local edit")
        rc, out = self.restore(overwrite=True)
        self.assertEqual((self.root / "restored_code/scripts/a.py").read_text(), "print(1)")
        self.assertEqual((self.root / "scripts/a.py").read_text(), "print(1)")

    def test_restore_detects_corrupted_object(self):
        self.seed_workspace()
        self.s3.objs[("rec", BASE + "code/scripts/a.py")]["data"] = b"tampered"
        rc, out = self.restore()
        self.assertEqual(rc, 1)
        self.assertIn("failed: scripts/a.py", out)
        self.assertFalse((self.root / "restored_code/scripts/a.py").exists())

    def test_path_traversal_keys_cannot_escape_target(self):
        self.seed_workspace()
        self.s3.seed("rec", BASE + "code/a/../../evil.py", b"e", {"sha256": hashlib.sha256(b"e").hexdigest()})
        rc, out = self.restore()
        self.assertIn("rejected key: a/../../evil.py", out)
        self.assertFalse((self.root.parent / "evil.py").exists())
        self.assertFalse((self.root / "evil.py").exists())

    def test_restore_adds_files_only_present_in_old_folders_and_run_flag_selects(self):
        self.seed_workspace()
        sha = hashlib.sha256(b"old").hexdigest()
        self.s3.seed("rec", BASE + "code/2025-12-31/only_old.py", b"old", {"sha256": sha})
        self.s3.seed("rec", BASE + "code/2025-12-31/scripts/a.py", b"OLD A", {"sha256": hashlib.sha256(b"OLD A").hexdigest()})
        rc, out = self.restore()
        self.assertEqual((self.root / "restored_code/only_old.py").read_text(), "old")
        self.assertEqual((self.root / "restored_code/scripts/a.py").read_text(), "print(1)")      # live wins
        rc, out = self.restore(run="2025-12-31", target_dir="from_old")
        self.assertEqual((self.root / "from_old/scripts/a.py").read_text(), "OLD A")
        self.assertFalse((self.root / "from_old/knowledge").exists())
        rc, out = self.restore(run="live", target_dir="live_only")
        self.assertFalse((self.root / "live_only/only_old.py").exists())

    def test_verify_code_ok_mismatch_missing_and_s3_only(self):
        self.seed_workspace()
        rc, out = self.call(fr.cmd_verify_code)
        self.assertEqual(rc, 0, out)
        self.assertIn("mismatch=0 missing_in_s3=0", out)
        self.w("scripts/a.py", "changed locally")
        self.w("new_file.py", "n")
        self.s3.seed("rec", BASE + "code/gone_locally.py", b"g")
        rc, out = self.call(fr.cmd_verify_code)
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH: scripts/a.py", out)
        self.assertIn("MISSING_IN_S3: new_file.py", out)
        self.assertIn("S3_ONLY: gone_locally.py", out)
        self.assertEqual(self.s3.objs[("rec", BASE + "code/gone_locally.py")]["data"], b"g")    # nothing deleted

    def test_restore_single_object_refuses_existing_target_and_verifies_hash(self):
        self.seed_workspace()
        path = "tester/Test Panel/code/scripts/a.py"
        ns = argparse.Namespace(recovery_path=path, target_bucket="live", target_key="k/a.py", overwrite=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fr.cmd_restore(ns), 0)
        self.assertEqual(self.s3.objs[("live", "k/a.py")]["data"], b"print(1)")
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fr.cmd_restore(ns), 1)
        ns.overwrite = True
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fr.cmd_restore(ns), 0)


class SetupCommand(Base):
    def test_setup_apply_runs_selfcheck_sweep_and_code_sweep_in_one_day_folder(self):
        (self.root / "recovery_config.json").unlink()
        self.w("outputs/a.csv", "1")
        self.w("scripts/a.py", "p")
        ns = argparse.Namespace(panel_name=self.PANEL, output_dir="outputs", config=str(self.root / "recovery_config.json"),
                                user=None, apply=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fr.cmd_setup(ns)
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("LAYOUT = user/panel", out)
        self.assertIn("config written: recovery_config.json", out)
        cfg = json.loads((self.root / "recovery_config.json").read_text())
        self.assertEqual(cfg, {"panel_name": self.PANEL, "output_dir": "outputs"})
        day_keys = self.s3.keys(self.folder())
        self.assertIn(self.folder() + "selfcheck_probe.txt", day_keys)
        self.assertIn(self.folder() + "a.csv", day_keys)
        self.assertIn(BASE + "code/scripts/a.py", self.s3.keys())
        self.assertEqual({k.split("/")[3] for k in self.s3.keys(BASE)}, {self.day + "_current_data", "code"})

    def test_setup_dry_run_writes_nothing_to_s3(self):
        (self.root / "outputs").mkdir()
        ns = argparse.Namespace(panel_name=self.PANEL, output_dir="outputs", config=str(self.root / "recovery_config.json"),
                                user=None, apply=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fr.cmd_setup(ns), 0)
        self.assertEqual(self.s3.keys(), [])

    def test_setup_keeps_existing_config_and_rejects_placeholder_names(self):
        (self.root / "outputs").mkdir()
        ns = argparse.Namespace(panel_name="Other Name", output_dir="outputs", config=str(self.root / "recovery_config.json"),
                                user=None, apply=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fr.cmd_setup(ns)
        self.assertIn("config kept", buf.getvalue())
        self.assertEqual(json.loads((self.root / "recovery_config.json").read_text())["panel_name"], self.PANEL)
        ns.panel_name = "<Panel Name>"
        with self.assertRaises(ValueError):
            fr.cmd_setup(ns)


def load(name, copy_to=None):
    """Import a repo module. copy_to: import a COPY placed in that folder, so the module's own location
    (its _HERE) is the temp folder and it can never discover the real workspace next to the repo."""
    import importlib.util
    import shutil
    path = os.path.join(ROOT, name + ".py")
    if copy_to:
        path = shutil.copyfile(path, os.path.join(copy_to, name + ".py"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SetupPanelWrapper(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self._td.name)
        self.addCleanup(os.chdir, self.cwd)
        self.sp = load("setup_panel")
        self.sp.results.clear()

    def fake_run(self, rc=0, out="SUMMARY uploaded=1 unchanged=0 failed=0"):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return calls, run

    def test_installs_script_and_calls_the_installed_setup_command(self):
        calls, run = self.fake_run()
        with mock.patch.object(self.sp.subprocess, "run", run), contextlib.redirect_stdout(io.StringIO()):
            rc = self.sp.main(["--panel-name", "My Panel", "--output-dir", "outputs", "--apply"])
        self.assertEqual(rc, 0)
        installed = Path("scripts/fathom_recovery.py")
        self.assertEqual(installed.read_bytes(), Path(ROOT, "fathom_recovery.py").read_bytes())
        cmd = calls[0]
        self.assertEqual(cmd[3], "setup")
        self.assertIn("--apply", cmd)
        self.assertEqual(cmd[cmd.index("--panel-name") + 1], "My Panel")
        self.assertNotIn("init", cmd)

    def test_dry_run_does_not_pass_apply_and_bad_names_fail_early(self):
        calls, run = self.fake_run()
        with mock.patch.object(self.sp.subprocess, "run", run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.sp.main(["--panel-name", "P"]), 0)
            self.sp.results.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(self.sp.main(["--panel-name", "<Panel>"]), 1)
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(len(calls), 1)         # the bad name never reached the script

    def test_failed_setup_returns_1(self):
        calls, run = self.fake_run(rc=2, out="STOP: NoCredentialsError: Unable to locate credentials")
        buf = io.StringIO()
        with mock.patch.object(self.sp.subprocess, "run", run), contextlib.redirect_stdout(buf):
            self.assertEqual(self.sp.main(["--panel-name", "P", "--apply"]), 1)
        self.assertIn("[FAIL] 3 setup", buf.getvalue())
        self.assertIn("no S3 permission", buf.getvalue())


class PanelSnippet(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self._td.name)
        self.addCleanup(os.chdir, self.cwd)
        Path("scripts").mkdir()
        Path("scripts/fathom_recovery.py").write_text("# stub")
        Path("recovery_config.json").write_text('{"panel_name": "P", "output_dir": "outputs"}')
        self.ps = load("panel_snippet", copy_to=os.path.join(self._td.name, "scripts"))

    def test_sweep_now_and_code_sweep_now_run_the_cli_with_state_files(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "SUMMARY uploaded=2 unchanged=5 failed=0", "")
        with mock.patch.object(self.ps.subprocess, "run", run), contextlib.redirect_stdout(io.StringIO()):
            r1 = self.ps.sweep_now()
            r2 = self.ps.code_sweep_now()
        self.assertEqual((r1["ok"], r1["uploaded"], r1["unchanged"], r1["failed"]), (True, 2, 5, 0))
        self.assertEqual(seen[0][seen[0].index("--state-file") + 1], os.path.join(".fathom_recovery", "state.json"))
        self.assertIn("code-sweep", seen[1])
        self.assertEqual(seen[1][seen[1].index("--state-file") + 1], os.path.join(".fathom_recovery", "code_state.json"))
        self.assertTrue(all("--apply" in c for c in seen))
        self.assertTrue(r2["ok"])

    def test_failures_and_stop_are_reported_not_hidden(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 2, "STOP: AccessDenied", "")
        buf = io.StringIO()
        with mock.patch.object(self.ps.subprocess, "run", run), contextlib.redirect_stdout(buf):
            r = self.ps.sweep_now()
        self.assertFalse(r["ok"])
        self.assertEqual(r["exit_code"], 2)
        self.assertIn("PROBLEM", buf.getvalue())

    def test_missing_config_is_a_clear_error(self):
        os.remove("recovery_config.json")
        with self.assertRaises(FileNotFoundError):
            self.ps.sweep_now()

    def test_no_old_api_names_remain(self):
        text = Path(ROOT, "panel_snippet.py").read_text()
        for old in ("upload_to_recovery", "SWEEP_DEFAULT_PATTERNS", "copy_created_object_to_recovery", "PANEL_NAME ="):
            self.assertNotIn(old, text)
        self.assertTrue(hasattr(self.ps, "sweep_now") and hasattr(self.ps, "code_sweep_now"))


class SyncRecovery(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.sr = load("sync_recovery")
        self.repo = os.path.join(self._td.name, "src")
        os.makedirs(os.path.join(self.repo, "tests"))
        Path(self.repo, "fathom_recovery.py").write_text('__version__ = "9.9.9"\n')
        Path(self.repo, "tests", "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "x"],
                    ["git", "tag", "v9.9.9"]):
            subprocess.run(cmd, cwd=self.repo, env=env, check=True, capture_output=True)

    def test_sync_installs_tagged_file_and_reports_version_and_sha(self):
        dest = os.path.join(self._td.name, "dest")
        res = self.sr.sync(self.repo, "v9.9.9", dest, run_tests=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["version"], "9.9.9")
        self.assertTrue(res["tests"]["ok"])
        installed = Path(dest, "fathom_recovery.py")
        self.assertEqual(res["sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())

    def test_unknown_ref_fails_and_run_setup_uses_setup_not_init(self):
        with self.assertRaises(RuntimeError):
            self.sr.sync(self.repo, "v0.0.0", os.path.join(self._td.name, "d2"))
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "SUMMARY uploaded=0 unchanged=0 failed=0", "")
        with mock.patch.object(self.sr.subprocess, "run", run):
            res = self.sr.run_setup("scripts/fathom_recovery.py", "P", "outputs", apply=False)
        self.assertTrue(res["ok"])
        self.assertEqual(seen[0][3], "setup")
        self.assertNotIn("--apply", seen[0])
        self.assertNotIn("init", seen[0])


class RepoFiles(unittest.TestCase):
    def test_no_stale_api_names_in_companion_files_or_readme(self):
        stale = ("upload_to_recovery", "copy_created_object_to_recovery", "put_object_and_copy_to_recovery",
                 "SWEEP_DEFAULT_PATTERNS", "make_day_folder", "make_run_folder", "list_recoveries", "_require_panel",
                 "fr.CONFIG_NAME", "render_instructions", "RECOVERY_INSTRUCTIONS.md", "fathom_recovery.py init",
                 "fathom_recovery.py upload", "fathom_recovery.py copy")
        for name in ("panel_snippet.py", "setup_panel.py", "sync_recovery.py", "README.md"):
            text = Path(ROOT, name).read_text()
            for s in stale:
                self.assertNotIn(s, text, f"{name} still mentions {s}")

    def test_readme_names_current_version_and_commands(self):
        text = Path(ROOT, "README.md").read_text()
        self.assertIn("v1.9.2", text)
        self.assertNotIn("--branch v1.9.1", text)       # the tag v1.9.1 only has the script, not the companion files
        for cmd in ("setup", "sweep", "code-sweep", "selfcheck", "list-code", "restore-code", "verify-code"):
            self.assertIn(cmd, text)
        self.assertIn("<YYYYMMDD>_current_data", text)
        self.assertIn('PROBE_DIR = "data-day"', text)

    def test_gitignore_keeps_state_and_bytecode_out(self):
        text = Path(ROOT, ".gitignore").read_text()
        for line in ("__pycache__/", "*.pyc", ".fathom_recovery/"):
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
