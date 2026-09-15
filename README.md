# Vingo — Facebook Marketplace Ingestion

Seeds Vingo's catalog with Facebook Marketplace listings (India). Built as a
POC whose own telemetry produces the evidence for a maintenance retainer.

Full plan: `~/.claude/plans/ok-i-am-trying-peaceful-ocean.md`

---

## Read this before running anything

**1. Marketplace requires login.** Verified live on this machine:
`/marketplace/mumbai/search` hard-redirects to `/login/?next=…`. There is no
logged-out path to search results.

**2. That weakens the legal footing.** *Meta v. Bright Data* (N.D. Cal., Jan
2024) held Facebook's ToS do not bar **logged-out** scraping of public data.
Because this project must log in, that ruling does not cover it — logged-in
scraping makes us a user, bound by the ToS. Do not let anyone cite Bright Data
as cover here.

**3. Republishing is the bigger exposure.** Listing photos are the seller's
copyright; seller names and locations are personal data under India's DPDP Act
2023. Mitigations are built in (review queue, no auto-publish, link-back,
image rehosting off by default) but they reduce exposure, they do not erase it.
Get Vingo's decision in writing. Not legal advice — have counsel review before
anything goes buyer-visible.

**4. Density is unproven.** Marketplace India launched as a limited trial and
never reached OLX/Quikr density. Phase 0 exists to settle this before money is
spent on infrastructure.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m playwright install chromium
cp .env.example .env
```

## Phase 0 — density probe (do this first)

```bash
# One-time: authenticate an account in a real browser window.
# Credentials are never stored by this tool; only the session is kept.
vingo-login --account probe1

# Run the city x category grid.
vingo-probe --account probe1
```

Writes `reports/density_probe.json` and prints a matrix plus a go/no-go.

Exit codes are meaningful, so a scheduled run cannot fail silently:

| Code | Meaning |
|---|---|
| 0 | GO or MARGINAL — usable data collected |
| 2 | BLOCKED — session dead/challenged. **Not** a statement about inventory |
| 3 | NO-GO — measured successfully, inventory too thin |

The BLOCKED/NO-GO split matters: "we couldn't measure" and "there's nothing
there" lead to opposite decisions, and conflating them is how a client gets
told the wrong thing.

---

## Architecture

### Why intercept GraphQL instead of parsing the DOM

The well-known open-source scraper for this
([passivebot](https://github.com/passivebot/facebook-marketplace-scraper),
398★, archived Nov 2024) drives Playwright, dumps HTML, and parses it with CSS
selectors. Facebook's class names are hashed and regenerated on deploy, so
every selector is a tripwire. That is why it is archived.

Instead, we drive a real browser and attach a passive listener to
`/api/graphql/` responses, reading the structured JSON the page already
receives. We do **not** call GraphQL directly — that needs a `doc_id` (rotates
every few weeks) plus a session-bound `fb_dtsg` token. Letting the page make
its own authenticated requests means a `doc_id` rotation costs us nothing.

The parser then **walks the payload tree** for listing-shaped objects rather
than following a fixed path like
`data.marketplace_search.feed_units.edges[]`. FB renames wrappers far more
often than it changes the listing model, so a restructure is a no-op for us.
`tests/test_graphql_extract.py::test_survives_wrapper_restructuring` pins this.

### Two paths, one contract

| Path | Source | Tag |
|---|---|---|
| Primary | Intercepted GraphQL JSON | `graphql` |
| Fallback | Embedded JSON in `<script>`, then anchor heuristics | `dom_fallback` |

Both emit the same `Listing`. Every row records `extraction_method`, so a
**rising share of `dom_fallback` is the early warning** that the GraphQL shape
has drifted — days before anyone notices missing data. That signal is what the
retainer is actually selling.

The fallback never matches on hashed class names — it anchors on
`/marketplace/item/<id>/` hrefs, the one structural invariant the site needs to
keep working. `test_no_dependency_on_hashed_class_names` enforces this.

### Layout

```
src/vingo_scraper/
  config.py            # geo, categories, rate limits -- check here first when FB changes
  browser/
    session.py         # persistent per-account contexts, login-wall detection
    stealth.py         # fingerprint hardening (hygiene, not a cloak)
    interceptor.py     # passive /api/graphql/ capture
  extract/
    schema.py          # the Listing contract
    graphql.py         # primary parser (tree-walking)
    dom.py             # fallback parser
  pipeline/
    normalize.py       # INR parsing (incl. lakh/crore), condition, timestamps
    search.py          # drives one city x category search
    probe.py           # Phase 0 density metrics + go/no-go
  cli.py
```

---

## Status

Built and tested:

- Playwright harness with persistent sessions and stealth hardening
- GraphQL interceptor (handles NDJSON multipart + anti-hijack prefixes)
- GraphQL tree-walking parser, resilient to wrapper renames
- DOM fallback on the same contract, independent of class names
- INR normalization including `1.2 lakh` / `45k` / `2.5 cr`
- Phase 0 probe with tested go/no-go thresholds
- 60 tests, all offline against frozen fixtures

Not built yet (Phases 3, 5–8 of the plan):

- Postgres persistence, Alembic migrations, upsert/dedupe
- Account pool, sticky proxies, ban detection
- FastAPI service and review queue
- Canary monitor and `selector_health` baseline

## Rate discipline

`config.py` defaults are deliberately conservative: 3–8s randomized delays,
~50 page loads per account per day. **Raising these is the fastest way to burn
the account pool.** Account burn is a recurring operating cost, not an
incident — budget for it.

## Development

```bash
.venv/Scripts/python.exe -m pytest tests/ -q     # 60 tests, no network
.venv/Scripts/python.exe -m ruff check src/ tests/
```

### When Facebook breaks something

1. Capture a fresh payload and drop it in `tests/fixtures/`
2. Run the suite — the golden tests localize the break
3. Fix the key lists in `extract/graphql.py` (or `config.py` for URL params)
4. Keep the fixture; it becomes the regression guard

This loop is the retainer's actual work, and it is why the tests run entirely
offline: diagnosing a breakage should never require a live session.
