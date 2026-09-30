"""Download Facebook CDN images and rehost them to our own storage.

Why this exists: fbcdn URLs are signed with `oh=` (hash) and `oe=` (hex expiry)
parameters and die within hours-to-days. A listing stored with an fbcdn URL
looks perfectly healthy at write time and shows a broken image to a buyer a
week later. Rehosting is what makes a listing durable.

Two design rules that come from that failure mode:

- **Rehost during ingestion, never as a later batch.** A nightly sweep would
  find the URLs already dead.
- **A failed image must not fail its listing.** Status is tracked per image so
  one dead photo costs one photo.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from vingo_scraper.config import settings
from vingo_scraper.extract.schema import ImageStatus, Listing, ListingImage
from vingo_scraper.media.storage import ObjectStore, StorageError

log = structlog.get_logger(__name__)

# Magic-byte signatures. We identify images from their actual bytes rather
# than the URL extension or the served Content-Type header, because we are
# writing to public storage and neither of those is trustworthy -- an expired
# fbcdn URL commonly returns an HTML error page with a 200.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
)


class NotAnImageError(ValueError):
    """Downloaded bytes are not a recognised image format."""


def sniff_image(data: bytes) -> tuple[str, str]:
    """Return (content_type, extension) or raise NotAnImageError."""
    for magic, content_type, ext in _SIGNATURES:
        if data.startswith(magic):
            return content_type, ext
    # RIFF....WEBP -- the format marker sits at offset 8, not 0.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise NotAnImageError(f"unrecognised image header: {data[:16]!r}")


def storage_key(fb_listing_id: str, sha256: str, ext: str) -> str:
    """Content-addressed path.

    Because the hash is in the path, re-ingesting a listing overwrites the
    byte-identical object instead of creating a second copy. That is what makes
    the whole pipeline idempotent.
    """
    return f"listings/{fb_listing_id}/{sha256[:16]}.{ext}"


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def download(client: httpx.AsyncClient, url: str, *, max_bytes: int) -> bytes:
    """Fetch an image, refusing anything oversized.

    Streamed with a running size check so a mis-sized or malicious response
    cannot exhaust memory -- we stop reading rather than downloading it all and
    checking afterwards.
    """
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"image exceeds {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


async def rehost_image(
    image: ListingImage,
    fb_listing_id: str,
    *,
    client: httpx.AsyncClient,
    store: ObjectStore,
    seen: dict[str, str] | None = None,
) -> ListingImage:
    """Download, verify, and upload one image. Mutates and returns it.

    Never raises: every failure is recorded on the image so the listing can
    still be persisted.
    """
    try:
        data = await download(
            client, image.source_url, max_bytes=settings.max_image_bytes
        )
    except Exception as exc:
        image.status = ImageStatus.FAILED
        image.error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("media.download_failed", url=image.source_url[:80],
                    error=image.error)
        return image

    try:
        content_type, ext = sniff_image(data)
    except NotAnImageError as exc:
        # Distinct from FAILED: we got bytes, they just are not an image.
        # Usually an expired URL returning an HTML error page with a 200.
        image.status = ImageStatus.REJECTED
        image.error = str(exc)[:500]
        log.warning("media.not_an_image", url=image.source_url[:80])
        return image

    digest = hashlib.sha256(data).hexdigest()
    image.sha256 = digest
    image.content_type = content_type
    image.size_bytes = len(data)

    # Same bytes already uploaded in this batch -- reposted photos are common.
    if seen is not None and digest in seen:
        image.storage_url = seen[digest]
        image.status = ImageStatus.STORED
        return image

    key = storage_key(fb_listing_id, digest, ext)
    try:
        image.storage_url = store.upload(key, data, content_type)
        image.status = ImageStatus.STORED
        if seen is not None:
            seen[digest] = image.storage_url
    except StorageError as exc:
        image.status = ImageStatus.FAILED
        image.error = str(exc)[:500]
        log.error("media.upload_failed", key=key, error=image.error)

    return image


async def rehost_listing_images(
    listing: Listing,
    *,
    client: httpx.AsyncClient,
    store: ObjectStore,
    semaphore: asyncio.Semaphore,
    seen: dict[str, str] | None = None,
) -> Listing:
    """Rehost every image on one listing, bounded by the semaphore."""

    async def _one(img: ListingImage) -> ListingImage:
        async with semaphore:
            return await rehost_image(
                img, listing.fb_listing_id, client=client, store=store, seen=seen
            )

    if listing.images:
        listing.images = list(
            await asyncio.gather(*(_one(img) for img in listing.images))
        )
    return listing


async def rehost_all(
    listings: list[Listing], *, store: ObjectStore
) -> tuple[list[Listing], int, int]:
    """Rehost images across many listings. Returns (listings, stored, failed)."""
    semaphore = asyncio.Semaphore(settings.image_concurrency)
    # Shared across the batch so a photo reposted under several listings is
    # downloaded and uploaded once.
    seen: dict[str, str] = {}

    timeout = httpx.Timeout(settings.image_download_timeout_s)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; VingoIngest/1.0)"},
    ) as client:
        await asyncio.gather(
            *(
                rehost_listing_images(
                    listing, client=client, store=store,
                    semaphore=semaphore, seen=seen,
                )
                for listing in listings
            )
        )

    stored = sum(
        1 for x in listings for img in x.images if img.status is ImageStatus.STORED
    )
    failed = sum(
        1
        for x in listings
        for img in x.images
        if img.status in (ImageStatus.FAILED, ImageStatus.REJECTED)
    )
    log.info("media.rehost_done", listings=len(listings), stored=stored, failed=failed)
    return listings, stored, failed
