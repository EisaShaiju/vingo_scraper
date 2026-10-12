"""The sweep buys Vingo's inventory, not Facebook's menu.

Laptops, gaming, photography and watches are Facebook SUBcategories and no
verified numeric id exists for any of them. `category_id` also loses to a
conflicting `query` -- a run sending both returns the keyword's results and
discards the category without saying so -- which makes "Electronics narrowed to
laptops" unavailable at the Actor.

So these cells are keyword searches sending no category at all. These tests pin
that, and the title filter that has to compensate for Facebook matching
DESCRIPTION text.
"""

from __future__ import annotations

from vingo_scraper.config import (
    CATEGORIES,
    VINGO_CATEGORY_CELLS,
    VINGO_CELLS,
    sweep_cells,
)


def test_01_every_keyword_cell_has_a_non_empty_keyword() -> None:
    for key, keyword in VINGO_CELLS.items():
        assert keyword.strip(), f"{key} has no search term"


def test_02_no_keyword_cell_collides_with_a_facebook_category() -> None:
    """The `mobile_phones` class of bug.

    That key sat in AUTOMATION_CATEGORIES after being deleted from CATEGORIES,
    and `client.py` falls back to `category.replace("_", " ")` for an unknown
    key -- so the cell quietly ran a keyword search for "mobile phones" while
    presenting itself as a category browse. A collision in either direction is
    ambiguous about which path a cell takes.
    """
    assert not set(VINGO_CELLS) & set(CATEGORIES)


def test_03_every_category_cell_is_a_real_verified_category() -> None:
    """A category cell must carry an id somebody opened in a browser. A wrong
    one does not 404 -- Facebook serves the generic feed, which reads as a
    working search with strange results."""
    for key in VINGO_CATEGORY_CELLS:
        assert key in CATEGORIES, f"{key} is not a verified category"
        assert CATEGORIES[key].get("category_id") or CATEGORIES[key].get("slug")


def test_04_sweep_cells_is_the_single_source_of_what_a_sweep_walks() -> None:
    """Volume lived in two files once and they disagreed, so every search
    returned ~24 listings whatever was asked for."""
    cells = sweep_cells()
    assert len(cells) == len(set(cells)), "a duplicated cell is paid for twice"
    assert set(cells) == set(VINGO_CELLS) | set(VINGO_CATEGORY_CELLS)


def test_05_the_targets_the_business_asked_for_are_covered() -> None:
    """Gaming, laptops, music, photography and watches. Named explicitly so
    dropping one is a test failure rather than a quiet gap in the catalog."""
    terms = " ".join(VINGO_CELLS.values()).lower()
    for want in ("laptop", "playstation", "xbox", "camera", "drone",
                 "guitar", "watch"):
        assert want in terms, f"nothing in the sweep searches for {want}"


def test_06_a_keyword_cell_sends_a_query_and_no_category() -> None:
    """Both together is the trap: a run sending `category_id` alongside a
    conflicting `query` is charged, succeeds, and silently drops the
    category."""
    import inspect

    from vingo_scraper.api import main as api

    src = inspect.getsource(api._run_one_cell)
    assert "VINGO_CELLS.get(category)" in src
    assert "category=None if keyword else category" in src


def test_07_a_keyword_cell_filters_on_the_title_afterwards() -> None:
    """Facebook matches DESCRIPTION text, so `cycles` returned MacBooks whose
    listings mentioned a battery cycle count. Actor-side terms reduce COST;
    they never guarantee correctness."""
    import inspect

    from vingo_scraper.api import main as api

    assert "search_term=keyword" in inspect.getsource(api._run_one_cell)


def test_08_the_sweep_walks_the_vingo_cells_not_the_facebook_menu() -> None:
    """`plan_cells` allocates only to cells present in `yields`, so the yields
    dict is what decides coverage."""
    from vingo_scraper.api.main import measured_yields

    yields, _ = measured_yields()
    assert set(yields) == set(sweep_cells())


def test_09_a_partial_baseline_does_not_shrink_the_sweep(monkeypatch) -> None:
    """The dangerous case. A baseline measured against the OLD category set
    matches one key, and `plan_cells` would then allocate to that cell alone --
    a 1000-target run buying one page and reporting itself exhausted."""
    from pathlib import Path

    from vingo_scraper.api import main as api

    one_key = next(iter(sweep_cells()))
    fake = f"| `{one_key}` | 28 | **26** | 4% | 41 | $0.0015 |"

    real_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **kw: fake if "supply-baseline" in str(self)
        else real_read(self, *a, **kw),
    )
    yields, measured = api.measured_yields()
    assert set(yields) == set(sweep_cells()), "cells vanished from the plan"
    assert measured is False, "a partial baseline must not claim to be measured"
