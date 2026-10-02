"""Central configuration.

Everything that can change out from under us -- Actor id, bucket names, city
slugs, category slugs -- lives here rather than being scattered through the
pipeline. When an ingest breaks, this is the first file to check.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / ".state"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VINGO_", extra="ignore"
    )

    # --- Apify ---------------------------------------------------------
    apify_api_token: str = ""
    # curious_coder: ~8x cheaper ($0.0015 vs $0.0124/listing) and returns
    # more -- all photos, attributes, and seller identity where Facebook
    # exposes it. Verified on 47 real Bengaluru listings.
    apify_actor_id: str = "curious_coder/facebook-marketplace"
    # Hard ceiling per run, independent of what a caller asks for. Apify bills
    # per result, so a typo in max_items is a billing incident -- this caps it.
    apify_max_items_hard_cap: int = 2_000
    # Second, independent guard: Apify enforces this server-side, so even a
    # runaway Actor cannot spend past it.
    apify_max_charge_usd: float = 1.00
    apify_timeout_s: int = 900

    # Volume on curious_coder is bought by the PAGE (~24 listings, ~$0.012).
    # Asking for fewer bills the whole page anyway and discards the rest, so
    # taking the full page is strictly more data for the same money --
    # *if* there is time to ingest it.
    #
    # There is not, on Vercel: `maxDuration` is 60s on the Hobby plan and a
    # full page measured 60.5s, so the function was cut off mid-ingest.
    # Default OFF for that reason. Turn it ON wherever there is no function
    # timeout -- the VM, or Vercel Pro's 300s -- and each tick keeps roughly
    # three times the listings at identical cost.
    apify_accept_full_page: bool = False
    # Ceiling on TOTAL Apify spend per UTC day, checked against actual
    # recorded spend in `scrape_run` before each cell. This is the guard
    # that makes unattended scheduling safe on a per-result-billed API.
    # HARD ceiling on total Apify spend, checked against Apify's own monthly
    # usage figure. Account hard limit is $9; capping at $7 leaves a buffer
    # so Apify's own wall is never hit mid-run (which fails the run rather
    # than declining it cleanly).
    total_spend_cap_usd: float = 7.00
    daily_spend_cap_usd: float = 2.00

    # --- Postgres --------------------------------------------------------
    # ONE url on the VM. The old Supabase deployment needed two -- pooler
    # :6543 for runtime (no prepared statements) and :5432 for Alembic --
    # because Supavisor cannot handle prepared statements. Local Postgres has
    # no pooler, so `database_migration_url` is only read if explicitly set.
    database_url: str = ""
    database_migration_url: str = ""

    # --- object storage --------------------------------------------------
    # "gcs" on the VM, "memory" in tests. Selected in one place
    # (`media.storage.get_store`) so the rehost pipeline never branches.
    storage_backend: str = "gcs"

    # Google Cloud Storage. Authentication is Application Default Credentials
    # from the VM's attached service account -- there is no key file, and
    # there should never be one.
    gcs_bucket: str = "vingo-comet-media-as1"
    # Set when the bucket serves through a CDN or custom domain; otherwise the
    # standard storage.googleapis.com path is built.
    gcs_public_base: str = ""

    # Legacy Supabase storage. Retained only so an old deployment can still
    # read; nothing new is written here.
    supabase_url: str = ""
    supabase_service_key: str = ""
    storage_bucket: str = "listing-images"

    # --- media pipeline ------------------------------------------------
    max_image_bytes: int = 10 * 1024 * 1024
    image_download_timeout_s: int = 30
    image_concurrency: int = 8

    # --- scrape scope --------------------------------------------------
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"

    # Lock files, run state. Overridable so a Linux deploy can point at
    # /var/lib/vingo instead of the repo.
    state_dir: Path = STATE_DIR

    # Shared secret guarding the demo control and cron endpoints. These
    # start and stop real spending, so they are never left open.
    demo_secret: str = ""

    # --- ops -----------------------------------------------------------
    log_level: str = "INFO"
    alert_webhook: str | None = None


settings = Settings()


# Marketplace location slugs used in /marketplace/<slug>/...
INDIA_CITIES: dict[str, str] = {
    "mumbai": "mumbai",
    "delhi": "delhi",
    "bengaluru": "bangalore",
    "hyderabad": "hyderabad",
    "pune": "pune",
}

# Candidate categories for Vingo's launch catalog. `slug` is the Marketplace
# category path; `query` is the free-text search fallback.
CATEGORIES: dict[str, dict[str, str]] = {
    "mobile_phones": {"slug": "mobile-phones", "query": "mobile phone"},
    "electronics": {"slug": "electronics", "query": "electronics"},
    "furniture": {"slug": "furniture", "query": "furniture"},
    "vehicles": {"slug": "vehicles", "query": "bike"},
    "appliances": {"slug": "appliances", "query": "appliance"},
}
