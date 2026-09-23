"""
Private document storage: report PDFs, approved dispute letters, and (later)
bureau responses and evidence the consumer uploads.

Two backends behind one interface, chosen by STORAGE_BACKEND:
  local — a directory (development, or a mounted volume)
  r2    — a private Cloudflare R2 bucket via its S3-compatible API

Nothing is ever public: there are no public URLs, only server-side reads.
Keys are namespaced per user so deleting a user's data is a prefix delete.
"""
import asyncio
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import settings


class StorageError(RuntimeError):
    pass


def report_key(user_id: uuid.UUID, report_id: uuid.UUID) -> str:
    return f"users/{user_id}/reports/{report_id}.pdf"


def approved_package_key(user_id: uuid.UUID, case_id: uuid.UUID, stamp: str) -> str:
    return f"users/{user_id}/cases/{case_id}/approved-package-{stamp}.pdf"


def response_key(user_id: uuid.UUID, case_id: uuid.UUID, document_id: uuid.UUID, ext: str) -> str:
    return f"users/{user_id}/cases/{case_id}/responses/{document_id}.{ext}"


def evidence_key(user_id: uuid.UUID, claim_id: uuid.UUID, document_id: uuid.UUID, ext: str) -> str:
    return f"users/{user_id}/evidence/{claim_id}/{document_id}.{ext}"


# How long a presigned download URL stays valid. Short: it is minted per
# authorized request, used immediately by the browser, and then useless.
SIGNED_URL_TTL_SECONDS = 120


class Storage(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...

    def signed_url(self, key: str) -> str | None:
        """A short-lived, credential-free URL the browser can fetch directly,
        or None if this backend can't sign one (the caller then streams the
        bytes through the API instead)."""
        ...


class LocalStorage:
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError(f"Invalid storage key: {key!r}")
        return path

    async def put(self, key, data, content_type):
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def get(self, key):
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError as e:
            raise StorageError(f"Not found: {key}") from e

    async def delete(self, key):
        await asyncio.to_thread(self._path(key).unlink, True)

    def signed_url(self, key: str) -> str | None:
        # Local files aren't reachable by URL; the API streams them instead.
        return None


class R2Storage:
    """Cloudflare R2 through its S3-compatible endpoint. The bucket must be
    private (R2's default); objects are encrypted at rest by R2."""

    name = "r2"

    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str, client=None):
        if not all((account_id, access_key_id, secret_access_key, bucket)):
            raise StorageError(
                "STORAGE_BACKEND=r2 needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET"
            )
        self.bucket = bucket
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
            )
        self._client = client

    async def put(self, key, data, content_type):
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key):
        try:
            response = await asyncio.to_thread(self._client.get_object, Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey as e:
            raise StorageError(f"Not found: {key}") from e
        return await asyncio.to_thread(response["Body"].read)

    async def delete(self, key):
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    def signed_url(self, key: str) -> str | None:
        # Presigned GET against the private bucket. The URL carries a
        # time-boxed signature, not the R2 credentials, so it is safe to hand
        # to the browser and expires on its own.
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=SIGNED_URL_TTL_SECONDS,
        )


@lru_cache
def get_storage() -> Storage:
    if settings.storage_backend == "r2":
        return R2Storage(settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket)
    if settings.storage_backend == "local":
        return LocalStorage(settings.upload_dir)
    raise StorageError(f"Unknown STORAGE_BACKEND: {settings.storage_backend!r}")
