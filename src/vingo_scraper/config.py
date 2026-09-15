"""Central configuration.

Everything that Facebook can change out from under us -- URLs, city ids,
category slugs, rate limits -- lives here rather than being scattered through
the scrape logic. When FB changes something, this is the first file to check.
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

    # --- storage -------------------------------------------------------
    database_url: str = "postgresql+psycopg://vingo:vingo@localhost:5432/vingo_scraper"

    # --- browser -------------------------------------------------------
    headless: bool = True
    # Per-account persistent profile dirs live under here. Sessions survive
    # restarts so we are not re-authenticating (and re-triggering checkpoints)
    # on every run.
    profile_dir: Path = STATE_DIR / "profiles"
    nav_timeout_ms: int = 45_000

    # --- rate discipline ----------------------------------------------
    # Deliberately conservative. See plan: ~40-60 page loads/account/day.
    # These are the numbers that keep accounts alive; raising them is the
    # fastest way to burn the pool.
    min_delay_s: float = 3.0
    max_delay_s: float = 8.0
    max_pages_per_account_per_day: int = 50
    max_session_minutes: int = 25
    cooldown_hours: int = 8

    # --- scrape scope --------------------------------------------------
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"

    # --- images --------------------------------------------------------
    # OFF by default. Rehosting seller photos is a separate, explicitly
    # agreed decision (copyright). Launch is thumbnail + link-back.
    rehost_images: bool = False

    # --- ops -----------------------------------------------------------
    capture_dir: Path = STATE_DIR / "captures"
    log_level: str = "INFO"
    alert_webhook: str | None = None


settings = Settings()


# Marketplace location ids for the Phase 0 density probe.
# These are FB's own city identifiers used in /marketplace/<id>/search.
INDIA_CITIES: dict[str, str] = {
    "mumbai": "mumbai",
    "delhi": "delhi",
    "bengaluru": "bangalore",
    "hyderabad": "hyderabad",
    "pune": "pune",
}

# Candidate categories for Vingo's launch catalog. Slugs map to FB's
# marketplace category paths; queries are the free-text fallback when a
# category slug stops resolving.
CATEGORIES: dict[str, dict[str, str]] = {
    "mobile_phones": {"slug": "mobile-phones", "query": "mobile phone"},
    "electronics":   {"slug": "electronics",   "query": "electronics"},
    "furniture":     {"slug": "furniture",     "query": "furniture"},
    "vehicles":      {"slug": "vehicles",      "query": "bike"},
    "appliances":    {"slug": "appliances",    "query": "appliance"},
}

BASE_URL = "https://www.facebook.com"
MARKETPLACE_SEARCH = BASE_URL + "/marketplace/{city}/search"
MARKETPLACE_CATEGORY = BASE_URL + "/marketplace/{city}/{slug}"

# Substrings that mean "we are not logged in / we got challenged".
# Checked against the landed URL after navigation.
LOGIN_WALL_MARKERS = ("/login", "/checkpoint", "/recover", "login.php")
