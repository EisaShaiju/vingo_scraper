"""Supabase Storage adapter.

Kept behind a tiny interface so the rest of the pipeline never imports the
Supabase SDK directly -- swapping to S3/R2 later means replacing this file.

The three quirks handled here all fail *silently* if you get them wrong; see
docs/supabase.md.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from vingo_scraper.config import settings

log = structlog.get_logger(__name__)


class StorageError(RuntimeError):
    pass


class ObjectStore(Protocol):
    """What the media pipeline needs. Deliberately two methods."""

    def upload(self, key: str, data: bytes, content_type: str) -> str: ...
    def public_url(self, key: str) -> str: ...


class SupabaseStore:
    """Uploads to a Supabase Storage bucket and returns permanent public URLs."""

    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        self.url = (url or settings.supabase_url).rstrip("/")
        self.service_key = service_key or settings.supabase_service_key
        self.bucket = bucket or settings.storage_bucket
        if not self.url or not self.service_key:
            raise StorageError(
                "VINGO_SUPABASE_URL / VINGO_SUPABASE_SERVICE_KEY not set "
                "-- copy .env.example to .env"
            )
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from supabase import create_client

            # Service-role key: bypasses RLS. Server-side only -- this must
            # never be reachable from a client bundle.
            self._client = create_client(self.url, self.service_key)
        return self._client

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        """Upload bytes and return the permanent public URL."""
        try:
            self.client.storage.from_(self.bucket).upload(
                path=key,
                file=data,
                file_options={
                    # Without this Supabase stores the object as text/html and
                    # browsers refuse to render it. Nothing errors at upload
                    # time, so the failure only shows up in the UI.
                    "content-type": content_type,
                    # Images are content-addressed, so they never change: cache
                    # them for a year.
                    "cache-control": "31536000",
                    # Must be the STRING "true". A Python bool is silently
                    # ignored and re-uploads 409 instead of replacing.
                    "upsert": "true",
                },
            )
        except Exception as exc:
            raise StorageError(f"upload failed for {key}: {exc}") from exc

        return self.public_url(key)

    def public_url(self, key: str) -> str:
        """Construct the public URL.

        Built rather than fetched: the format is stable and deterministic, and
        one fewer round trip per image matters at catalog scale.
        """
        return f"{self.url}/storage/v1/object/public/{self.bucket}/{key}"


class InMemoryStore:
    """Test double. Records uploads instead of performing them."""

    def __init__(self, base: str = "https://test.storage/public") -> None:
        self.base = base
        self.objects: dict[str, tuple[bytes, str]] = {}

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = (data, content_type)
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self.base}/{key}"

    def download(self, key: str) -> tuple[bytes, str]:
        data, content_type = self.objects[key]
        return data, content_type


class GCSStore:
    """Google Cloud Storage, authenticated by Application Default Credentials.

    No key file. The VM's attached service account supplies credentials, which
    means there is no secret to rotate, leak, or accidentally commit. If an
    upload 403s, the fix is an IAM binding on the bucket -- never a key.

    The client is built lazily and cached: constructing it reads credentials
    and costs a round trip, which is wasted work in any process that never
    rehosts an image (CLI dry runs, migrations, the whole test suite).
    """

    def __init__(self, bucket: str | None = None, public_base: str | None = None):
        self.bucket_name = bucket or settings.gcs_bucket
        self.public_base = (public_base or settings.gcs_public_base).rstrip("/")
        self._bucket = None

    @property
    def bucket(self):
        if self._bucket is None:
            try:
                from google.cloud import storage as gcs
            except ImportError as exc:  # pragma: no cover - deployment issue
                raise StorageError(
                    "google-cloud-storage is not installed; "
                    "pip install -e . on the VM"
                ) from exc
            try:
                self._bucket = gcs.Client().bucket(self.bucket_name)
            except Exception as exc:
                raise StorageError(
                    f"could not reach gs://{self.bucket_name}: {exc}. "
                    "Check the VM service account has roles/storage.objectAdmin "
                    "(`gcloud storage ls gs://...` reproduces it)."
                ) from exc
        return self._bucket

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        blob = self.bucket.blob(key)
        try:
            # Content-addressed keys mean the same bytes always land on the
            # same path, so re-uploading is harmless and idempotent.
            blob.upload_from_string(data, content_type=content_type)
        except Exception as exc:
            raise StorageError(f"upload of {key} failed: {exc}") from exc
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        """Built, not fetched -- the format is stable and deterministic, and
        one fewer round trip per image matters at catalog scale.

        NOTE: this URL only resolves for a browser if the bucket grants public
        read. Ours does not, and the org policy may forbid it, so the console
        renders images through `/api/image/...` instead -- see `download`.
        """
        if self.public_base:
            return f"{self.public_base}/{key}"
        return f"https://storage.googleapis.com/{self.bucket_name}/{key}"

    def download(self, key: str) -> tuple[bytes, str]:
        """(bytes, content_type). Used by the image proxy.

        The app can read the bucket with its own credentials even when the
        objects are private, which is what lets the catalog show photos
        without making the bucket world-readable.
        """
        blob = self.bucket.blob(key)
        try:
            data = blob.download_as_bytes()
        except Exception as exc:
            raise StorageError(f"download of {key} failed: {exc}") from exc
        return data, (blob.content_type or "image/jpeg")


def get_store() -> ObjectStore:
    """The one place a storage backend is chosen.

    `rehost_all()` and every caller stay backend-agnostic, which is what made
    swapping Supabase for GCS a configuration change rather than a rewrite.
    """
    backend = (settings.storage_backend or "gcs").lower()
    if backend == "gcs":
        return GCSStore()
    if backend == "memory":
        return InMemoryStore()
    if backend == "supabase":
        return SupabaseStore()
    raise StorageError(
        f"unknown VINGO_STORAGE_BACKEND={backend!r}; use gcs, memory or supabase"
    )
