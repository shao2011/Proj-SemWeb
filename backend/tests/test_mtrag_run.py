import pytest

from rag_ingestion.chunking import ChunkLevel
from scripts.benchmark.mtrag_data import Passage
from scripts.benchmark.mtrag_run import (
    BENCHMARK_OWNER,
    passages_to_chunks,
    sample_passages,
)


def _passage(passage_id: str, text: str = "body", title: str = "") -> Passage:
    return Passage(passage_id=passage_id, text=text, title=title)


# --- passage to chunk -------------------------------------------------------


def test_the_chunk_id_is_the_passage_id_so_qrels_match_directly() -> None:
    """The whole scoring path depends on this.

    MTRAG qrels reference passage ids, so a retrieved chunk has to carry the
    same id back or every hit scores as a miss.
    """
    chunks = list(passages_to_chunks([_passage("10171-0-2129")], project_id="p"))

    assert chunks[0].chunk_id == "10171-0-2129"


def test_passages_are_indexed_as_citable_leaves() -> None:
    chunks = list(passages_to_chunks([_passage("a")], project_id="p"))

    assert chunks[0].level is ChunkLevel.LEAF
    assert chunks[0].citable is True
    assert chunks[0].parent_id is None


def test_every_chunk_carries_the_benchmark_owner_and_the_given_project() -> None:
    """Retrieval filters on both, so a wrong value here returns nothing at all
    rather than returning something wrong."""
    chunks = list(passages_to_chunks([_passage("a")], project_id="prj_fiqa"))

    assert chunks[0].owner_user_id == BENCHMARK_OWNER
    assert chunks[0].project_id == "prj_fiqa"


def test_a_title_is_prepended_to_the_indexed_text_but_not_the_source_text() -> None:
    """The title is retrieval signal; the source text is what a citation would
    quote, so it stays verbatim."""
    chunks = list(
        passages_to_chunks([_passage("a", text="body", title="Heading")], project_id="p")
    )

    assert "Heading" in chunks[0].index_text
    assert "body" in chunks[0].index_text
    assert chunks[0].source_text == "body"


def test_an_empty_title_leaves_the_indexed_text_equal_to_the_body() -> None:
    chunks = list(passages_to_chunks([_passage("a", text="body")], project_id="p"))

    assert chunks[0].index_text == "body"


def test_blank_passages_are_dropped_rather_than_indexed_empty() -> None:
    """An empty vector is meaningless and some providers reject the request."""
    chunks = list(
        passages_to_chunks(
            [_passage("a", text="  "), _passage("b", text="real")], project_id="p"
        )
    )

    assert [chunk.chunk_id for chunk in chunks] == ["b"]


# --- sampling ---------------------------------------------------------------


def test_no_limit_keeps_every_passage() -> None:
    passages = [_passage(str(index)) for index in range(5)]

    assert len(sample_passages(passages, relevant=set(), limit=None)) == 5


def test_sampling_always_keeps_the_judged_passages() -> None:
    """Dropping a judged passage would make its query unanswerable and quietly
    depress recall for a reason that has nothing to do with the retriever."""
    passages = [_passage(str(index)) for index in range(100)]

    sampled = sample_passages(passages, relevant={"7", "42"}, limit=10)

    kept = {passage.passage_id for passage in sampled}
    assert {"7", "42"} <= kept
    assert len(sampled) == 10


def test_a_limit_below_the_judged_count_still_keeps_them_all() -> None:
    passages = [_passage(str(index)) for index in range(10)]

    sampled = sample_passages(passages, relevant={"1", "2", "3"}, limit=2)

    assert {passage.passage_id for passage in sampled} == {"1", "2", "3"}


def test_sampling_is_deterministic_for_a_given_seed() -> None:
    passages = [_passage(str(index)) for index in range(50)]

    first = sample_passages(passages, relevant=set(), limit=10, seed=7)
    second = sample_passages(passages, relevant=set(), limit=10, seed=7)

    assert [p.passage_id for p in first] == [p.passage_id for p in second]


