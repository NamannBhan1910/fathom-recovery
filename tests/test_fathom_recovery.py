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
            def paginate(self, Bucket, Prefix):
                items = [{"Key": k, "Size": len(o["data"]), "LastModified": o["lm"]}
                         for (b, k), o in fs.objs.items() if b == Bucket and k.startswith(Prefix)]
                yield {"Contents": items}
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
            fr.copy_created_object_to_recovery("src", "nope.csv", panel_name="P",
                                               destination_bucket="rec", s3=FakeS3())

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


class Defaults(unittest.TestCase):
    def test_target_defaults(self):
        self.assertEqual(fr.DESTINATION_PREFIX, "6_Recovery/")

    def test_missing_destination_fails_before_source_write(self):
        s3 = FakeS3()
        with self.assertRaisesRegex(ValueError, "RECOVERY_DESTINATION_BUCKET"):
            fr.put_object_and_copy_to_recovery("source", "new.csv", b"data",
                                               panel_name="P", destination_bucket="", s3=s3)
        self.assertEqual(s3.objs, {})

    def test_missing_destination_rejected_by_read_commands(self):
        s3 = FakeS3()
        with self.assertRaisesRegex(ValueError, "RECOVERY_DESTINATION_BUCKET"):
            fr.list_recoveries("P", destination_bucket="", s3=s3)
        self.assertFalse(fr.selfcheck("P", destination_bucket="", s3=s3)["ok"])


if __name__ == "__main__":
    unittest.main()
