import json
import zipfile

import pytest

from scripts.benchmark.mtrag_data import (
    Passage,
    read_corpus,
    read_qrels,
    read_queries,
    strip_turn_markers,
)
from scripts.benchmark.mtrag_metrics import evaluate, ndcg_at_k, recall_at_k


# --- metrics ----------------------------------------------------------------


def test_recall_counts_relevant_passages_found_in_the_top_k() -> None:
    ranked = ["a", "b", "c", "d"]
    relevant = {"a": 1, "d": 1}

    assert recall_at_k(ranked, relevant, 1) == pytest.approx(0.5)
    assert recall_at_k(ranked, relevant, 4) == pytest.approx(1.0)


def test_recall_is_zero_when_nothing_relevant_was_retrieved() -> None:
    assert recall_at_k(["x", "y"], {"a": 1}, 10) == 0.0


def test_recall_with_no_relevant_passages_is_zero_not_a_crash() -> None:
    """A query whose qrels are all zero-scored reaches here with an empty dict."""
    assert recall_at_k(["a"], {}, 10) == 0.0


def test_ndcg_rewards_putting_the_relevant_passage_first() -> None:
    relevant = {"a": 1}

    first = ndcg_at_k(["a", "b", "c"], relevant, 10)
    third = ndcg_at_k(["b", "c", "a"], relevant, 10)

    assert first == pytest.approx(1.0)
    assert 0 < third < first


def test_ndcg_is_one_when_the_ideal_ranking_is_returned() -> None:
    relevant = {"a": 1, "b": 1}

    assert ndcg_at_k(["a", "b", "z"], relevant, 10) == pytest.approx(1.0)


def test_ndcg_uses_graded_relevance_not_just_a_hit_flag() -> None:
    """A 2 above a 1 must score higher than the reverse, or graded qrels are
    being silently flattened into binary ones."""
    graded = {"a": 2, "b": 1}

    assert ndcg_at_k(["a", "b"], graded, 10) > ndcg_at_k(["b", "a"], graded, 10)


def test_ndcg_beyond_k_is_ignored() -> None:
    relevant = {"a": 1}

    assert ndcg_at_k(["x", "y", "a"], relevant, 2) == 0.0


def test_evaluate_averages_over_queries_and_reports_every_cutoff() -> None:
    qrels = {"q1": {"a": 1}, "q2": {"b": 1}}
    runs = {"q1": ["a", "z"], "q2": ["z", "b"]}

    scores = evaluate(runs, qrels, cutoffs=(1, 5))

    assert scores["recall@1"] == pytest.approx(0.5)
    assert scores["recall@5"] == pytest.approx(1.0)
    assert scores["ndcg@1"] == pytest.approx(0.5)
    assert scores["queries"] == 2


def test_evaluate_scores_a_query_with_no_results_as_zero_rather_than_skipping() -> None:
    """Dropping unanswered queries would quietly raise the average, which is
    the difference between "our retriever missed" and "we did not ask"."""
    qrels = {"q1": {"a": 1}, "q2": {"b": 1}}

    scores = evaluate({"q1": ["a"]}, qrels, cutoffs=(10,))

    assert scores["queries"] == 2
    assert scores["recall@10"] == pytest.approx(0.5)


def test_evaluate_ignores_a_run_for_a_query_that_has_no_qrels() -> None:
    scores = evaluate({"ghost": ["a"]}, {"q1": {"a": 1}}, cutoffs=(10,))

    assert scores["queries"] == 1


# --- data loading -----------------------------------------------------------


def test_turn_markers_are_stripped_but_the_turns_are_kept() -> None:
    text = "|user|: where do the cardinals play\n|user|: do they play abroad?"

    assert strip_turn_markers(text) == (
        "where do the cardinals play\ndo they play abroad?"
    )


def test_plain_text_survives_stripping_unchanged() -> None:
    assert strip_turn_markers("no markers here") == "no markers here"


def test_queries_load_with_markers_already_removed(tmp_path) -> None:
    path = tmp_path / "lastturn.jsonl"
    path.write_text(
        json.dumps({"_id": "c<::>2", "text": "|user|: hello"}) + "\n\n",
        encoding="utf-8",
    )

    queries = read_queries(path)

    assert len(queries) == 1
    assert queries[0].query_id == "c<::>2"
    assert queries[0].text == "hello"


def test_qrels_drop_zero_scored_rows(tmp_path) -> None:
    """Judged-irrelevant is not the same as relevant, and counting it would
    inflate the recall denominator."""
    path = tmp_path / "qrels.tsv"
    path.write_text(
        "query-id\tcorpus-id\tscore\nq1\tp1\t1\nq1\tp2\t0\nq2\tp3\t2\n",
        encoding="utf-8",
    )

    qrels = read_qrels(path)

    assert qrels == {"q1": {"p1": 1}, "q2": {"p3": 2}}


def test_corpus_streams_passages_out_of_the_zip(tmp_path) -> None:
    archive = tmp_path / "fiqa.jsonl.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "fiqa.jsonl",
            json.dumps({"_id": "10171-0-2129", "text": "body", "title": "t"}) + "\n\n",
        )

    passages = list(read_corpus(archive))

    assert passages == [Passage(passage_id="10171-0-2129", text="body", title="t")]
