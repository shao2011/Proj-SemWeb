import pytest

from rag_ingestion.providers.keys import KeyRotator, StickyKeyPool, parse_keys


def test_parse_keys_splits_on_comma_and_trims() -> None:
    assert parse_keys("a, b ,c") == ["a", "b", "c"]


def test_parse_keys_drops_blank_entries() -> None:
    assert parse_keys("a,,  ,b") == ["a", "b"]


def test_parse_keys_single_value_is_one_element() -> None:
    assert parse_keys("only-one") == ["only-one"]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_keys_empty_input_is_empty_list(raw: str | None) -> None:
    assert parse_keys(raw) == []


def test_key_rotator_cycles_in_order_and_wraps() -> None:
    rotator = KeyRotator(["a", "b", "c"])

    drawn = [rotator.next_key() for _ in range(7)]

    assert drawn == ["a", "b", "c", "a", "b", "c", "a"]


def test_key_rotator_single_key_always_returns_it() -> None:
    rotator = KeyRotator(["only"])

    assert [rotator.next_key() for _ in range(3)] == ["only", "only", "only"]


def test_key_rotator_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError):
        KeyRotator([])


def test_key_rotator_is_thread_safe() -> None:
    """Every draw across many threads must be a valid key, none skipped or
    duplicated by a race on the shared index."""
    from concurrent.futures import ThreadPoolExecutor

    rotator = KeyRotator(["a", "b"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        drawn = list(pool.map(lambda _: rotator.next_key(), range(400)))

    assert len(drawn) == 400
    assert all(key in {"a", "b"} for key in drawn)
    assert drawn.count("a") == 200
    assert drawn.count("b") == 200


def test_sticky_key_pool_same_sticky_always_resolves_the_same_key() -> None:
    pool = StickyKeyPool(["a", "b", "c"])

    resolved = [pool.key_for("thread-1") for _ in range(5)]

    assert len(set(resolved)) == 1


def test_sticky_key_pool_spreads_different_stickies() -> None:
    pool = StickyKeyPool(["a", "b"])

    resolved = {pool.key_for(f"thread-{i}") for i in range(50)}

    assert resolved == {"a", "b"}


def test_sticky_key_pool_none_sticky_round_robins() -> None:
    pool = StickyKeyPool(["a", "b", "c"])

    drawn = [pool.key_for(None) for _ in range(6)]

    assert drawn == ["a", "b", "c", "a", "b", "c"]


def test_sticky_key_pool_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError):
        StickyKeyPool([])
