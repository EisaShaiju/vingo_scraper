# Vingo — Facebook Marketplace Ingestion

Ingests Facebook Marketplace listings (India) via Apify's managed Actor,
rehosts every image to our own storage, and writes to Supabase.

## Read this before running anything

**1. Images must be rehosted, and that is the point of this codebase.**
Facebook CDN urls are signed with `oh=`/`oe=` parameters and expire within
hours-to-days. A listing saved with an `fbcdn.net` url looks perfectly healthy
at write time and shows a broken image to a buyer a week later. Every image is
downloaded, verified, and uploaded to our storage *during ingestion* — never as
a later batch job, which would find the urls already dead.

**2. Nothing auto-publishes.** Listings land `pending_review`. A human approves
before anything is buyer-visible. This is both catalog quality control and the
process to point at if a seller or Meta objects.

**3. The legal position needs counsel.** Listing photos are the seller's
copyright and seller details are personal data under India's DPDP Act. Because
we now *store* seller photos rather than hot-linking them, the exposure is more
concrete than it was, not less. Mitigations are built in (review queue,
link-back, minimal seller fields) but they reduce exposure, they don't erase
it. Not legal advice.

**4. Apify bills per result** (~$0.005/item). `resultsLimit` is always set and
two independent caps are enforced (`apify_max_items_hard_cap`,
`apify_max_charge_usd`) because a typo in `--max-items` is a billing incident.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env          # fill in Apify token + Supabase credentials
```

Supabase needs: a project, a **public** bucket named `listing-images`, both
connection strings, and the service-role key. See `docs/supabase.md` — the
pooler ports are not interchangeable and the traps there are silent.

```bash
alembic upgrade head          # uses the :5432 migration url, not :6543
```

## Run

```bash
# Fetch and map only — no storage writes, no database writes.
vingo-scrape --query iphone --location mumbai --max-items 10 --dry-run

# Full ingest.
vingo-scrape --query iphone --location mumbai --max-items 100
```

Exit codes are meaningful so a scheduled run can't fail quietly:

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Apify run failed — **not** a statement about inventory |
| 3 | Ran fine, zero listings — a density signal |
| 4 | Listings ingested but every image failed — not servable |

The 2-vs-3 split matters: "we couldn't measure" and "there's nothing there"
lead to opposite decisions.

---

## Architecture

```
apify/urls.py      (search_query, location) -> Marketplace URL
apify/client.py    trigger Actor, wait, fetch dataset, record cost
apify/runner.py    orchestration: fetch -> parse -> rehost -> persist
extract/           fb_payload.py: payload -> Listing (tree-walking parser)
pipeline/          normalize.py: INR incl. lakh/crore, condition, timestamps
media/             rehost.py: download -> verify -> hash -> upload
                   storage.py: Supabase adapter (+ InMemoryStore for tests)
db/                models, session (pooler-aware), repository (upsert/dedupe)
alembic/           migrations
```

### Why our parameters differ from the Actor's

The Actor takes `startUrls` / `resultsLimit` / `includeListingDetails`. We
expose `(search_query, location, max_items)` and translate in `apify/client.py`.
There are several competing Marketplace Actors; swapping one in should touch
two files, not every call site.

### Why the parser survived the pivot from self-hosted browsers

The Actor returns **Facebook's own field names**, and `fb_payload.py` walks the
payload tree looking for listing-shaped objects rather than following a fixed
path. It parsed Apify items with zero changes. Don't "simplify" it into direct
key access — that fragility is exactly what it avoids.

### Media pipeline guarantees

- Images identified by **magic bytes**, not url extension or `Content-Type`.
  An expired fbcdn url commonly returns an HTML error page with a 200; those
  are rejected, never uploaded.
- Storage key is `listings/{fb_listing_id}/{sha256[:16]}.{ext}` —
  content-addressed, so re-ingesting overwrites rather than duplicates.
- Identical bytes across listings upload **once**.
- A failed image costs one image; the listing still persists with
  `status='failed'` for retry.

---

## Development

```bash
.venv/Scripts/python.exe -m pytest tests/ -q          # 93 tests, fully offline
.venv/Scripts/python.exe -m ruff check src/ tests/ --fix
```

Tests never touch the network or a live database — Apify is faked, HTTP is
stubbed with `respx`, storage uses `InMemoryStore`. Diagnosing a breakage must
never require credentials.

On Windows use `PYTHONIOENCODING=utf-8` for anything printing `₹`.

## Status

Built and tested: Apify trigger with billing guards · payload parsing ·
INR/lakh/crore normalization · media rehosting with verification, dedupe and
failure isolation · SQLAlchemy models + Alembic migration · upsert/dedupe
repository · end-to-end orchestration · CLI.

**Not yet run against live Apify or Supabase** — that needs credentials. The
first real run should be small (`--max-items 10`) to freeze a genuine dataset
fixture and confirm the per-item cost.

Not built: FastAPI review-queue service, the failed-image retry sweep, and the
India density probe (now cheap — a few small Actor runs).
