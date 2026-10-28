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
        self.assertRegex(k, r"^6_Recovery/Daily Checklist Brokerage/test-user/20261027T144507Z-[0-9a-f]{8}/report\.csv$")

    def test_unique_per_call(self):
        a = fr.build_destination_key("x.csv", "P", user="u")
        b = fr.build_destination_key("x.csv", "P", user="u")
        self.assertNotEqual(a, b)

    def test_panels_get_separate_folders(self):
        a = fr.build_destination_key("checklist_state.json", "Panel A", user="u")
        b = fr.build_destination_key("checklist_state.json", "Panel B", user="u")
        self.assertTrue(a.startswith("6_Recovery/Panel A/"))
        self.assertTrue(b.startswith("6_Recovery/Panel B/"))

    def test_name_is_exact(self):
        a = fr.build_destination_key("x.csv", "Daily Checklist Equities", user="u")
        b = fr.build_destination_key("x.csv", "daily checklist equities", user="u")
        self.assertNotEqual(a.split("/")[1], b.split("/")[1])

    def test_slashes_cannot_escape(self):
        k = fr.build_destination_key("x.csv", "../Evil/Panel", user="u")
        self.assertNotIn("..", k)
        self.assertEqual(len(k.split("/")), 5)  # prefix/panel/user/run/file

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
        self.assertTrue(cr["recovery_path"].startswith("Equities Checklist EU/tester/"))
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
        s.seed("rec", "6_Recovery/Panel A/u1/20261027T100000Z-aaaaaaaa/f.csv", b"v1", meta={"sha256": hashlib.sha256(b"v1").hexdigest()})
        s.seed("rec", "6_Recovery/Panel A/u2/20261027T110000Z-bbbbbbbb/f.csv", b"v2")
        s.seed("rec", "6_Recovery/Panel A/legacy.csv", b"old")
        s.seed("rec", "6_Recovery/Panel A/", b"")  # folder marker
        s.seed("rec", "6_Recovery/Panel B/u1/20261027T120000Z-cccccccc/f.csv", b"other")

    def test_list_scopes_to_panel(self):
        rows = fr.list_recoveries("Panel A", destination_bucket="rec", s3=self.s3)
        self.assertEqual(len(rows), 3)  # marker skipped, Panel B excluded
        self.assertTrue(all(r["recovery_path"].startswith("Panel A/") for r in rows))

    def test_list_user_filter_and_parsing(self):
        rows = fr.list_recoveries("Panel A", user="u1", destination_bucket="rec", s3=self.s3)
        self.assertEqual([(r["user"], r["run_id"], r["filename"]) for r in rows],
                         [("u1", "20261027T100000Z-aaaaaaaa", "f.csv")])
        legacy = [r for r in fr.list_recoveries("Panel A", destination_bucket="rec", s3=self.s3) if r["filename"] == "legacy.csv"][0]
        self.assertIsNone(legacy["run_id"])

    def test_restore_verifies_hash(self):
        p = "Panel A/u1/20261027T100000Z-aaaaaaaa/f.csv"
        r = fr.restore_from_recovery(p, "live", "panel/f.csv", destination_bucket="rec", s3=self.s3)
        self.assertTrue(r["ok"])
        self.assertTrue(r["sha256_verified"])
        self.assertEqual(self.s3.objs[("live", "panel/f.csv")]["data"], b"v1")

    def test_restore_refuses_overwrite(self):
        self.s3.seed("live", "panel/f.csv", b"current")
        p = "Panel A/u1/20261027T100000Z-aaaaaaaa/f.csv"
        r = fr.restore_from_recovery(p, "live", "panel/f.csv", destination_bucket="rec", s3=self.s3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "refused")
        self.assertEqual(self.s3.objs[("live", "panel/f.csv")]["data"], b"current")
        r2 = fr.restore_from_recovery(p, "live", "panel/f.csv", overwrite=True, destination_bucket="rec", s3=self.s3)
        self.assertTrue(r2["ok"])

    def test_restore_dry_run_and_bad_path(self):
        p = "Panel A/u1/20261027T100000Z-aaaaaaaa/f.csv"
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
        self.assertRegex(r["recovery_path"], r"^Panel X/tester/\d{8}T\d{6}Z-[0-9a-f]{8}/r\.csv$")
        (bucket, key), obj = next(iter(s3.objs.items()))
        self.assertEqual(bucket, "rec")
        self.assertTrue(key.startswith("6_Recovery/Panel X/tester/"))
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
    EXISTING = "6_Recovery/Daily Checklist Equities/u/20261027T100000Z-aaaaaaaa/f.csv"

    def fs(self):
        s3 = FakeS3()
        s3.seed("rec", self.EXISTING, b"old")
        s3.seed("rec", "6_Recovery/Brokerage Recon/", b"")  # folder marker only
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
        s3.seed("rec", "6_Recovery/daily checklist equities/u/r/f.csv", b"x")
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
        self.assertTrue(r["recovery_path"].startswith("Daily Checklist Equities/"))
        self.assertEqual(r["panel_folder"], "Daily Checklist Equities")
        self.assertFalse(any(k.startswith("6_Recovery/daily checklist equities/") for (_, k) in s3.objs))

    def test_dry_run_reports_matched_folder_and_writes_nothing(self):
        s3 = self.fs()
        n = len(s3.objs)
        r = fr.upload_to_recovery(b"x", "x.csv", panel_name="BROKERAGE RECON", dry_run=True,
                                  destination_bucket="rec", s3=s3)
        self.assertTrue(r["recovery_path"].startswith("Brokerage Recon/"))
        self.assertEqual(len(s3.objs), n)

    def test_list_and_copy_use_matched_folder(self):
        s3 = self.fs()
        rows = fr.list_recoveries("DAILY checklist EQUITIES", destination_bucket="rec", s3=s3)
        self.assertEqual([r["filename"] for r in rows], ["f.csv"])
        s3.seed("src", "k/y.csv", b"zz")
        c = fr.copy_created_object_to_recovery("src", "k/y.csv", panel_name="daily checklist equities",
                                               destination_bucket="rec", s3=s3)
        self.assertTrue(c["recovery_path"].startswith("Daily Checklist Equities/"))

    def test_sweep_uses_matched_folder(self):
        import pathlib
        import tempfile
        s3 = self.fs()
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "a.csv").write_bytes(b"1")
            (pathlib.Path(d) / "b.csv").write_bytes(b"2")
            r = fr.sweep(d, "daily checklist equities", apply=True, destination_bucket="rec", s3=s3)
        self.assertEqual(r["uploaded"], 2)
        self.assertTrue(all(f["recovery_path"].startswith("Daily Checklist Equities/")
                            for f in r["files"] if f["action"] == "uploaded"))

    def test_selfcheck_probe_uses_matched_folder(self):
        s3 = self.fs()
        r = fr.selfcheck("daily checklist equities", apply=True, destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"], r)
        probes = [k for (_, k) in s3.objs if k.endswith("selfcheck_probe.txt")]
        self.assertEqual(len(probes), 1)
        self.assertTrue(probes[0].startswith("6_Recovery/Daily Checklist Equities/"))

    def test_listing_failure_falls_back_with_warning(self):
        class NoList(FakeS3):
            def get_paginator(self, name):
                raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                                  "ListObjectsV2")
        s3 = NoList()
        r = fr.upload_to_recovery(b"x", "x.csv", panel_name="My Panel", destination_bucket="rec", s3=s3)
        self.assertTrue(r["ok"])
        self.assertTrue(r["recovery_path"].startswith("My Panel/"))
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




class Defaults(unittest.TestCase):
    def test_target_defaults(self):
        self.assertEqual(fr.DESTINATION_BUCKET, "mosaic-derivatives-margining")
        self.assertEqual(fr.DESTINATION_PREFIX, "6_Recovery/")


if __name__ == "__main__":
    unittest.main()
