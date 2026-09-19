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
    apify_actor_id: str = "apify/facebook-marketplace-scraper"
    # Hard ceiling per run, independent of what a caller asks for. Apify bills
    # per result, so a typo in max_items is a billing incident -- this caps it.
    apify_max_items_hard_cap: int = 2_000
    # Second, independent guard: Apify enforces this server-side, so even a
    # runaway Actor cannot spend past it.
    apify_max_charge_usd: float = 25.0
    apify_timeout_s: int = 900

    # --- Supabase Postgres ---------------------------------------------
    # TWO urls on purpose, and they are not interchangeable:
    #   runtime   -> transaction pooler :6543 (IPv4, NO prepared statements)
    #   migrations-> session/direct     :5432 (IPv6-only without the add-on)
    # See docs/supabase.md before changing either.
    database_url: str = ""
    database_migration_url: str = ""

    # --- Supabase Storage ----------------------------------------------
    supabase_url: str = ""
    # Service-role key: bypasses RLS. Server-side only, never in a client
    # bundle. Lives in .env, which is gitignored.
    supabase_service_key: str = ""
    storage_bucket: str = "listing-images"

    # --- media pipeline ------------------------------------------------
    max_image_bytes: int = 10 * 1024 * 1024
    image_download_timeout_s: int = 30
    image_concurrency: int = 8

    # --- scrape scope --------------------------------------------------
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"

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
