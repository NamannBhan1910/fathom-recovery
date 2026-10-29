"""Offline tests (fake S3, no network, no writes). Run: python3 -m unittest discover -s tests -v"""
import hashlib
import io
import os
import re
import sys
import unittest
from datetime import datetime, timezone

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["RECOVERY_USER"] = "tester"
import fathom_recovery as fr  # noqa: E402


class FakeS3:
    def __init__(self):
        self.objs = {}  # (bucket, key) -> dict(data, ct, meta, lm)
        self.copies = []

    def seed(self, b, k, data, ct="text/csv", meta=None):
        self.objs[(b, k)] = {"data": data, "ct": ct, "meta": meta or {}, "lm": datetime.now(timezone.utc)}

    def _get(self, b, k, op):
        if (b, k) not in self.objs:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, op)
        return self.objs[(b, k)]

    def head_object(self, Bucket, Key):
        o = self._get(Bucket, Key, "HeadObject")
        return {"ContentLength": len(o["data"]), "ContentType": o["ct"], "LastModified": o["lm"], "Metadata": o["meta"]}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._get(Bucket, Key, "GetObject")["data"])}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):
        self.seed(Bucket, Key, Body, ContentType, Metadata)

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective, ContentType=None, Metadata=None):
        src = self._get(CopySource["Bucket"], CopySource["Key"], "CopyObject")
        if MetadataDirective == "REPLACE":
            self.seed(Bucket, Key, src["data"], ContentType, Metadata)
        else:
            self.seed(Bucket, Key, src["data"], src["ct"], dict(src["meta"]))
        self.copies.append(Key)

    def get_paginator(self, name):
        fs = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = [(k, o) for (b, k), o in fs.objs.items() if b == Bucket and k.startswith(Prefix)]
                if Delimiter:
                    cps = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                                  for k, _ in keys if Delimiter in k[len(Prefix):]})
                    direct = [{"Key": k, "Size": len(o["data"]), "LastModified": o["lm"]}
                              for k, o in keys if Delimiter not in k[len(Prefix):]]
                    yield {"Contents": direct, "CommonPrefixes": [{"Prefix": c} for c in cps]}
                else:
                    yield {"Contents": [{"Key": k, "Size": len(o["data"]), "LastModified": o["lm"]}
                                        for k, o in keys]}
        return P()



