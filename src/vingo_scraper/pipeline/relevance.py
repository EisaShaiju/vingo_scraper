"""Keep search results that actually match the search.

Facebook matches a keyword against the whole listing -- **including the
description**. Laptop and phone listings routinely say "battery cycle count:
120", so a search for `cycles` returns MacBooks and iPhones alongside the
bicycles, and Facebook considers them genuine matches.

That is Facebook's relevance, not a scraper bug. This filter is ours: keep a
listing when the **title** matches, drop it when only the description did.

Two things worth knowing:

* **It runs whenever a keyword was given -- category or not.** An earlier
  version skipped category browses, reasoning that "a category URL asks
  Facebook for the category itself, so there is no keyword to match". True
  when written; false once keyword and category started travelling together.
  The checkbox stayed ticked while the filter quietly stopped running, which
  is how "guitar" inside Musical instruments returned a townhouse and two
  cars.
* **It does not save money.** Apify charges by the page before any of this
  runs. This narrows what we KEEP, and the UI says so -- a filter that looks
  like a saving and is not would be worse than no filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# Words too common to carry meaning. A query of "new phone" should match on
# "phone"; requiring "new" as well would drop most of the real results.
STOPWORDS = frozenset({
    "a", "an", "and", "for", "in", "new", "of", "or", "sale", "the", "to",
    "used", "with",
})


@dataclass
class RelevanceReport:
    kept: int = 0
    dropped: int = 0
    # Titles that were dropped, so an over-aggressive filter is visible rather
    # than silently eating half the results.
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kept": self.kept, "dropped": self.dropped,
                "examples": self.examples[:5]}


def _stems(term: str) -> list[str]:
    """Meaningful words from a query, each reduced to a crude stem.

    Crude on purpose: `cycle` must match `cycles` and `bicycle`, and a real
    stemmer would be a dependency and a surprise. Trailing `s`/`es` is the
    only inflection that matters for product nouns.
    """
    words = re.findall(r"[a-z0-9]+", (term or "").lower())
    out = []
    for w in words:
        if w in STOPWORDS or len(w) < 3:
            continue
        if w.endswith("es") and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and len(w) > 3:
            w = w[:-1]
        out.append(w)
    return out


def title_matches(title: str | None, term: str) -> bool:
    """Does the title contain any meaningful word from the query?

    ANY rather than ALL: "xbox series s" should match "Xbox Series S 512gb"
    and also plain "Xbox controller". Requiring every word would drop most of
    what a person means by the search.
    """
    stems = _stems(term)
    if not stems:
        # Nothing meaningful to match on -- keep everything rather than
        # silently returning an empty result for a query like "for sale".
        return True
    haystack = (title or "").lower()
    return any(s in haystack for s in stems)


def filter_relevant(listings, term: str | None) -> tuple[list, RelevanceReport]:
    """Keep listings whose title matches `term`. No term means no filtering."""
    report = RelevanceReport()
    if not term or not _stems(term):
        report.kept = len(listings)
        return list(listings), report

    kept = []
    for item in listings:
        if title_matches(getattr(item, "title", None), term):
            kept.append(item)
        else:
            report.dropped += 1
            if len(report.examples) < 5:
                report.examples.append((getattr(item, "title", "") or "")[:60])
    report.kept = len(kept)

    if report.dropped:
        log.info("relevance.filtered", term=term, kept=report.kept,
                 dropped=report.dropped, examples=report.examples)
    return kept, report