# --- pacing -----------------------------------------------------------------


class FakeClock:
    """Monotonic time the test drives, so pacing is asserted not waited out."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    from scripts.benchmark import mtrag_run

    fake = FakeClock()
    monkeypatch.setattr(mtrag_run.time, "monotonic", lambda: fake.now)
    return fake


def test_requests_within_the_budget_never_sleep(clock) -> None:
    from scripts.benchmark.mtrag_run import TokenBudget

    budget = TokenBudget(1000, sleep=clock.sleep)

    assert budget.take(400) == 0.0
    assert budget.take(400) == 0.0
    assert clock.slept == []


def test_exceeding_the_budget_waits_out_the_rest_of_the_minute(clock) -> None:
    """This is what keeps a long run from collapsing into 429 backoff."""
    from scripts.benchmark.mtrag_run import TokenBudget

    budget = TokenBudget(1000, sleep=clock.sleep)
    budget.take(900)
    clock.now += 20  # 20s into the minute

    paused = budget.take(900)

    assert paused == pytest.approx(40)
    assert clock.slept == [pytest.approx(40)]


def test_the_window_resets_on_its_own_after_a_minute(clock) -> None:
    from scripts.benchmark.mtrag_run import TokenBudget

    budget = TokenBudget(1000, sleep=clock.sleep)
    budget.take(900)
    clock.now += 61

    assert budget.take(900) == 0.0
    assert clock.slept == []


def test_a_budget_of_zero_disables_pacing_for_a_paid_key(clock) -> None:
    from scripts.benchmark.mtrag_run import TokenBudget

    budget = TokenBudget(0, sleep=clock.sleep)

    assert budget.take(10_000_000) == 0.0
    assert clock.slept == []


def test_token_estimate_is_conservative_about_four_chars_each() -> None:
    from scripts.benchmark.mtrag_run import TokenBudget

    assert TokenBudget.estimate(["a" * 400]) == pytest.approx(101, abs=1)


# --- retry ------------------------------------------------------------------


class FlakyIndex:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def upsert(self, chunks, embedder) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            from rag_ingestion.providers.registry import ProviderRequestError

            raise ProviderRequestError("Provider request failed")


def test_a_transient_provider_failure_is_retried() -> None:
    """A 429 and a 400 reach us as the same sanitized exception, so retrying
    both is the only option the adapter leaves open."""
    from scripts.benchmark.mtrag_run import _upsert_with_retry

    index = FlakyIndex(failures=2)

    _upsert_with_retry(index, [], None, sleep=lambda _: None)

    assert index.calls == 3


def test_retries_are_bounded_and_the_failure_surfaces() -> None:
    from rag_ingestion.providers.registry import ProviderError
    from scripts.benchmark.mtrag_run import _upsert_with_retry

    index = FlakyIndex(failures=99)

    with pytest.raises(ProviderError):
        _upsert_with_retry(index, [], None, attempts=3, sleep=lambda _: None)

    assert index.calls == 3


def test_the_budget_scales_with_the_number_of_keys(clock) -> None:
    """Round-robin over N keys means N times the per-key allowance.

    `KeyRotator` moves to the next key on every call, so a pool of two trial
    keys has twice the per-minute budget of one. Pacing to the per-key limit
    would run at half the available speed.
    """
    from scripts.benchmark.mtrag_run import budget_for

    budget = budget_for(tokens_per_minute=100_000, keys=2, sleep=clock.sleep)

    assert budget.tokens_per_minute == 200_000
    assert budget.take(150_000) == 0.0
    assert clock.slept == []


def test_a_single_key_gets_the_plain_allowance(clock) -> None:
    from scripts.benchmark.mtrag_run import budget_for

    assert budget_for(tokens_per_minute=100_000, keys=1).tokens_per_minute == 100_000


def test_disabled_pacing_stays_disabled_whatever_the_key_count() -> None:
    from scripts.benchmark.mtrag_run import budget_for

    assert budget_for(tokens_per_minute=0, keys=5).tokens_per_minute == 0
