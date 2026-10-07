#!/usr/bin/env bash
# Does the safety net actually catch the two bugs that got through?
#
# A net nobody has watched fail is not known to work. This project has twice
# shipped code that passed every test and did not run, so the tests written
# afterwards get the same scepticism: re-introduce each bug, and the suite must
# go RED. A test that stays green is decoration and gets rewritten.
#
#   bash scripts/prove_the_net.sh "postgresql+psycopg://comet:PW@127.0.0.1:5432/comet_test"
#
# Works on a COPY of the source in a temp directory. Nothing under /opt/comet
# is modified, and the copy is deleted on exit however this ends.

set -uo pipefail

URL="${1:-${VINGO_TEST_DATABASE_URL:-}}"
if [ -z "$URL" ]; then
  echo "usage: $0 <VINGO_TEST_DATABASE_URL>" >&2
  exit 2
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$SRC/.venv/bin/python}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "copying source to $WORK"
cp -r "$SRC/src" "$SRC/tests" "$SRC/alembic" "$SRC/alembic.ini" "$WORK/" 2>/dev/null
cd "$WORK"

FAILED=0

# $1 = label, $2 = python snippet that re-introduces the bug,
# $3 = the test file that must now fail
try_break() {
  local label="$1" patch="$2" target="$3"
  echo
  echo "=============================================================="
  echo "  re-breaking: $label"
  echo "=============================================================="

  cp -r "$SRC/src" "$WORK/src.bak" >/dev/null 2>&1 || true
  if ! "$PY" -c "$patch"; then
    echo "  COULD NOT APPLY the break -- the code has moved on."
    echo "  This proves nothing either way. Fix the patch, not the test."
    FAILED=$((FAILED+1))
    rm -rf "$WORK/src"; mv "$WORK/src.bak" "$WORK/src"
    return
  fi

  set +e
  VINGO_TEST_DATABASE_URL="$URL" "$PY" -m pytest "$target" -q --no-header 2>&1 | tail -4
  local rc=${PIPESTATUS[0]}
  set -e

  if [ "$rc" -ne 0 ]; then
    echo "  CAUGHT -- the suite went red, as it must."
  else
    echo "  *** NOT CAUGHT. The tests passed with the bug reintroduced. ***"
    echo "  *** That test is decoration. Rewrite it.                   ***"
    FAILED=$((FAILED+1))
  fi

  rm -rf "$WORK/src"; mv "$WORK/src.bak" "$WORK/src"
}

# ---------------------------------------------------------------- bug 1 ---
# Three outreach columns back onto Seller, exactly as the original edit did:
# anchored on a `first_seen_at` line that exists in BOTH classes, replacing
# the FIRST match.
try_break "outreach columns onto Seller instead of Listing" '
import pathlib
p = pathlib.Path("src/vingo_scraper/db/models.py"); t = p.read_text(encoding="utf-8")
block = t[t.index("    # Outreach state."):t.index("    outreach_note: Mapped[str | None] = mapped_column(Text)") + len("    outreach_note: Mapped[str | None] = mapped_column(Text)") + 2]
t = t.replace(block, "", 1)
anchor = "    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)"
assert t.count(anchor) == 2, t.count(anchor)
t = t.replace(anchor, block + anchor, 1)   # the original bug: first match wins
p.write_text(t, encoding="utf-8")
' tests/integration/test_schema_matches_models.py

# ---------------------------------------------------------------- bug 2 ---
# The scorer reads listing.images again instead of taking the count.
try_break "lead_score lazy-loading listing.images" '
import pathlib
p = pathlib.Path("src/vingo_scraper/pipeline/lead_score.py"); t = p.read_text(encoding="utf-8")
old = """    if photo_count is None:
        photo_count = len(getattr(listing, "images", None) or [])"""
assert t.count(old) == 1
t = t.replace(old, """    photo_count = len(getattr(listing, "images", None) or [])""", 1)
p.write_text(t, encoding="utf-8")
' tests/integration/test_listings_endpoint.py

echo
echo "=============================================================="
if [ "$FAILED" -gt 0 ]; then
  echo "  $FAILED of 2 bugs were NOT caught. The net has holes."
  exit 1
fi
echo "  Both bugs caught. The net works."
echo "=============================================================="
