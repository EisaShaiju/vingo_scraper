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
