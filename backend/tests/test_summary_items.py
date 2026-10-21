from rag_ingestion.schemas.summary import SummaryItem
from rag_ingestion.summary import parse_items, render_items


def test_render_then_parse_roundtrips() -> None:
    items = [
        SummaryItem(id="s1", text="User works at FPT on a 12-person team", used=3, folds=7),
        SummaryItem(id="s3", text="Payment module deadline moved to Q4"),
    ]

    assert parse_items(render_items(items)) == items


def test_counters_ride_in_the_stored_line() -> None:
    """They live in the text column with the item they belong to: a separate
    table would have to be kept in sync with every eviction."""
    rendered = render_items(
        [SummaryItem(id="s2", text="Deadline is 30/9", used=4, folds=9)]
    )

    assert rendered == "[s2|u:4|f:9] Deadline is 30/9"


def test_lines_written_before_counters_existed_read_as_zero() -> None:
    """Backward compatible on purpose: no migration, and a thread summarized
    by the previous build keeps every item."""
    items = parse_items("[s1] An older item\n[s2|u:2|f:5] A newer one")

    assert (items[0].used, items[0].folds) == (0, 0)
    assert (items[1].used, items[1].folds) == (2, 5)


def test_none_and_blank_parse_to_nothing() -> None:
    assert parse_items(None) == []
    assert parse_items("") == []
    assert parse_items("   \n  ") == []


def test_legacy_prose_summary_becomes_one_addressable_item() -> None:
    """Threads written by the old summarizer hold a paragraph with no ids.

    It must come back as `s1` rather than being dropped: dropping it loses
    history, and leaving it unparsed leaves the model nothing to retire by
    name.
    """
    items = parse_items("They discussed articles 5 and 6.\nArticle 5 defines scope.")

    assert len(items) == 1
    assert items[0].id == "s1"
    assert "articles 5 and 6" in items[0].text
    assert "\n" not in items[0].text


def test_unparseable_lines_between_items_are_skipped() -> None:
    stored = "[s1] First fact\nstray line the model slipped in\n[s2] Second fact"

    items = parse_items(stored)

    assert [item.id for item in items] == ["s1", "s2"]


def test_item_order_is_preserved() -> None:
    """List order is recency of touch; parse must not re-sort by id."""
    stored = "[s3] Oldest untouched\n[s1] Recently updated"

    assert [item.id for item in parse_items(stored)] == ["s3", "s1"]
