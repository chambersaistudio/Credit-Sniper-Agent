import uuid

import boto3
import pytest
from botocore.stub import Stubber

from urllib.parse import parse_qs, urlparse

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


def test_local_storage_has_no_signed_url(tmp_path):
    # Local files aren't URL-addressable; the API streams them instead.
    assert LocalStorage(str(tmp_path)).signed_url("users/u/reports/r.pdf") is None


def test_r2_signed_url_is_scoped_and_time_boxed():
    """generate_presigned_url signs locally (no network). The URL must point
    at the exact object, carry a signature and an expiry, and NOT contain the
    secret key — so it's safe to hand to the browser."""
    client = boto3.client("s3", endpoint_url="https://acct.r2.cloudflarestorage.com", region_name="auto",
                          aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="supersecret")
    storage = R2Storage("acct", "AKIAEXAMPLE", "supersecret", "reports", client=client)
    url = storage.signed_url("users/u/reports/r.pdf")
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.path.endswith("/reports/users/u/reports/r.pdf")
    assert "X-Amz-Signature" in q and "X-Amz-Credential" in q
    assert int(q["X-Amz-Expires"][0]) <= 300  # short-lived
    assert "supersecret" not in url  # the secret is never exposed in the URL
