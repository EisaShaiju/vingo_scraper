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
    # The Actor STOPS when its own event accounting reaches this, reporting
    # "Stopping the actor because we reached the max total charge of 1 USD" --
    # observed after only 25 listing-items, so its per-event accounting is far
    # coarser than the store page's "$0.50 / 1,000". LOWERING this truncates
    # runs sooner; raising it buys more listings per run. A local .env or
    # /etc/comet.env override wins over this default.
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

    # How many Marketplace pages one run may ask for.
    #
    # MEASURED, not assumed. On the FREE Apify plan the Actor answers every
    # request with roughly one page whatever we ask for:
    #
    #     pages asked -> items returned
    #        1              25
    #        2              21, 21, 16
    #        3              26, 28, 21, 26
    #
    # and it says so: "Number of records scraped per run by this actor is
    # limited to 10 for free users" (the stated 10 does not match the observed
    # 16-28, but the ceiling is real and sits near one page). Cost tracks ITEMS
    # returned, not pages requested, so asking for three is not wasteful --
    # it is simply a promise the UI cannot keep.
    #
    # Raise to 3 on a paid plan and the selector grows back on its own.
    apify_max_pages: int = 3

    # Actor-side dedup. The Actor keeps a key-value store of listing ids it
    # has already returned and skips them on the next run -- which is the ONLY
    # mechanism that avoids the CHARGE. Our own `fb_listing_id` check runs
    # after the money is spent (Apify bills when it scrapes, before any id
    # reaches us), so it saves image bandwidth and never saved a cent.
    #
    # Measured: a repeat query with this off cost $0.01205 and returned 24
    # listings we already held.
    #
    # DEFAULT OFF, and that is deliberate now that manual is the only runner.
    #
    # The cache made sense for a scheduled sweep hitting the same five
    # categories every day. For a person typing "xbox" it is actively wrong:
    # the Actor replies "already gave you those", returns nothing, charges
    # nothing, and the console looks broken. Observed exactly that -- repeated
    # keyword searches returning 0 listings for $0.0000.
    #
    # The database check (`_split_known`) still prevents re-downloading images
    # for listings we hold, so a repeat search is cheap in bandwidth. It costs
    # one Apify page, which is the price of actually seeing results.
    #
    # Set a store id to re-enable it if a scheduled runner comes back.
    apify_cache_store_id: str = ""

    # --- Polaris handoff -------------------------------------------------
    # Where newly stored listings are POSTed so Polaris can build them into
    # Vingo listings. EMPTY MEANS OFF, deliberately: there is no live endpoint
    # yet, and a sweep firing forty-odd unexpected POSTs at whatever a
    # half-configured URL points at is worse than firing none.
    polaris_webhook_url: str = ""
    polaris_webhook_token: str = ""
    # On by default even once a URL is set. Logs the exact payload without
    # sending it -- which is what gets shown to whoever builds the receiver to
    # agree the shape before anything real is delivered. Turn OFF to send.
    polaris_dry_run: bool = True
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
# Facebook's real Marketplace categories, verified by walking every entry in
# the Bengaluru sidebar by hand on 2026-10-07 and reading the URL it produced.
#
# THE CATEGORY IS A NUMERIC `category_id` IN A SEARCH URL, NOT A PATH SLUG.
# Sixteen of seventeen resolve to
#     /marketplace/<loc>/search/?category_id=<id>&query=<name>
# Only `property_rent` has a working path form. Every hyphenated slug this file
# used to carry -- musical-instruments, home-goods, sporting-goods and the rest
# -- was invented, and a wrong path does NOT 404: Facebook serves the generic
# Marketplace feed, which is why a search for Musical instruments returned IBC
# totes, a townhouse and a Rs 9,00,000 Volvo.
#
# `query` is the exact wording Facebook pairs with each id, used as the search
# term and as the fallback for any entry without an id.
#
# `slug` is LEGACY ONLY -- the non-curious_coder branch still builds a path
# from it (`apify/urls.py::category_url`). It is not used by the primary Actor
# and should not be trusted as a real Facebook path.
#
# Do not edit an id by hand. They came from a manual pass that should not have
# to be repeated, and `tests/test_categories.py` pins every one.
CATEGORIES: dict[str, dict[str, str]] = {
    "vehicles": {
        "category_id": "546583916084032",
        "query": "Vehicles", "slug": "vehicles"},
    "classifieds": {
        "category_id": "759201041254543",
        "query": "Classifieds", "slug": "classifieds"},
    "clothing": {
        "category_id": "677457442746983",
        "query": "Clothing", "slug": "apparel"},
    "electronics": {
        "category_id": "479353692612078",
        "query": "Electronics", "slug": "electronics"},
    "entertainment": {
        "category_id": "529595287638583",
        "query": "Entertainment", "slug": "entertainment"},
    "family": {
        "category_id": "891748581240437",
        "query": "Family", "slug": "family"},
    "garden_outdoor": {
        "category_id": "1109541402725436",
        "query": "Garden and outdoors", "slug": "garden"},
    "hobbies": {
        "category_id": "459026188375950",
        "query": "Hobbies", "slug": "hobbies"},
    "home_goods": {
        "category_id": "753380185098614",
        "query": "Home goods", "slug": "home-goods"},
    "home_improvement": {
        "category_id": "476291743038859",
        "query": "Home improvement supplies", "slug": "home-improvement"},
    "musical_instruments": {
        "category_id": "1078592699170502",
        "query": "Musical instruments", "slug": "musical-instruments"},
    "office_supplies": {
        "category_id": "586894672179024",
        "query": "Office supplies", "slug": "office-supplies"},
    "pet_supplies": {
        "category_id": "197143151659643",
        "query": "Pet supplies", "slug": "pet-supplies"},
    "property_sale": {
        "category_id": "1270772586445798",
        "query": "Property for sale", "slug": "propertyforsale"},
    "sporting_goods": {
        "category_id": "391335928190702",
        "query": "Sporting goods", "slug": "sporting-goods"},
    "toys_games": {
        "category_id": "199404184572737",
        "query": "Toys and games", "slug": "toys"},

    # The exception. Facebook gave a PATH for this one and no id:
    #   /marketplace/bangalore/propertyrentals
    # It runs as a search for its own name instead, so the price and recency
    # filters ride in a query string Facebook demonstrably honours. Appending
    # filters to a bare path is untested, and one unverified mechanism in this
    # file is enough.
    "property_rent":       {"query": "Property for rent",      "slug": "propertyrentals"},

    # REMOVED: `mobile_phones`. It is not in Facebook's category menu at all --
    # phones live under Electronics. Offering it would have silently returned
    # the generic feed, which is worse than not offering it.
}

