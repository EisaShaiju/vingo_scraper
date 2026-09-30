"""One-shot setup against a fresh Supabase project.

Validates credentials, creates the bucket, runs migrations, and smoke-tests a
real upload+fetch. Run after pasting new values into .env:

    .venv/Scripts/python.exe scripts/setup_supabase.py

Exits non-zero on the first thing that is wrong, with the fix, rather than
letting the app discover it later.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

OK, BAD, WARN = "[ok]  ", "[FAIL]", "[warn]"


def main() -> int:
    from vingo_scraper.config import settings

    print("\nVingo -> Supabase setup\n" + "=" * 46)

    # --- 1. config present -------------------------------------------------
    missing = [
        name for name, val in [
            ("VINGO_SUPABASE_URL", settings.supabase_url),
            ("VINGO_SUPABASE_SERVICE_KEY", settings.supabase_service_key),
            ("VINGO_DATABASE_URL", settings.database_url),
            ("VINGO_DATABASE_MIGRATION_URL", settings.database_migration_url),
        ] if not val
    ]
    if missing:
        print(f"{BAD} missing in .env: {', '.join(missing)}")
        print("      Supabase dashboard -> Connect -> Direct connection string")
        print("      Use the POOLER rows: :6543 runtime, :5432 migrations.")
        return 1
    print(f"{OK} .env has all four values")

    if "supabase.co" in settings.database_url and ":6543" not in settings.database_url:
        print(f"{WARN} runtime URL is not on :6543 (transaction pooler)")
    if settings.supabase_service_key.startswith("sb_publishable"):
        print(f"{BAD} that is the PUBLISHABLE key -- it respects RLS and uploads")
        print("      will fail. Use the sb_secret_... service-role key.")
        return 1

    # --- 2. storage --------------------------------------------------------
    try:
        from supabase import create_client

        sb = create_client(settings.supabase_url, settings.supabase_service_key)
        names = {b.name for b in sb.storage.list_buckets()}
        print(f"{OK} storage reachable; buckets: {sorted(names) or 'none'}")

        if settings.storage_bucket not in names:
            sb.storage.create_bucket(
                settings.storage_bucket,
                options={"public": True, "file_size_limit": 10485760},
            )
            print(f"{OK} created public bucket '{settings.storage_bucket}'")
        else:
            print(f"{OK} bucket '{settings.storage_bucket}' exists")
    except Exception as exc:
        print(f"{BAD} storage: {type(exc).__name__}: {str(exc)[:160]}")
        return 1

    # --- 3. migrations -----------------------------------------------------
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [str(root / ".venv/Scripts/alembic.exe"), "upgrade", "head"],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"{BAD} alembic upgrade head failed:")
        print("      " + (proc.stderr or proc.stdout).strip()[-400:])
        return 1
    print(f"{OK} migrations applied (0001, 0002)")

    # --- 4. round-trip an actual image ------------------------------------
    import hashlib

    import httpx

    from vingo_scraper.media.rehost import sniff_image, storage_key
    from vingo_scraper.media.storage import SupabaseStore

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )
    ctype, ext = sniff_image(png)
    key = storage_key("_setup", hashlib.sha256(png).hexdigest(), ext)
    url = SupabaseStore().upload(key, png, ctype)
    r = httpx.get(url, timeout=30)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        print(f"{BAD} uploaded but fetch gave {r.status_code} "
              f"({r.headers.get('content-type')}) -- is the bucket public?")
        return 1
    print(f"{OK} image round-trip 200 {r.headers['content-type']}")

    # --- 5. database -------------------------------------------------------
    import asyncio

    from sqlalchemy import func, select

    from vingo_scraper.db import models
    from vingo_scraper.db.session import ensure_compatible_event_loop, session_scope

    ensure_compatible_event_loop()

    async def counts():
        async with session_scope() as s:
            return (
                await s.scalar(select(func.count(models.Listing.id))),
                await s.scalar(select(func.count(models.ListingImage.id))),
            )

    try:
        listings, images = asyncio.run(counts())
        print(f"{OK} database reachable -- {listings} listings, {images} images")
    except Exception as exc:
        print(f"{BAD} database: {type(exc).__name__}: {str(exc)[:160]}")
        return 1

    print("=" * 46)
    print("Ready.\n")
    print("  vingo-console --port 8010     # then press Start automation")
    print("  vingo-sweep --per-cell 20     # or run one full sweep\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
