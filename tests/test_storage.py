import uuid

import boto3
import pytest
from botocore.stub import Stubber

from app.services.storage import LocalStorage, R2Storage, StorageError, report_key


async def test_local_roundtrip(tmp_path):
    storage = LocalStorage(str(tmp_path))
    key = report_key(uuid.uuid4(), uuid.uuid4())
    await storage.put(key, b"%PDF-1.4 test", "application/pdf")
    assert await storage.get(key) == b"%PDF-1.4 test"
    await storage.delete(key)
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_local_rejects_path_traversal(tmp_path):
    with pytest.raises(StorageError):
        await LocalStorage(str(tmp_path)).put("../../etc/passwd", b"x", "text/plain")


def test_r2_requires_full_configuration():
    with pytest.raises(StorageError, match="R2_BUCKET"):
        R2Storage("acct", "key", "secret", "")


async def test_r2_uses_private_bucket_operations():
    client = boto3.client("s3", endpoint_url="https://acct.r2.cloudflarestorage.com", region_name="auto",
                          aws_access_key_id="k", aws_secret_access_key="s")
    storage = R2Storage("acct", "k", "s", "reports", client=client)
    with Stubber(client) as stub:
        stub.add_response("put_object", {}, {"Bucket": "reports", "Key": "users/u/reports/r.pdf",
                                             "Body": b"%PDF", "ContentType": "application/pdf"})
        await storage.put("users/u/reports/r.pdf", b"%PDF", "application/pdf")
        stub.assert_no_pending_responses()