# What the automated sweep walks: Vingo's actual target inventory.
#
# These are KEYWORD searches, not category browses, and the distinction is the
# whole point. Laptops, gaming, photography and watches are Facebook SUB
# categories, and we have verified numeric ids only for the 17 top-level ones.
# `category_id` also loses to a conflicting `query` -- a run sending both
# returns the keyword's results and silently discards the category -- so
# "Electronics narrowed to laptops" is not a thing the Actor can be asked for.
#
# A cell here therefore sends `query=<keyword>` and NO `category_id` at all.
# The cost: Facebook matches DESCRIPTION text, so "cycles" once returned
# MacBooks whose listings mentioned a battery cycle count. `_run_one_cell`
# passes the keyword to `ingest_collected(search_term=...)` so
# `pipeline.relevance` filters on the title afterwards.
#
# Replaced `AUTOMATION_CATEGORIES`, whose first entry was `mobile_phones` --
# a key deleted from CATEGORIES when it turned out not to be a Facebook
# category at all. `client.py` falls back to `category.replace("_", " ")` for
# an unknown key, so that cell had been quietly running a keyword search for
# "mobile phones" while presenting itself as a category browse.
#
# Tune the keywords freely; `test_vingo_cells.py` pins the invariants, not the
# wording.
VINGO_CELLS: dict[str, str] = {
    # Laptops
    "laptops": "laptop",
    "macbooks": "macbook",
    # Gaming and consoles
    "consoles_playstation": "playstation",
    "consoles_xbox": "xbox",
    "consoles_nintendo": "nintendo switch",
    "gaming_controllers": "gaming controller",
    "vr_headsets": "vr headset",
    "video_games": "video games",
    # Photography
    "cameras": "dslr camera",
    "lenses": "camera lens",
    "drones": "drone",
    "studio_dj": "dj controller",
    # Music -- the one area with a VERIFIED top-level category id, which is
    # why `musical_instruments` stays a category cell in CATEGORIES above.
    # These narrow within it; one real id beats four keywords where it exists.
    "guitars": "guitar",
    "keyboards_piano": "keyboard piano",
    "drums": "drum kit",
    "amps_effects": "guitar amplifier",
    # Watches
    "watches": "watch",
}

# Category cells the sweep also walks, by verified `category_id`. Keep this
# short: a category buys a whole page, and only the ones Vingo actually wants
# earn that.
VINGO_CATEGORY_CELLS: list[str] = ["musical_instruments"]


def sweep_cells() -> list[str]:
    """Every cell key the sweep walks, keywords first.

    One list so the quota allocator, the cost estimate and the runner cannot
    disagree about what a sweep covers -- volume lived in two files once
    before and they diverged.
    """
    return [*VINGO_CELLS, *VINGO_CATEGORY_CELLS]


# Kept as an alias: `sweep.automation_cells()` and the CLI still import this
# name, and a sweep walking the wrong list is not worth a rename.
AUTOMATION_CATEGORIES: list[str] = sweep_cells()
