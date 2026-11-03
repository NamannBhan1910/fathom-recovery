"""Test-only: replaces boto3.client with a disk-backed fake S3 (path in env FAKE_S3_FILE), so subprocess runs share state."""
import io
import os
import pickle

import boto3
from botocore.exceptions import ClientError

PATH = os.environ.get("FAKE_S3_FILE")


class FakeS3:
    def __init__(self):
        self.objs = pickle.load(open(PATH, "rb")) if os.path.exists(PATH) else {}

    def _save(self):
        pickle.dump(self.objs, open(PATH, "wb"))

    def put_object(self, Bucket, Key, Body, Metadata=None, **kw):
        self.objs[(Bucket, Key)] = (Body, dict(Metadata or {}))
        self._save()

    def _g(self, b, k):
        if (b, k) not in self.objs:
            raise ClientError({"Error": {"Code": "404"}}, "op")
        return self.objs[(b, k)]

    def head_object(self, Bucket, Key):
        d, m = self._g(Bucket, Key)
        return {"ContentLength": len(d), "Metadata": m}

    def get_object(self, Bucket, Key):
        d, m = self._g(Bucket, Key)
        return {"Body": io.BytesIO(d), "Metadata": m}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000):
        return {"Contents": []}

    def get_paginator(self, name):
        fs = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                ks = [(k, d) for (b, k), (d, m) in fs.objs.items() if b == Bucket and k.startswith(Prefix)]
                if Delimiter:
                    cps = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                                  for k, _ in ks if Delimiter in k[len(Prefix):]})
                    yield {"Contents": [{"Key": k, "Size": len(d)} for k, d in ks if Delimiter not in k[len(Prefix):]],
                           "CommonPrefixes": [{"Prefix": c} for c in cps]}
                else:
                    yield {"Contents": [{"Key": k, "Size": len(d)} for k, d in ks]}
        return P()


if PATH:
    boto3.client = lambda *a, **k: FakeS3()
