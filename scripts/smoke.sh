#!/usr/bin/env bash
# Does the deployed console actually work?
#
# Run this after EVERY deploy. "Tests pass locally" is not a deployment -- the
# VM once ran four commits behind for two sessions while the work was reported
# as finished.
#
#   ./scripts/smoke.sh                                  # on the VM itself
#   ./scripts/smoke.sh https://x.trycloudflare.com user:pass
#
# Exits non-zero on the first failure and prints what it actually got.
#
# The check that matters is STATS vs LISTINGS. Twice the console reported
# "No listings yet" while 237 listings sat in Postgres: once because three
# columns were mapped onto the wrong class, once because the lead scorer
# lazy-loaded a relationship under the async engine. Both were invisible to 345
# offline tests and both would have tripped that one comparison instantly,
# because /api/listings catches its own exceptions and returns an empty list --
# so a crashed query is indistinguishable from an empty catalog unless you
# compare it against something.

set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
AUTH="${2:-}"
BASE="${BASE%/}"

CURL=(curl -fsS --max-time 30)
[ -n "$AUTH" ] && CURL+=(-u "$AUTH")

PASS=0
FAIL=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '        %s\n' "$1"; }

get() { "${CURL[@]}" "$BASE$1" 2>/dev/null; }

# A tiny JSON reader. python3 is on the VM and in every dev environment here;
# jq is not guaranteed, and this script must not need installing to be run.
jq_() { python3 -c "
import json,sys
try: d = json.load(sys.stdin)
except Exception: print('__BADJSON__'); raise SystemExit
$1" 2>/dev/null; }

echo
echo "smoke: $BASE"
echo

# -------------------------------------------------------------- service ---
HEALTH="$(get /api/health)"
if [ -z "$HEALTH" ]; then
  bad "/api/health unreachable"
  note "the service is down, or the URL/credentials are wrong"
  echo; echo "1 check could not run -- stopping here."; exit 1
fi
if printf '%s' "$HEALTH" | grep -q '"ok":true'; then
  ok "/api/health"
else
  bad "/api/health did not report ok"
  note "$HEALTH"
fi

# ------------------------------------------------- stats versus listings ---
# The single most valuable line in this file.
STATS_N="$(get /api/stats | jq_ "print(d.get('listings', d.get('listing_count', -1)))")"
LIST="$(get '/api/listings?limit=5')"
LIST_N="$(printf '%s' "$LIST" | jq_ "print(d.get('count', -1))")"
DB_OK="$(printf '%s' "$LIST" | jq_ "print(d.get('db_available', True))")"
ERR="$(printf '%s' "$LIST" | jq_ "print(d.get('error') or '')")"

if [ "$DB_OK" = "False" ]; then
  bad "/api/listings reported a failed read"
  note "${ERR:-no reason given}"
elif [ "$STATS_N" -gt 0 ] 2>/dev/null && [ "$LIST_N" = "0" ]; then
  bad "stats says $STATS_N listings, /api/listings returned 0"
  note "this is the signature of both production bugs so far:"
  note "a crashed query rendering as an empty catalog"
elif [ "$LIST_N" = "-1" ]; then
  bad "/api/listings returned nothing parseable"
  note "$(printf '%s' "$LIST" | head -c 160)"
else
  ok "/api/listings  ($LIST_N rows, stats says $STATS_N)"
fi

# ----------------------------------------------------------- lead desk ---
LEAD="$(get '/api/listings?limit=5&order=lead')"
LEAD_OK="$(printf '%s' "$LEAD" | jq_ "
rows = d.get('listings') or []
if d.get('db_available') is False: print('failed:' + str(d.get('error'))[:150])
elif not rows: print('empty')
else: print('yes' if 'score' in (rows[0].get('lead') or {}) else 'no')")"
case "$LEAD_OK" in
  yes)   ok "/api/listings?order=lead carries a lead score" ;;
  empty) ok "/api/listings?order=lead (no rows yet -- nothing to score)" ;;
  failed:*)
         bad "/api/listings?order=lead failed to read"
         note "${LEAD_OK#failed:}" ;;
  *)     bad "lead rows have no score"
         note "the scorer failed, or the payload shape changed" ;;
esac

# -------------------------------------------------------------- export ---
CSV="$(get /api/export.csv)"
CSV_HEAD="$(printf '%s' "$CSV" | head -1)"
if printf '%s' "$CSV_HEAD" | grep -q '^lead_score,band,signals,title'; then
  CSV_ROWS=$(( $(printf '%s\n' "$CSV" | wc -l) - 1 ))
  ok "/api/export.csv  ($CSV_ROWS rows)"
else
  bad "/api/export.csv has no header row"
  note "$(printf '%s' "$CSV_HEAD" | head -c 120)"
fi

# ----------------------------------------------------------- the console ---
HTML="$(get /)"
# Check the panels are PRESENT by name, not that there are N of them. A count
# goes stale the moment a tab is added -- which it did, reporting "an old build
# is deployed" about a build that was brand new and correct. Naming them means
# the failure says which one is missing.
MISSING=""
for panel in p-manual p-leads p-bulk p-catalog; do
  printf '%s' "$HTML" | grep -q "data-panel=\"$panel\"" || MISSING="$MISSING $panel"
done
if [ -z "$MISSING" ]; then
  ok "console serves every tab (Search, Leads, Bulk, Catalog)"
else
  bad "console is missing tab(s):$MISSING"
  note "an older build is deployed, or a panel was renamed"
fi

# ------------------------------------------------------------ invariant ---
if printf '%s' "$LIST" | grep -q 'fbcdn\.net'; then
  bad "an fbcdn URL is being served"
  note "Facebook's signed URLs expire in hours -- storage_url is the renderable one"
else
  ok "no fbcdn URL served"
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "$FAIL failed, $PASS passed"
  exit 1
fi
echo "all $PASS checks passed"