class KeyLayout(unittest.TestCase):
    def test_layout(self):
        now = datetime(2026, 10, 27, 14, 45, 7, tzinfo=timezone.utc)
        k = fr.build_destination_key("a/b/report.csv", "Daily Checklist Brokerage", user="test-user", now=now)
        self.assertRegex(k, r"^6_Recovery/test-user/Daily Checklist Brokerage/20261027T144507Z-[0-9a-f]{8}/report\.csv$")

    def test_unique_per_call(self):
        a = fr.build_destination_key("x.csv", "P", user="u")
        b = fr.build_destination_key("x.csv", "P", user="u")
        self.assertNotEqual(a, b)

    def test_panels_get_separate_folders(self):
        a = fr.build_destination_key("checklist_state.json", "Panel A", user="u")
        b = fr.build_destination_key("checklist_state.json", "Panel B", user="u")
        self.assertTrue(a.startswith("6_Recovery/u/Panel A/"))
        self.assertTrue(b.startswith("6_Recovery/u/Panel B/"))

    def test_name_is_exact(self):
        a = fr.build_destination_key("x.csv", "Daily Checklist Equities", user="u")
        b = fr.build_destination_key("x.csv", "daily checklist equities", user="u")
        self.assertNotEqual(a.split("/")[2], b.split("/")[2])

    def test_slashes_cannot_escape(self):
        k = fr.build_destination_key("x.csv", "../Evil/Panel", user="u")
        self.assertNotIn("..", k)
        self.assertEqual(len(k.split("/")), 5)  # prefix/user/panel/run/file

    def test_bad_panel_names(self):
        for bad in ["", "   ", "..", ".", None, "///"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                fr.build_destination_key("x.csv", bad, user="u")

    def test_no_filename(self):
        with self.assertRaises(ValueError):
            fr.build_destination_key("folder/", "P", user="u")

    def test_user_resolution(self):
        self.assertEqual(fr.resolve_user("jdoe@corp.com"), "jdoe")
        self.assertEqual(fr.resolve_user(None), "tester")  # from RECOVERY_USER


class CopyAndPut(unittest.TestCase):
    def test_put_then_copy_writes_sha_and_content_type(self):
        s3 = FakeS3()
        data = b"a,b\n1,2\n"
        res = fr.put_object_and_copy_to_recovery("src", "out/data.csv", data, panel_name="Equities Checklist EU",
                                                 destination_bucket="rec", s3=s3)
        cr = res["copy_result"]
        self.assertTrue(res["ok"])
        self.assertEqual(cr["action"], "copied")
        self.assertTrue(cr["recovery_path"].startswith("tester/Equities Checklist EU/"))
        dest = s3.copies[0]
        self.assertEqual(s3.objs[("rec", dest)]["meta"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(s3.objs[("rec", dest)]["ct"], "text/csv")
        self.assertIn(("src", "out/data.csv"), s3.objs)  # source untouched

    def test_dry_run_writes_nothing(self):
        s3 = FakeS3()
        s3.seed("src", "x/y.csv", b"zz")
        r = fr.copy_created_object_to_recovery("src", "x/y.csv", panel_name="P", destination_bucket="rec",
                                               dry_run=True, s3=s3)
        self.assertEqual(r["action"], "would_copy")
        self.assertEqual(s3.copies, [])

    def test_bad_panel_rejected_before_any_write(self):
        s3 = FakeS3()
        with self.assertRaises(ValueError):
            fr.put_object_and_copy_to_recovery("src", "k.csv", b"x", panel_name=" ", s3=s3)
        self.assertEqual(s3.objs, {})  # nothing was created

    def test_missing_source_raises(self):
        with self.assertRaises(ClientError):
            fr.copy_created_object_to_recovery("src", "nope.csv", panel_name="P", s3=FakeS3())

    def test_two_copies_both_kept(self):
        s3 = FakeS3()
        s3.seed("src", "f.csv", b"1")
        fr.copy_created_object_to_recovery("src", "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        fr.copy_created_object_to_recovery("src", "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        self.assertEqual(len(set(s3.copies)), 2)


class ListAndRestore(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        s = self.s3
        s.seed("rec", "6_Recovery/u1/Panel A/20261027T100000Z-aaaaaaaa/f.csv", b"v1", meta={"sha256": hashlib.sha256(b"v1").hexdigest()})
        s.seed("rec", "6_Recovery/u2/Panel A/20261027T110000Z-bbbbbbbb/f.csv", b"v2")
        s.seed("rec", "6_Recovery/u1/Panel A/", b"")  # folder marker
        s.seed("rec", "6_Recovery/u1/Panel B/20261027T120000Z-cccccccc/f.csv", b"other")
        # objects written before v1.6.0, panel-first: <panel>/<user>/<run>/<file>
        s.seed("rec", "6_Recovery/Panel A/u3/20261026T090000Z-dddddddd/old.csv", b"old")
        s.seed("rec", "6_Recovery/Panel A/legacy.csv", b"older")

    def test_list_is_scoped_to_user_and_panel(self):
        rows = fr.list_recoveries("Panel A", user="u1", destination_bucket="rec", s3=self.s3)
        self.assertEqual([r["recovery_path"] for r in rows], ["u1/Panel A/20261027T100000Z-aaaaaaaa/f.csv"])
        rows2 = fr.list_recoveries("Panel A", user="u2", destination_bucket="rec", s3=self.s3)
        self.assertEqual([r["recovery_path"] for r in rows2], ["u2/Panel A/20261027T110000Z-bbbbbbbb/f.csv"])
        rowsb = fr.list_recoveries("Panel B", user="u1", destination_bucket="rec", s3=self.s3)
        self.assertEqual(len(rowsb), 1)

    def test_list_parsing_and_default_user(self):
        r = fr.list_recoveries("Panel A", user="u1", destination_bucket="rec", s3=self.s3)[0]
        self.assertEqual((r["user"], r["panel"], r["run_id"], r["filename"]),
                         ("u1", "Panel A", "20261027T100000Z-aaaaaaaa", "f.csv"))
        # no user given -> the current user ("tester" from RECOVERY_USER), who has no objects here
        self.assertEqual(fr.list_recoveries("Panel A", destination_bucket="rec", s3=self.s3), [])

    def test_users_do_not_see_each_others_panels(self):
        self.s3.seed("rec", "6_Recovery/tester/Mine/20261027T130000Z-eeeeeeee/m.csv", b"m")
        self.assertEqual(len(fr.list_recoveries("Mine", destination_bucket="rec", s3=self.s3)), 1)
        self.assertEqual(fr.list_recoveries("Mine", user="u1", destination_bucket="rec", s3=self.s3), [])

    def test_legacy_listing_finds_old_layout_only(self):
        rows = fr.list_legacy_recoveries("panel a", destination_bucket="rec", s3=self.s3)
        self.assertEqual(sorted(r["filename"] for r in rows), ["legacy.csv", "old.csv"])
        old = [r for r in rows if r["filename"] == "old.csv"][0]
        self.assertEqual((old["user"], old["run_id"]), ("u3", "20261026T090000Z-dddddddd"))
        self.assertIsNone([r for r in rows if r["filename"] == "legacy.csv"][0]["run_id"])
        only_u3 = fr.list_legacy_recoveries("Panel A", user="u3", destination_bucket="rec", s3=self.s3)
        self.assertEqual([r["filename"] for r in only_u3], ["old.csv"])
        # a legacy object is restorable by its path
        r = fr.restore_from_recovery(old["recovery_path"], "live", "o.csv", destination_bucket="rec", s3=self.s3)
        self.assertTrue(r["ok"])

    def test_restore_verifies_hash(self):
        p = "u1/Panel A/20261027T100000Z-aaaaaaaa/f.csv"
        r = fr.restore_from_recovery(p, "live", "panel/f.csv", destination_bucket="rec", s3=self.s3)
        self.assertTrue(r["ok"])
        self.assertTrue(r["sha256_verified"])
        self.assertEqual(self.s3.objs[("live", "panel/f.csv")]["data"], b"v1")

    def test_restore_refuses_overwrite(self):
        self.s3.seed("live", "panel/f.csv", b"current")
        p = "u1/Panel A/20261027T100000Z-aaaaaaaa/f.csv"
        r = fr.restore_from_recovery(p, "live", "panel/f.csv", destination_bucket="rec", s3=self.s3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "refused")
        self.assertEqual(self.s3.objs[("live", "panel/f.csv")]["data"], b"current")
        r2 = fr.restore_from_recovery(p, "live", "panel/f.csv", overwrite=True, destination_bucket="rec", s3=self.s3)
        self.assertTrue(r2["ok"])

    def test_restore_dry_run_and_bad_path(self):
        p = "u1/Panel A/20261027T100000Z-aaaaaaaa/f.csv"
        r = fr.restore_from_recovery(p, "live", "k.csv", dry_run=True, destination_bucket="rec", s3=self.s3)
        self.assertEqual(r["action"], "would_restore")
        self.assertNotIn(("live", "k.csv"), self.s3.objs)
        for bad in ["", "Panel A", "../x/y", "Panel A//f.csv"]:
            with self.assertRaises(ValueError, msg=bad):
                fr.restore_from_recovery(bad, "live", "k", destination_bucket="rec", s3=self.s3)


class Upload(unittest.TestCase):
    def test_bytes_upload_layout_and_verification(self):
        s3 = FakeS3()
        data = b"a,b\n1,2\n"
        r = fr.upload_to_recovery(data, "r.csv", panel_name="Panel X", destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        self.assertRegex(r["recovery_path"], r"^tester/Panel X/\d{8}T\d{6}Z-[0-9a-f]{8}/r\.csv$")
        (bucket, key), obj = next(iter(s3.objs.items()))
        self.assertEqual(bucket, "rec")
        self.assertTrue(key.startswith("6_Recovery/tester/Panel X/"))
        self.assertEqual(obj["data"], data)
        self.assertEqual(obj["meta"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(obj["ct"], "text/csv")
        self.assertTrue(r["size_verified"] and r["sha256_metadata_verified"] and r["content_verified"])

    def test_str_is_content_and_path_is_file(self):
        import pathlib
        import tempfile
        s3 = FakeS3()
        fr.upload_to_recovery("hello", "h.txt", panel_name="P", destination_bucket="rec", s3=s3)
        self.assertEqual(list(s3.objs.values())[0]["data"], b"hello")
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "local.csv"
            p.write_bytes(b"x,y\n")
            s3b = FakeS3()
            r = fr.upload_to_recovery(p, panel_name="P", destination_bucket="rec", s3=s3b)
            self.assertTrue(r["ok"])
            self.assertEqual(r["filename"], "local.csv")

    def test_dry_run_writes_nothing(self):
        s3 = FakeS3()
        r = fr.upload_to_recovery(b"zz", "f.csv", panel_name="P", dry_run=True, destination_bucket="rec", s3=s3)
        self.assertEqual(r["action"], "would_upload")
        self.assertEqual(s3.objs, {})

    def test_validation_before_any_write(self):
        s3 = FakeS3()
        for kwargs in ({"panel_name": " "}, {"panel_name": ".."}, {"panel_name": "P", "filename": ""},
                       {"panel_name": "P", "filename": ".."}):
            fname = kwargs.pop("filename", "f.csv")
            with self.assertRaises(ValueError, msg=str(kwargs)):
                fr.upload_to_recovery(b"x", fname, destination_bucket="rec", s3=s3, **kwargs)
        with self.assertRaises(TypeError):
            fr.upload_to_recovery(123, "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        self.assertEqual(s3.objs, {})

    def test_filename_cannot_escape_folder(self):
        s3 = FakeS3()
        r = fr.upload_to_recovery(b"x", "../../evil.csv", panel_name="P", destination_bucket="rec", s3=s3)
        self.assertEqual(r["filename"], "evil.csv")
        self.assertEqual(len(list(s3.objs)[0][1].split("/")), 5)

    def test_two_uploads_both_kept(self):
        s3 = FakeS3()
        fr.upload_to_recovery(b"1", "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        fr.upload_to_recovery(b"2", "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        self.assertEqual(len(s3.objs), 2)

    def test_detects_corruption(self):
        class Corrupting(FakeS3):
            def put_object(self, Bucket, Key, Body, ContentType, Metadata):
                super().put_object(Bucket, Key, Body[:-1] + b"?", ContentType, Metadata)
        r = fr.upload_to_recovery(b"abcdef", "f.txt", panel_name="P", destination_bucket="rec", s3=Corrupting())
        self.assertFalse(r["ok"])

    def test_upload_then_list_and_restore(self):
        s3 = FakeS3()
        r = fr.upload_to_recovery(b"v1", "f.csv", panel_name="P", destination_bucket="rec", s3=s3)
        rows = fr.list_recoveries("P", destination_bucket="rec", s3=s3)
        self.assertEqual([x["recovery_path"] for x in rows], [r["recovery_path"]])
        rr = fr.restore_from_recovery(r["recovery_path"], "live", "k.csv", destination_bucket="rec", s3=s3)
        self.assertTrue(rr["ok"] and rr["sha256_verified"])


class SelfCheck(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        s3 = FakeS3()
        r = fr.selfcheck("P", destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"])
        self.assertEqual(s3.objs, {})
        self.assertIn("skipped", r["steps"][-1]["detail"])

    def test_apply_writes_and_verifies_probe(self):
        s3 = FakeS3()
        r = fr.selfcheck("P", apply=True, destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(s3.objs), 1)
        self.assertEqual([s["ok"] for s in r["steps"]], [True] * 4)

    def test_bad_panel_fails_before_any_io(self):
        s3 = FakeS3()
        r = fr.selfcheck("  ", apply=True, destination_bucket="rec", s3=s3)
        self.assertFalse(r["ok"])
        self.assertEqual(s3.objs, {})

class Sweep(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = __import__("pathlib").Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def w(self, rel, data=b"a,b\n1,2\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_dry_run_uploads_nothing_and_writes_no_state(self):
        self.w("a.csv")
        s3 = FakeS3()
        r = fr.sweep(self.root, "P", destination_bucket="rec", s3=s3)
        self.assertEqual((r["would_upload"], r["uploaded"]), (1, 0))
        self.assertEqual(s3.objs, {})
        self.assertFalse((self.root / fr.SWEEP_STATE_NAME).exists())

    def test_uploads_new_then_skips_unchanged_then_reuploads_changed(self):
        self.w("a.csv", b"v1")
        s3 = FakeS3()
        r1 = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual((r1["uploaded"], r1["ok"]), (1, True))
        r2 = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual((r2["uploaded"], r2["unchanged"]), (0, 1))
        self.assertEqual(len(s3.objs), 1)
        self.w("a.csv", b"v2")
        r3 = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual(r3["uploaded"], 1)
        self.assertEqual(len(s3.objs), 2)  # both versions kept

    def test_new_file_only_is_uploaded(self):
        self.w("a.csv", b"1")
        s3 = FakeS3()
        fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.w("sub/b.csv", b"2")
        r = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual((r["uploaded"], r["unchanged"]), (1, 1))
        self.assertEqual(len(s3.objs), 2)

    def test_patterns_and_skip_dirs(self):
        self.w("a.csv")
        self.w("note.md")
        self.w(".git/x.csv")
        self.w("knowledge/k.csv")
        self.w("__pycache__/c.csv")
        s3 = FakeS3()
        r = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual([f["file"] for f in r["files"]], ["a.csv"])
        r2 = fr.sweep(self.root, "P", patterns=["*.md"], apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual([f["file"] for f in r2["files"]], ["note.md"])

    def test_state_file_not_uploaded_and_non_recursive(self):
        self.w("a.json", b"{}")
        self.w("deep/b.json", b"{}")
        s3 = FakeS3()
        r = fr.sweep(self.root, "P", recursive=False, apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual(r["uploaded"], 1)
        r = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=s3)  # state file is *.json-named? no
        names = [f["file"] for f in r["files"]]
        self.assertNotIn(fr.SWEEP_STATE_NAME, names)

    def test_failed_upload_not_recorded_and_retried(self):
        self.w("a.csv", b"abcdef")

        class Corrupting(FakeS3):
            def put_object(self, Bucket, Key, Body, ContentType, Metadata):
                super().put_object(Bucket, Key, Body[:-1] + b"?", ContentType, Metadata)

        r = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=Corrupting())
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed"], 1)
        r2 = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=FakeS3())
        self.assertEqual((r2["uploaded"], r2["ok"]), (1, True))

    def test_exception_in_one_file_does_not_stop_others(self):
        self.w("a.csv", b"1")
        self.w("b.csv", b"2")

        class Flaky(FakeS3):
            n = 0

            def put_object(self, Bucket, Key, Body, ContentType, Metadata):
                Flaky.n += 1
                if Flaky.n == 1:
                    raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
                super().put_object(Bucket, Key, Body, ContentType, Metadata)

        r = fr.sweep(self.root, "P", apply=True, destination_bucket="rec", s3=Flaky())
        self.assertEqual((r["uploaded"], r["failed"], r["ok"]), (1, 1, False))

    def test_bad_panel_or_dir_rejected_before_io(self):
        s3 = FakeS3()
        for bad in ("", "..", "<Panel Name given by the user>"):
            with self.assertRaises(ValueError, msg=bad):
                fr.sweep(self.root, bad, apply=True, destination_bucket="rec", s3=s3)
        with self.assertRaises(ValueError):
            fr.sweep(self.root / "nope", "P", s3=s3)
        self.assertEqual(s3.objs, {})


class PlaceholderGuard(unittest.TestCase):
    def test_placeholder_panel_names_rejected_everywhere(self):
        s3 = FakeS3()
        with self.assertRaises(ValueError):
            fr.upload_to_recovery(b"x", "f.csv", panel_name="<Panel Name given by the user>", destination_bucket="rec", s3=s3)
        with self.assertRaises(ValueError):
            fr.build_destination_key("x.csv", "<name>", user="u")
        self.assertEqual(s3.objs, {})

    def test_user_with_angle_brackets_is_not_blocked(self):
        self.assertEqual(fr.resolve_user("a<b"), "a<b")

class PanelSnippet(unittest.TestCase):
    """panel_snippet.py must import fathom_recovery from its own folder OR from ./scripts."""

    def _run(self, layout):
        import pathlib
        import shutil
        import subprocess
        import tempfile
        root = pathlib.Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            shutil.copy(root / "panel_snippet.py", d)
            dest = d / "scripts" if layout == "scripts" else d
            dest.mkdir(exist_ok=True)
            shutil.copy(root / "fathom_recovery.py", dest)
            code = ("import panel_snippet as p, fathom_recovery as f\n"
                    "assert p.sweep_now and p.push\n"
                    "print('OK', f.__version__)\n")
            return subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, text=True)

    def test_import_from_scripts_folder(self):
        r = self._run("scripts")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_from_same_folder(self):
        r = self._run("same")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sweep_now_uses_same_default_patterns_as_sweep(self):
        import inspect
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "panel_snippet.py").read_text()
        self.assertIn("SWEEP_DEFAULT_PATTERNS", src)
        self.assertIn("patterns=None", src)
        self.assertIn(".txt", ",".join(fr.SWEEP_DEFAULT_PATTERNS))


class CaseInsensitivePanel(unittest.TestCase):
    EXISTING = "6_Recovery/tester/Daily Checklist Equities/20261027T100000Z-aaaaaaaa/f.csv"

    def fs(self):
        s3 = FakeS3()
        s3.seed("rec", self.EXISTING, b"old")
        s3.seed("rec", "6_Recovery/tester/Brokerage Recon/", b"")  # folder marker only
        return s3

    def test_resolve_matches_existing_ignoring_case_and_spaces(self):
        s3 = self.fs()
        for given in ("daily checklist equities", "DAILY CHECKLIST EQUITIES", "  Daily   Checklist  Equities "):
            self.assertEqual(fr.resolve_panel_folder(given, destination_bucket="rec", s3=s3),
                             "Daily Checklist Equities", given)
        self.assertEqual(fr.resolve_panel_folder("brokerage recon", destination_bucket="rec", s3=s3), "Brokerage Recon")

    def test_exact_name_and_new_name(self):
        s3 = self.fs()
        self.assertEqual(fr.resolve_panel_folder("Daily Checklist Equities", destination_bucket="rec", s3=s3),
                         "Daily Checklist Equities")
        self.assertEqual(fr.resolve_panel_folder("Brand New Panel", destination_bucket="rec", s3=s3), "Brand New Panel")

    def test_ambiguous_existing_folders_raise(self):
        s3 = self.fs()
        s3.seed("rec", "6_Recovery/tester/daily checklist equities/r/f.csv", b"x")
        with self.assertRaises(ValueError):
            fr.resolve_panel_folder("DAILY CHECKLIST EQUITIES", destination_bucket="rec", s3=s3)
        # an exact spelling still works
        self.assertEqual(fr.resolve_panel_folder("daily checklist equities", destination_bucket="rec", s3=s3),
                         "daily checklist equities")

    def test_upload_uses_existing_folder_spelling(self):
        s3 = self.fs()
        r = fr.upload_to_recovery(b"a,b\n", "x.csv", panel_name="daily checklist equities",
                                  destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["recovery_path"].startswith("tester/Daily Checklist Equities/"))
        self.assertEqual(r["panel_folder"], "Daily Checklist Equities")
        self.assertFalse(any(k.startswith("6_Recovery/tester/daily checklist equities/") for (_, k) in s3.objs))

    # ---- user folder is matched case-insensitively (v1.6.1) ----
    def us(self):
        s3 = FakeS3()
        s3.seed("rec", "6_Recovery/NBHAN/Daily Checklist/20261027T100000Z-aaaaaaaa/f.csv", b"old")
        s3.seed("rec", "6_Recovery/other/Daily Checklist/20261027T100000Z-bbbbbbbb/f.csv", b"o")
        return s3

    def test_layout_marker(self):
        self.assertEqual(fr.LAYOUT, "user/panel")

    def test_user_folder_matches_ignoring_case(self):
        s3 = self.us()
        for given in ("nbhan", "NbHaN", "NBHAN", "nbhan@corp.com"):
            self.assertEqual(fr.resolve_user_folder(given, destination_bucket="rec", s3=s3), "NBHAN", given)
        self.assertEqual(fr.resolve_user_folder("newuser", destination_bucket="rec", s3=s3), "newuser")

    def test_user_folder_ambiguous_raises_but_exact_wins(self):
        s3 = self.us()
        s3.seed("rec", "6_Recovery/nbhan/P/r/f.csv", b"x")
        with self.assertRaises(ValueError):
            fr.resolve_user_folder("NbHaN", destination_bucket="rec", s3=s3)
        self.assertEqual(fr.resolve_user_folder("nbhan", destination_bucket="rec", s3=s3), "nbhan")
        self.assertEqual(fr.resolve_user_folder("NBHAN", destination_bucket="rec", s3=s3), "NBHAN")

    def test_upload_lowercase_user_lands_in_existing_user_folder(self):
        s3 = self.us()
        r = fr.upload_to_recovery(b"a\n", "x.csv", panel_name="daily checklist", user="nbhan",
                                  destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["recovery_path"].startswith("NBHAN/Daily Checklist/"), r["recovery_path"])
        self.assertFalse([k for (_, k) in s3.objs if k.startswith("6_Recovery/nbhan/")])

    def test_list_and_selfcheck_use_existing_user_folder(self):
        s3 = self.us()
        rows = fr.list_recoveries("Daily Checklist", user="nbhan", destination_bucket="rec", s3=s3)
        self.assertEqual([r["recovery_path"] for r in rows], ["NBHAN/Daily Checklist/20261027T100000Z-aaaaaaaa/f.csv"])
        self.assertEqual(rows[0]["user"], "NBHAN")
        res = fr.selfcheck("daily checklist", user="nbhan", apply=True, destination_bucket="rec", s3=s3)
        self.assertTrue(res["ok"], res)
        probes = [k for (_, k) in s3.objs if k.endswith("selfcheck_probe.txt")]
        self.assertEqual(len(probes), 1)
        self.assertTrue(probes[0].startswith("6_Recovery/NBHAN/Daily Checklist/"), probes)

    def test_sweep_lowercase_user_lands_in_existing_user_folder(self):
        import tempfile
        from pathlib import Path
        s3 = self.us()
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.csv").write_text("1\n")
            Path(d, "b.json").write_text("{}")
            r = fr.sweep(d, "DAILY checklist", user="nbhan", apply=True, destination_bucket="rec", s3=s3)
            self.assertEqual((r["uploaded"], r["failed"]), (2, 0), r)
        new = [k for (_, k) in s3.objs if k.endswith(("a.csv", "b.json"))]
        self.assertEqual(len(new), 2)
        self.assertTrue(all(k.startswith("6_Recovery/NBHAN/Daily Checklist/") for k in new), new)

    def test_user_listing_failure_falls_back_with_warning(self):
        from botocore.exceptions import ClientError as CE

        class Denied(FakeS3):
            def get_paginator(self, name):
                raise CE({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        r = fr.upload_to_recovery(b"a\n", "x.csv", panel_name="P", user="nbhan",
                                  destination_bucket="rec", s3=Denied())
        self.assertTrue(r["recovery_path"].startswith("nbhan/P/"), r)
        self.assertIn("warning", r)

    def test_dry_run_reports_matched_folder_and_writes_nothing(self):
        s3 = self.fs()
        n = len(s3.objs)
        r = fr.upload_to_recovery(b"x", "x.csv", panel_name="BROKERAGE RECON", dry_run=True,
                                  destination_bucket="rec", s3=s3)
        self.assertTrue(r["recovery_path"].startswith("tester/Brokerage Recon/"))
        self.assertEqual(len(s3.objs), n)

    def test_list_and_copy_use_matched_folder(self):
        s3 = self.fs()
        rows = fr.list_recoveries("DAILY checklist EQUITIES", destination_bucket="rec", s3=s3)
        self.assertEqual([r["filename"] for r in rows], ["f.csv"])
        s3.seed("src", "k/y.csv", b"zz")
        c = fr.copy_created_object_to_recovery("src", "k/y.csv", panel_name="daily checklist equities",
                                               destination_bucket="rec", s3=s3)
        self.assertTrue(c["recovery_path"].startswith("tester/Daily Checklist Equities/"))

    def test_sweep_uses_matched_folder(self):
        import pathlib
        import tempfile
        s3 = self.fs()
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "a.csv").write_bytes(b"1")
            (pathlib.Path(d) / "b.csv").write_bytes(b"2")
            r = fr.sweep(d, "daily checklist equities", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual(r["uploaded"], 2)
        self.assertTrue(all(f["recovery_path"].startswith("tester/Daily Checklist Equities/")
                            for f in r["files"] if f["action"] == "uploaded"))

    def test_selfcheck_probe_uses_matched_folder(self):
        s3 = self.fs()
        r = fr.selfcheck("daily checklist equities", apply=True, destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        probes = [k for (_, k) in s3.objs if k.endswith("selfcheck_probe.txt")]
        self.assertEqual(len(probes), 1)
        self.assertTrue(probes[0].startswith("6_Recovery/tester/Daily Checklist Equities/"))

    def test_listing_failure_falls_back_with_warning(self):
        class NoList(FakeS3):
            def get_paginator(self, name):
                raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                                  "ListObjectsV2")
        s3 = NoList()
        r = fr.upload_to_recovery(b"x", "x.csv", panel_name="My Panel", destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"])
        self.assertTrue(r["recovery_path"].startswith("tester/My Panel/"))
        self.assertIn("warning", r)

    def test_bad_names_still_rejected(self):
        s3 = self.fs()
        for bad in ("", "..", "<name>"):
            with self.assertRaises(ValueError, msg=bad):
                fr.resolve_panel_folder(bad, destination_bucket="rec", s3=s3)


class PanelConfigAndInstructions(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = __import__("pathlib").Path(self._td.name)

    def test_init_writes_config_and_instructions(self):
        r = fr.init_panel("daily checklist brokerage", "outputs", directory=self.d)
        self.assertTrue(r["ok"])
        cfg = __import__("json").loads((self.d / "recovery_config.json").read_text())
        self.assertEqual(cfg, {"panel_name": "daily checklist brokerage", "output_dir": "outputs"})
        ins = (self.d / "RECOVERY_INSTRUCTIONS.md").read_text()
        self.assertIn("sweep --config recovery_config.json --apply", ins)
        self.assertIn("daily checklist brokerage", ins)
        self.assertIn("Nothing uploads by itself", ins)

    def test_init_normalises_spaces_and_stores_patterns(self):
        fr.init_panel("  My   Panel ", "out", directory=self.d, patterns=["*.csv"])
        cfg = __import__("json").loads((self.d / "recovery_config.json").read_text())
        self.assertEqual((cfg["panel_name"], cfg["patterns"]), ("My Panel", ["*.csv"]))

    def test_init_refuses_overwrite_unless_force(self):
        fr.init_panel("A", directory=self.d)
        with self.assertRaises(ValueError):
            fr.init_panel("B", directory=self.d)
        self.assertIn('"A"', (self.d / "recovery_config.json").read_text())
        fr.init_panel("B", directory=self.d, force=True)
        self.assertIn('"B"', (self.d / "recovery_config.json").read_text())

    def test_init_rejects_bad_names_and_writes_nothing(self):
        for bad in ("", "..", "<Panel>"):
            with self.assertRaises(ValueError, msg=bad):
                fr.init_panel(bad, directory=self.d)
        self.assertEqual(list(self.d.iterdir()), [])

    def test_load_config_resolves_output_dir_next_to_config(self):
        fr.init_panel("P", "outputs", directory=self.d)
        cfg = fr.load_config(self.d / "recovery_config.json")
        self.assertEqual(cfg["output_dir_resolved"], str((self.d / "outputs").resolve()))

    def test_load_config_errors(self):
        p = self.d / "recovery_config.json"
        with self.assertRaises(ValueError):
            fr.load_config(p)  # missing
        p.write_text("not json")
        with self.assertRaises(ValueError):
            fr.load_config(p)
        p.write_text('{"panel_name": "<Panel Name>"}')
        with self.assertRaises(ValueError):
            fr.load_config(p)  # placeholder
        p.write_text('{"panel_name": "P", "patterns": "*.csv"}')
        with self.assertRaises(ValueError):
            fr.load_config(p)  # patterns must be a list

    def _cli(self, *args, cwd=None):
        import subprocess
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        env = dict(os.environ, RECOVERY_USER="tester")
        return subprocess.run([sys.executable, str(root / "fathom_recovery.py"), *args], cwd=cwd,
                              capture_output=True, text=True, env=env)

    def test_cli_init_then_sweep_config_dry_run_from_other_folder(self):
        r = self._cli("init", "--panel-name", "Cfg Panel", "--directory", str(self.d))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        (self.d / "outputs").mkdir()
        (self.d / "outputs" / "a.csv").write_text("x\n1\n")
        other = self.d / "elsewhere"
        other.mkdir()
        # dry run, run from a DIFFERENT folder: the config path alone must be enough
        r = self._cli("sweep", "--config", str(self.d / "recovery_config.json"), cwd=other)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        d = __import__("json").loads(r.stdout)
        self.assertEqual((d["scanned"], d["would_upload"], d["uploaded"]), (1, 1, 0))

    def test_cli_sweep_config_creates_missing_output_dir(self):
        self._cli("init", "--panel-name", "Cfg Panel", "--directory", str(self.d))
        self.assertFalse((self.d / "outputs").exists())
        r = self._cli("sweep", "--config", str(self.d / "recovery_config.json"))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.d / "outputs").is_dir())
        self.assertEqual(__import__("json").loads(r.stdout)["scanned"], 0)

    def test_cli_sweep_needs_panel_or_config(self):
        r = self._cli("sweep", "--dir", str(self.d))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--panel-name or --config", r.stdout)

    def test_cli_explicit_args_override_config(self):
        self._cli("init", "--panel-name", "From Config", "--directory", str(self.d))
        (self.d / "other").mkdir()
        (self.d / "other" / "b.csv").write_text("y\n")
        r = self._cli("sweep", "--config", str(self.d / "recovery_config.json"),
                      "--dir", str(self.d / "other"), "--pattern", "*.csv")
        d = __import__("json").loads(r.stdout)
        self.assertEqual([f["file"] for f in d["files"]], ["b.csv"])

    def test_sync_with_panel_name_writes_files_and_upgrade_keeps_config(self):
        import json
        import shutil
        import subprocess
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        ident = ["-c", "user.name=t", "-c", "user.email=t@e.com"]
        repo = self.d / "repo"
        repo.mkdir()
        for f in ("fathom_recovery.py", "sync_recovery.py"):
            shutil.copy(root / f, repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "add", "-A"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "commit", "-q", "-m", "x"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "tag", "v9.9.9"], cwd=repo, check=True, env=env)
        panel = self.d / "panel"
        panel.mkdir()

        def run(*extra):
            return subprocess.run([sys.executable, str(root / "sync_recovery.py"), "--repo", str(repo), "--ref", "v9.9.9",
                                   "--dest", "scripts", "--panel-name", "Sync Panel", *extra],
                                  cwd=panel, capture_output=True, text=True, env=env)
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((panel / "scripts" / "fathom_recovery.py").is_file())
        self.assertEqual(json.loads((panel / "recovery_config.json").read_text())["panel_name"], "Sync Panel")
        self.assertTrue((panel / "RECOVERY_INSTRUCTIONS.md").is_file())
        # upgrade: rerun with an edited config; it must stay, and the install must still succeed
        (panel / "recovery_config.json").write_text('{"panel_name": "Edited", "output_dir": "data"}')
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(json.loads(r.stdout)["init"]["action"], "kept_existing_config")
        self.assertIn("Edited", (panel / "recovery_config.json").read_text())

    def test_sync_without_panel_name_writes_no_config(self):
        import subprocess
        import shutil
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        ident = ["-c", "user.name=t", "-c", "user.email=t@e.com"]
        repo = self.d / "repo2"
        repo.mkdir()
        shutil.copy(root / "fathom_recovery.py", repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "add", "-A"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "commit", "-q", "-m", "x"], cwd=repo, check=True, env=env)
        subprocess.run(["git", *ident, "tag", "v9.9.9"], cwd=repo, check=True, env=env)
        panel = self.d / "panel2"
        panel.mkdir()
        r = subprocess.run([sys.executable, str(root / "sync_recovery.py"), "--repo", str(repo), "--ref", "v9.9.9",
                            "--dest", "scripts"], cwd=panel, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse((panel / "recovery_config.json").exists())




class SetupPanel(unittest.TestCase):
    """setup_panel.py: one-command install/config/check. No S3 is touched (dry run + --skip-selfcheck)."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = __import__("pathlib").Path(self._td.name)
        self.root = __import__("pathlib").Path(__file__).resolve().parent.parent

    def _run(self, *args):
        import subprocess
        return subprocess.run([sys.executable, str(self.root / "setup_panel.py"), *args], cwd=self.d,
                              capture_output=True, text=True)

    def test_setup_installs_writes_config_and_is_rerunnable(self):
        r = self._run("--panel-name", "  my   panel ", "--skip-selfcheck")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.d / "scripts" / "fathom_recovery.py").is_file())
        cfg = __import__("json").loads((self.d / "recovery_config.json").read_text())
        self.assertEqual(cfg["panel_name"], "my panel")
        self.assertTrue((self.d / "outputs").is_dir())
        self.assertIn("ALL STEPS PASSED", r.stdout)
        r2 = self._run("--panel-name", "my panel", "--skip-selfcheck")  # second run must also succeed
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("already up to date", r2.stdout)
        self.assertIn("kept as is", r2.stdout)

    def test_existing_config_is_not_overwritten(self):
        (self.d / "recovery_config.json").write_text('{"panel_name": "Edited", "output_dir": "data"}')
        r = self._run("--panel-name", "Other Name", "--skip-selfcheck")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Edited", (self.d / "recovery_config.json").read_text())
        self.assertTrue((self.d / "data").is_dir())  # folder from the config in effect, not --output-dir
        self.assertTrue((self.d / "RECOVERY_INSTRUCTIONS.md").is_file())

    def test_bad_names_fail_clearly_and_write_nothing(self):
        for bad in ("..", "<Panel Name>", "   "):
            r = self._run("--panel-name", bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("FAIL", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(list(self.d.iterdir()), [])

    def test_dry_run_sweep_reports_without_uploading(self):
        (self.d / "outputs").mkdir()
        (self.d / "outputs" / "a.csv").write_text("x\n1\n")
        r = self._run("--panel-name", "P", "--skip-selfcheck")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would_upload=1", r.stdout)
        self.assertIn("uploaded=0", r.stdout)
        self.assertFalse((self.d / "outputs" / ".fathom_recovery_state.json").exists())

    def test_setup_runs_code_sweep_dry_run_and_skip_code_works(self):
        (self.d / "knowledge").mkdir()
        (self.d / "knowledge" / "k.md").write_text("k")
        (self.d / "main.py").write_text("print(1)")
        r = self._run("--panel-name", "P", "--skip-selfcheck")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [l for l in r.stdout.splitlines() if "7 code-sweep" in l][0]
        self.assertIn("(dry run)", line)
        self.assertIn("uploaded=0", line)
        self.assertRegex(line, r"would_upload=[4-9]")  # main.py, k.md, config, instructions, scripts/fathom_recovery.py
        self.assertFalse((self.d / ".fathom_recovery").exists())  # dry run writes no state
        r2 = self._run("--panel-name", "P", "--skip-selfcheck", "--skip-code")
        self.assertIn("[SKIP] 7 code-sweep", r2.stdout)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

    def test_with_snippet_copies_it(self):
        r = self._run("--panel-name", "P", "--skip-selfcheck", "--with-snippet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.d / "panel_snippet.py").is_file())


class SetupCommandAndScopedInit(unittest.TestCase):
    """v1.7.0 fix: the CLI offers `setup` (as the prompt says) and `init` stays inside the recovery scope."""

    def setUp(self):
        import pathlib
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = pathlib.Path(self._td.name)

    def _cli(self, *args):
        import subprocess
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        env = dict(os.environ, RECOVERY_USER="tester")
        return subprocess.run([sys.executable, str(root / "fathom_recovery.py"), *args], cwd=self.d,
                              capture_output=True, text=True, env=env)

    def test_cli_init_does_not_write_instructions_by_default(self):
        r = self._cli("init", "--panel-name", "P")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.d / "recovery_config.json").is_file())
        self.assertFalse((self.d / "RECOVERY_INSTRUCTIONS.md").exists())

    def test_cli_init_with_instructions_flag_writes_it(self):
        r = self._cli("init", "--panel-name", "P", "--with-instructions")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.d / "RECOVERY_INSTRUCTIONS.md").is_file())

    def test_setup_is_a_valid_subcommand(self):
        r = self._cli("setup", "-h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--panel-name", r.stdout)
        self.assertIn("--apply", r.stdout)

    def test_setup_rejects_placeholder_name_and_writes_nothing(self):
        for bad in ("<Panel Name>", "..", ""):
            r = self._cli("setup", "--panel-name", bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("[FAIL] 1 panel name", r.stdout)
        self.assertEqual(list(self.d.iterdir()), [])

    def test_setup_panel_function_dry_run_and_apply_stay_in_scope(self):
        import json
        s3 = FakeS3()
        (self.d / "main.py").write_text("print(1)")
        (self.d / "outputs").mkdir()
        (self.d / "outputs" / "a.csv").write_text("x\n1\n")
        kw = dict(directory=self.d, user="tester", s3=s3)
        r = fr.setup_panel("My Panel", "outputs", **kw)  # dry run
        self.assertTrue(r["ok"], r["lines"])
        self.assertEqual(r["layout"], "user/panel")
        self.assertEqual(r["code_backup"], "1")
        self.assertEqual([s["status"] for s in r["steps"]], ["PASS"] * 7)
        self.assertEqual(s3.objs, {})  # dry run writes nothing to S3
        self.assertFalse((self.d / ".fathom_recovery").exists())
        self.assertFalse((self.d / "RECOVERY_INSTRUCTIONS.md").exists())
        before = {p.name for p in self.d.iterdir()}
        r = fr.setup_panel("My Panel", "outputs", apply=True, **kw)
        self.assertTrue(r["ok"], r["lines"])
        self.assertTrue((self.d / ".fathom_recovery" / "state.json").is_file())
        self.assertTrue((self.d / ".fathom_recovery" / "code_state.json").is_file())
        after = {p.name for p in self.d.iterdir()}
        self.assertEqual(after - before, {".fathom_recovery"})  # nothing else created locally
        self.assertFalse((self.d / "RECOVERY_INSTRUCTIONS.md").exists())
        # existing config is kept, not overwritten
        (self.d / "recovery_config.json").write_text(json.dumps({"panel_name": "Edited", "output_dir": "outputs"}))
        r = fr.setup_panel("Other", "outputs", **kw)
        self.assertIn("kept as is", r["lines"][1])
        self.assertIn("Edited", (self.d / "recovery_config.json").read_text())


class InstructionsText(unittest.TestCase):
    def test_instructions_include_setup_and_versioned_clone(self):
        ins = fr.render_instructions("My Panel", "outputs")
        self.assertIn("Setup (once per panel", ins)
        self.assertIn(f"fathom-recovery-v{fr.__version__}", ins)
        self.assertIn("setup_panel.py", ins)
        self.assertIn(fr.REPO_URL, ins)
        self.assertIn("AccessDenied", ins)
        self.assertIn("sweep --config recovery_config.json --apply", ins)
        self.assertIn("Nothing uploads by itself", ins)

    def test_setup_clone_line_is_idempotent_shell(self):
        ins = fr.render_instructions("P")
        line = [l for l in ins.splitlines() if "git clone" in l][0]
        self.assertTrue(line.strip().startswith("[ -d "))  # skipped when the folder already exists



class CodeSweep(unittest.TestCase):
    """v1.7.0: whole-workspace code + knowledge backup, folder paths kept, per-run manifest."""

    def setUp(self):
        import pathlib
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = pathlib.Path(self._td.name)

    def w(self, rel, data=b"x"):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def sweep(self, s3, **kw):
        kw.setdefault("apply", True)
        return fr.code_sweep(self.d, "Code Panel", destination_bucket="rec", s3=s3, **kw)

    def code_keys(self, s3):
        return sorted(k for (_, k) in s3.objs if "/code/" in k and not k.endswith(fr.CODE_MANIFEST_NAME))

    def test_folder_paths_are_kept_and_same_names_do_not_clash(self):
        self.w("scripts/setup_panel.py", "a")
        self.w("fathom-recovery/setup_panel.py", "b")
        self.w("main.py", "c")
        s3 = FakeS3()
        r = self.sweep(s3)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["uploaded"], 3)
        keys = self.code_keys(s3)
        run = r["run_id"]
        self.assertEqual(keys, sorted(f"6_Recovery/tester/Code Panel/code/{run}/{p}" for p in
                                      ("scripts/setup_panel.py", "fathom-recovery/setup_panel.py", "main.py")))
        self.assertRegex(run, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

    def test_knowledge_and_notebooks_are_included(self):
        self.w("knowledge/_index.md", "k")
        self.w("knowledge/domains/x.md", "k2")
        self.w("analysis.ipynb", "{}")
        self.w("dash.html", "<p>")
        r = self.sweep(FakeS3())
        self.assertEqual(r["uploaded"], 4, r)

    def test_hidden_generated_and_state_files_are_skipped(self):
        self.w(".git/config.py", "g")
        self.w(".fathom_runtime/workspace_state.json", "{}")
        self.w(".workspace_meta.json", "{}")
        self.w(".hidden.py", "h")
        self.w("pkg/__pycache__/m.py", "c")
        self.w("node_modules/lib/i.js", "n")
        self.w(".fathom_recovery/state.json", "{}")
        self.w("real.py", "r")
        s3 = FakeS3()
        r = self.sweep(s3)
        self.assertEqual([f["file"] for f in r["files"]], ["real.py"])
        self.assertEqual(r["uploaded"], 1)

    def test_data_files_in_output_dir_are_left_to_normal_sweep_but_code_there_is_backed_up(self):
        self.w("outputs/result.csv", "a,b")
        self.w("outputs/summary.json", "{}")
        self.w("outputs/report.py", "y=1")
        self.w("settings.json", "{}")  # data-looking file OUTSIDE outputs/ is backed up (it is config)
        r = self.sweep(FakeS3(), data_dir=self.d / "outputs")
        self.assertEqual(sorted(f["file"] for f in r["files"]), ["outputs/report.py", "settings.json"])

    def test_unchanged_files_are_not_uploaded_again_changed_ones_go_in_a_new_run(self):
        self.w("a.py", "1")
        self.w("b.py", "2")
        s3 = FakeS3()
        r1 = self.sweep(s3)
        self.assertEqual((r1["uploaded"], r1["unchanged"]), (2, 0))
        r2 = self.sweep(s3)
        self.assertEqual((r2["uploaded"], r2["unchanged"]), (0, 2))
        self.assertNotIn("run_id", r2)
        n = len(s3.objs)
        self.w("b.py", "22")
        r3 = self.sweep(s3)
        self.assertEqual((r3["uploaded"], r3["unchanged"]), (1, 1))
        self.assertNotEqual(r3["run_id"], r1["run_id"])
        self.assertEqual(len(s3.objs), n + 2)  # one new file + one manifest; nothing overwritten
        self.assertTrue(any(k.endswith(f"{r1['run_id']}/b.py") for (_, k) in s3.objs))

    def test_state_lives_in_dot_fathom_recovery_and_not_in_outputs(self):
        (self.d / "outputs").mkdir()
        self.w("a.py")
        self.sweep(FakeS3(), data_dir=self.d / "outputs")
        self.assertTrue((self.d / ".fathom_recovery" / "code_state.json").is_file())
        self.assertEqual(list((self.d / "outputs").iterdir()), [])

    def test_dry_run_uploads_nothing_and_writes_no_state(self):
        self.w("a.py")
        s3 = FakeS3()
        r = self.sweep(s3, apply=False)
        self.assertEqual((r["would_upload"], r["uploaded"]), (1, 0))
        self.assertEqual(s3.objs, {})
        self.assertFalse((self.d / ".fathom_recovery").exists())

    def test_failed_upload_is_not_recorded_so_it_is_retried(self):
        self.w("good.py", "g")
        self.w("bad.py", "b")

        class Flaky(FakeS3):
            def put_object(self, Bucket, Key, Body, ContentType, Metadata):
                if Key.endswith("bad.py"):
                    raise ClientError({"Error": {"Code": "InternalError"}, "ResponseMetadata": {}}, "PutObject")
                super().put_object(Bucket, Key, Body, ContentType, Metadata)
        s3 = Flaky()
        r = self.sweep(s3)
        self.assertFalse(r["ok"])
        self.assertEqual((r["uploaded"], r["failed"]), (1, 1))
        state = __import__("json").loads((self.d / ".fathom_recovery" / "code_state.json").read_text())
        self.assertEqual(list(state), ["good.py"])
        r2 = self.sweep(FakeS3())  # fresh bucket + same state: only bad.py is still pending
        self.assertEqual((r2["uploaded"], r2["unchanged"]), (1, 1))

    def test_secret_looking_config_names_are_reported_not_uploaded(self):
        self.w("config/credentials.json", "{}")
        self.w("api_token.txt", "t")
        self.w("service_account.json", "{}")
        self.w("tokenizer.py", "ok = 1")  # code files are never judged by name
        s3 = FakeS3()
        r = self.sweep(s3)
        self.assertEqual(r["skipped_sensitive"], 3)
        self.assertEqual(r["uploaded"], 1)
        self.assertEqual(sorted(f["file"] for f in r["files"] if f["action"] == "skipped_sensitive_name"),
                         ["api_token.txt", "config/credentials.json", "service_account.json"])
        self.assertFalse(any("credentials" in k or "token.txt" in k for (_, k) in s3.objs))

    def test_large_files_are_skipped_and_reported(self):
        self.w("big.py", "x" * 50)
        self.w("small.py", "y")
        r = self.sweep(FakeS3(), max_bytes=10)
        self.assertEqual((r["skipped_large"], r["uploaded"]), (1, 1))

    def test_symlinks_are_not_followed(self):
        self.w("real.py", "r")
        try:
            (self.d / "link.py").symlink_to(self.d / "real.py")
            (self.d / "linkdir").symlink_to(self.d / "scripts_x", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not supported")
        self.w("scripts_x/inner.py", "i")
        r = self.sweep(FakeS3())
        self.assertEqual(sorted(f["file"] for f in r["files"]), ["real.py", "scripts_x/inner.py"])

    def test_exclude_dirs_and_custom_patterns(self):
        self.w("uploads/a.py")
        self.w("keep/b.py")
        self.w("keep/c.rs")
        r = self.sweep(FakeS3(), exclude_dirs=["uploads"], patterns=["*.py", "*.rs"])
        self.assertEqual(sorted(f["file"] for f in r["files"]), ["keep/b.py", "keep/c.rs"])

    def test_manifest_is_written_and_hidden_from_listings(self):
        self.w("a.py", "1")
        self.w("sub/b.md", "2")
        s3 = FakeS3()
        r = self.sweep(s3)
        self.assertTrue(r["manifest_ok"])
        mkeys = [k for (_, k) in s3.objs if k.endswith(fr.CODE_MANIFEST_NAME)]
        self.assertEqual(len(mkeys), 1)
        m = __import__("json").loads(s3.objs[("rec", mkeys[0])]["data"])
        self.assertEqual(sorted(f["path"] for f in m["files"]), ["a.py", "sub/b.md"])
        self.assertEqual(m["files"][0]["sha256"], hashlib.sha256(b"1").hexdigest())
        self.assertEqual([o["path"] for o in fr.list_code("Code Panel", destination_bucket="rec", s3=s3)],
                         ["sub/b.md", "a.py"])

    def test_every_object_has_sha256_metadata(self):
        self.w("a.py", "abc")
        s3 = FakeS3()
        self.sweep(s3)
        for (_, k), o in s3.objs.items():
            self.assertEqual(o["meta"]["sha256"], hashlib.sha256(o["data"]).hexdigest(), k)

    def test_data_listing_does_not_include_code_runs(self):
        self.w("a.py")
        s3 = FakeS3()
        fr.upload_to_recovery(b"1,2", "d.csv", panel_name="Code Panel", destination_bucket="rec", s3=s3)
        self.sweep(s3)
        rows = fr.list_recoveries("Code Panel", destination_bucket="rec", s3=s3)
        self.assertEqual([r["filename"] for r in rows], ["d.csv"])
        self.assertEqual(len(fr.list_code("Code Panel", destination_bucket="rec", s3=s3)), 1)

    def test_existing_user_and_panel_folders_are_matched_ignoring_case(self):
        s3 = FakeS3()
        s3.seed("rec", "6_Recovery/TESTER/code panel/20260101T000000Z-aaaaaaaa/d.csv", b"1")
        self.w("a.py")
        r = self.sweep(s3)
        self.assertTrue(all(k.startswith("6_Recovery/TESTER/code panel/code/") for k in self.code_keys(s3)), r)

    def test_listing_failure_falls_back_with_warning(self):
        class NoList(FakeS3):
            def get_paginator(self, name):
                raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                                  "ListObjectsV2")
        self.w("a.py")
        s3 = NoList()
        r = self.sweep(s3)
        self.assertTrue(r["ok"], r)
        self.assertIn("warning", r)
        self.assertTrue(self.code_keys(s3)[0].startswith("6_Recovery/tester/Code Panel/code/"))

    def test_bad_panel_name_fails_before_any_io(self):
        self.w("a.py")
        s3 = FakeS3()
        for bad in ("", "..", "<Panel>"):
            with self.assertRaises(ValueError, msg=bad):
                fr.code_sweep(self.d, bad, apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual(s3.objs, {})

    def test_local_files_are_never_modified_and_script_has_no_delete_calls(self):
        p = self.w("a.py", "keep me")
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        self.sweep(FakeS3())
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fathom_recovery.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(src, r"delete_object|delete_objects|\.unlink\(|shutil\.rmtree|os\.remove\(")

    def test_empty_workspace_is_fine(self):
        r = self.sweep(FakeS3())
        self.assertEqual((r["ok"], r["scanned"], r["uploaded"]), (True, 0, 0))
        self.assertNotIn("run_id", r)


class CodeRestore(unittest.TestCase):
    def setUp(self):
        import pathlib
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = pathlib.Path(self._td.name)
        self.src = self.d / "panel"
        self.out = self.d / "restored"
        self.s3 = FakeS3()

    def w(self, rel, data):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def sweep(self):
        return fr.code_sweep(self.src, "R Panel", apply=True, destination_bucket="rec", s3=self.s3)

    def restore(self, **kw):
        return fr.restore_code("R Panel", self.out, destination_bucket="rec", s3=self.s3, **kw)

    def two_runs(self):
        self.w("a.py", "a1")
        self.w("knowledge/k.md", "k1")
        r1 = self.sweep()
        self.w("a.py", "a2")
        self.w("new/n.py", "n1")
        r2 = self.sweep()
        return r1["run_id"], r2["run_id"]

    def test_latest_restores_newest_version_of_every_file_with_folders(self):
        self.two_runs()
        r = self.restore()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["restored"], 3)
        self.assertEqual((self.out / "a.py").read_text(), "a2")
        self.assertEqual((self.out / "knowledge" / "k.md").read_text(), "k1")
        self.assertEqual((self.out / "new" / "n.py").read_text(), "n1")

    def test_latest_is_right_even_when_two_runs_share_the_same_second(self):
        # same second, and the OLDER run has the larger random hex: run id order would pick the wrong one
        ids = iter(["20260101T000000Z-ffffffff", "20260101T000000Z-00000000"])
        real = fr.make_run_folder
        fr.make_run_folder = lambda now=None: next(ids)
        self.addCleanup(setattr, fr, "make_run_folder", real)
        self.w("a.py", "old")
        self.sweep()
        self.w("a.py", "new")
        self.sweep()
        self.restore()
        self.assertEqual((self.out / "a.py").read_text(), "new")

    def test_latest_still_works_if_a_manifest_is_missing(self):
        self.two_runs()
        for k in [k for (_, k) in self.s3.objs if k.endswith(fr.CODE_MANIFEST_NAME)]:
            del self.s3.objs[("rec", k)]
        self.restore()
        self.assertEqual((self.out / "knowledge" / "k.md").read_text(), "k1")
        self.assertIn((self.out / "a.py").read_text(), {"a1", "a2"})

    def test_restore_one_run_gives_exactly_that_runs_files(self):
        r1, _ = self.two_runs()
        r = self.restore(run=r1)
        self.assertEqual(sorted(f["file"] for f in r["files"]), ["a.py", "knowledge/k.md"])
        self.assertEqual((self.out / "a.py").read_text(), "a1")

    def test_unknown_run_is_an_error(self):
        self.two_runs()
        with self.assertRaises(ValueError):
            self.restore(run="20000101T000000Z-deadbeef")

    def test_dry_run_writes_nothing_locally(self):
        self.two_runs()
        r = self.restore(dry_run=True)
        self.assertEqual(r["would_restore"], 3)
        self.assertFalse(self.out.exists())

    def test_existing_local_file_is_refused_unless_overwrite(self):
        self.two_runs()
        self.out.mkdir()
        (self.out / "a.py").write_text("LOCAL")
        r = self.restore()
        self.assertEqual((r["refused_exists"], r["restored"]), (1, 2))
        self.assertEqual((self.out / "a.py").read_text(), "LOCAL")
        r = self.restore(overwrite=True)
        self.assertEqual((self.out / "a.py").read_text(), "a2")

    def test_restore_never_touches_the_live_panel_or_s3(self):
        self.two_runs()
        n = len(self.s3.objs)
        live = (self.src / "a.py").read_text()
        self.restore()
        self.assertEqual((self.src / "a.py").read_text(), live)
        self.assertEqual(len(self.s3.objs), n)

    def test_corrupted_object_is_detected(self):
        self.two_runs()
        for (b, k), o in self.s3.objs.items():
            if k.endswith("n.py"):
                o["data"] = b"tampered"
        r = self.restore()
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed"], 1)
        self.assertFalse((self.out / "new" / "n.py").exists())

    def test_path_traversal_in_a_key_cannot_escape_target_dir(self):
        self.two_runs()
        base = [k for (_, k) in self.s3.objs if k.endswith("a.py")][0]
        run_prefix = base.rsplit("/", 1)[0]
        self.s3.seed("rec", run_prefix + "/../../../../evil.py", b"x", meta={"sha256": hashlib.sha256(b"x").hexdigest()})
        r = self.restore()
        self.assertFalse((self.d / "evil.py").exists())
        self.assertFalse(r["ok"])
        self.assertTrue(any(f["action"] == "failed" and f["error"] == "unsafe path" for f in r["files"]))

    def test_nothing_backed_up_yet_restores_nothing(self):
        r = self.restore()
        self.assertEqual((r["ok"], r["restored"]), (True, 0))


class CodeCli(unittest.TestCase):
    def setUp(self):
        import pathlib
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = pathlib.Path(self._td.name)

    def cli(self, *args, cwd=None):
        import subprocess
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        env = dict(os.environ, RECOVERY_USER="tester")
        return subprocess.run([sys.executable, str(root / "fathom_recovery.py"), *args], cwd=cwd,
                              capture_output=True, text=True, env=env)

    def test_code_sweep_dry_run_via_config_from_another_folder(self):
        import json
        fr.init_panel("Cli Panel", "outputs", directory=self.d)
        (self.d / "outputs").mkdir()
        (self.d / "outputs" / "a.csv").write_text("x\n1\n")
        (self.d / "main.py").write_text("print(1)")
        (self.d / "knowledge").mkdir()
        (self.d / "knowledge" / "k.md").write_text("k")
        other = self.d / "elsewhere"
        other.mkdir()
        r = self.cli("code-sweep", "--config", str(self.d / "recovery_config.json"), cwd=other)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertFalse(out["applied"])
        # the config + instructions files are workspace files too, so they are backed up; outputs/a.csv is not
        self.assertEqual(sorted(f["file"] for f in out["files"]),
                         ["RECOVERY_INSTRUCTIONS.md", "knowledge/k.md", "main.py", "recovery_config.json"])
        self.assertFalse((self.d / ".fathom_recovery").exists())
        self.assertNotIn("mosaic-derivatives-margining", r.stdout)  # bucket stays redacted

    def test_code_sweep_needs_panel_or_config(self):
        r = self.cli("code-sweep")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs --panel-name or --config", r.stdout)

    def test_config_code_keys_are_validated_and_used(self):
        import json
        (self.d / "recovery_config.json").write_text(json.dumps(
            {"panel_name": "P", "code_patterns": ["*.py"], "code_exclude_dirs": ["skipme"]}))
        (self.d / "a.py").write_text("1")
        (self.d / "b.md").write_text("2")
        (self.d / "skipme").mkdir()
        (self.d / "skipme" / "c.py").write_text("3")
        r = self.cli("code-sweep", "--config", str(self.d / "recovery_config.json"))
        self.assertEqual([f["file"] for f in json.loads(r.stdout)["files"]], ["a.py"])
        (self.d / "recovery_config.json").write_text(json.dumps({"panel_name": "P", "code_patterns": "*.py"}))
        r = self.cli("code-sweep", "--config", str(self.d / "recovery_config.json"))
        self.assertEqual(r.returncode, 1)

    def test_instructions_cover_code_sweep_at_task_end_and_session_start(self):
        ins = fr.render_instructions("My Panel", "outputs")
        self.assertIn("code-sweep --config recovery_config.json --apply", ins)
        self.assertIn("knowledge/", ins)
        self.assertIn("run BOTH commands", ins)
        self.assertIn("/code/<timestamp>", ins)
        self.assertIn("sweep --config recovery_config.json --apply", ins)  # data rule is still there

    def test_markers(self):
        self.assertEqual(fr.__version__, "1.7.1")
        self.assertEqual(fr.LAYOUT, "user/panel")
        self.assertEqual(fr.CODE_BACKUP, "1")


class Defaults(unittest.TestCase):
    def test_target_defaults(self):
        self.assertEqual(fr.DESTINATION_BUCKET, "mosaic-derivatives-margining")
        self.assertEqual(fr.DESTINATION_PREFIX, "6_Recovery/")


if __name__ == "__main__":
    unittest.main()
