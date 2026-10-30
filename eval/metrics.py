"""Lexical answer metrics, retrieval relevance, Hit@k and MRR@k.

F1 follows LoCoMo's official `task_eval/evaluation.py` (normalisation, Porter
stemming, the per-category rules). BLEU-1 is NLTK's sentence BLEU with unigram
weights over the same normalised words, as in the Mem0 evaluation.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from collections.abc import Sequence

from nltk.stem import PorterStemmer
from nltk.translate.bleu_score import SmoothingFunction, sentence_bleu

from eval.settings import RELEVANCE_TOKEN_SHARE

_stemmer = PorterStemmer()
_smoothing = SmoothingFunction().method1
_CITE = re.compile(r"\[cite:\d+\]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def clean_answer(text: str) -> str:
    """The answer a reader sees: no citation markers, no hidden used-marker."""
    return _HTML_COMMENT.sub("", _CITE.sub("", text)).strip()


def normalize_answer(text: str) -> str:
    text = text.replace(",", "").lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the|and)\b", " ", text)
    return " ".join(text.split())


def f1_score(prediction: str, reference: str) -> float:
    predicted = [_stemmer.stem(word) for word in normalize_answer(prediction).split()]
    expected = [_stemmer.stem(word) for word in normalize_answer(reference).split()]
    common = Counter(predicted) & Counter(expected)
    same = sum(common.values())
    if same == 0:
        return 0.0
    precision = same / len(predicted)
    recall = same / len(expected)
    return 2 * precision * recall / (precision + recall)


def _multi_answer_f1(prediction: str, reference: str) -> float:
    predictions = [part.strip() for part in prediction.split(",")]
    references = [part.strip() for part in reference.split(",")]
    best = [max(f1_score(p, r) for p in predictions) for r in references]
    return sum(best) / len(best)


def locomo_f1(prediction: str, reference: str, *, category: int) -> float:
    """LoCoMo's scoring for the answered categories (1 to 4)."""
    if category == 1:
        return _multi_answer_f1(prediction, reference)
    if category == 3:
        reference = reference.split(";")[0].strip()
    if category in (2, 3, 4):
        return f1_score(prediction, reference)
    raise ValueError(f"Category {category} has no reference answer to score")


def bleu1(prediction: str, reference: str) -> float:
    predicted = normalize_answer(prediction).split()
    expected = normalize_answer(reference).split()
    if not predicted or not expected:
        return 0.0
    return float(
        sentence_bleu([expected], predicted, weights=(1, 0, 0, 0), smoothing_function=_smoothing)
    )


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def is_relevant(chunk_text: str, gold_passage: str) -> bool:
    """A chunk counts as relevant when it holds enough of the gold passage."""
    gold = Counter(_word_tokens(gold_passage))
    if not gold:
        return False
    held = sum((Counter(_word_tokens(chunk_text)) & gold).values())
    return held / sum(gold.values()) >= RELEVANCE_TOKEN_SHARE


def hit_at(relevant_flags: Sequence[bool], k: int) -> float:
    return 1.0 if any(relevant_flags[:k]) else 0.0


def mrr_at(relevant_flags: Sequence[bool], k: int) -> float:
    for rank, relevant in enumerate(relevant_flags[:k], start=1):
        if relevant:
            return 1.0 / rank
    return 0.0
