import pytest

from eval.metrics import (
    bleu1,
    clean_answer,
    f1_score,
    hit_at,
    is_relevant,
    locomo_f1,
    mrr_at,
)


def test_f1_follows_locomo_normalisation_and_stemming() -> None:
    # "the" and "and" are dropped, punctuation and case ignored, words stemmed.
    assert f1_score("The Systems, running", "system run") == pytest.approx(1.0)
    assert f1_score("completely unrelated", "within 5 seconds") == 0


def test_f1_partial_overlap() -> None:
    # prediction 4 tokens, reference 2, 2 shared: P=0.5, R=1.0
    assert f1_score("within 5 seconds always", "5 seconds") == pytest.approx(2 / 3)


def test_locomo_category_rules() -> None:
    # Category 1 splits comma-separated answers and averages the best matches.
    assert locomo_f1("Paris, London", "London, Paris", category=1) == pytest.approx(1.0)
    # Category 3 keeps only the part before ';'.
    assert locomo_f1("yes", "yes; because she said so", category=3) == pytest.approx(1.0)
    assert locomo_f1("7 May 2023", "7 May 2023", category=2) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        locomo_f1("x", "y", category=5)


def test_bleu1_is_clipped_unigram_precision_with_brevity_penalty() -> None:
    assert bleu1("within 5 seconds", "within 5 seconds") == pytest.approx(1.0)
    # Same words, half as long as the reference: brevity penalty exp(1 - 2) applies.
    assert bleu1("5 seconds", "within 5 seconds always") == pytest.approx(0.3679, abs=1e-3)
    assert bleu1("", "anything") == 0.0


def test_clean_answer_strips_citations_and_the_used_marker() -> None:
    raw = "It takes 5 seconds [cite:1][cite:2].<!--used:s1,s2-->"
    assert clean_answer(raw) == "It takes 5 seconds ."


def test_relevance_needs_half_of_the_gold_tokens() -> None:
    gold = "The AMSC Number will be assigned by the Director"
    assert is_relevant("... AMSC Number will be assigned by someone else ...", gold)
    assert not is_relevant("The Director", gold)


def test_hit_and_mrr() -> None:
    flags = [False, False, True, False]
    assert hit_at(flags, 5) == 1.0
    assert hit_at(flags, 2) == 0.0
    assert mrr_at(flags, 10) == pytest.approx(1 / 3)
    assert mrr_at([False] * 12 + [True], 10) == 0.0
