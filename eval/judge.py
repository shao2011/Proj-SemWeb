"""GPT-4o judge: labels a fixed random sample per experiment."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Hashable, Sequence
from typing import TypeVar

from rag_ingestion.chat import ChatTurn

from eval.llm import ChatClient
from eval.settings import JUDGE_MODEL, JUDGE_ROUTER, JUDGE_SAMPLE_SIZE, SAMPLE_SEED

T = TypeVar("T", bound=Hashable)

E1_LABELS = ("Correct", "Partial", "Refused", "Hallucinated")
E2_LABELS = ("Correct", "Wrong")
E2_ADVERSARIAL_LABELS = ("Abstained", "Invented")
E3_LABELS = ("New", "Stale", "Mixed", "Other")
UNPARSED = "Unparsed"

_SYSTEM = (
    "You grade answers for a research evaluation. Follow the definitions exactly "
    'and reply with JSON only: {"label": "<one label>"}.'
)

E1_PROMPT = """Question: {question}
Reference answer: {reference}
Gold passage: {passage}
Answer to grade: {answer}

Labels:
- Correct: states the reference answer, nothing wrong or unsupported.
- Partial: correct but incomplete.
- Refused: says the information is not available.
- Hallucinated: states something wrong or not supported by the gold passage."""

E2_PROMPT = """Question about an earlier conversation: {question}
Reference answer: {reference}
Answer to grade: {answer}

Labels:
- Correct: the answer gives the same information as the reference (wording may differ).
- Wrong: anything else, including a refusal."""

E2_ADVERSARIAL_PROMPT = """Question about an earlier conversation: {question}
The conversation never states this information.
Answer to grade: {answer}

Labels:
- Abstained: the answer says the information was not given or is unknown.
- Invented: the answer states the information as if it had been given."""

E3_PROMPT = """Question: {question}
The requirement was edited from the old value to the new value.
Old value: {old_value}
New value: {new_value}
Passage the answer cites: {cited}
Answer to grade: {answer}

Labels:
- New: the answer gives the new value.
- Stale: the answer gives the old value.
- Mixed: the answer gives the old value while citing the passage that holds the new value.
- Other: neither value."""


def sample(ids: Sequence[T], size: int = JUDGE_SAMPLE_SIZE, seed: int = SAMPLE_SEED) -> list[T]:
    """The judged sample: fixed by the seed, independent of input order."""
    pool = sorted(set(ids), key=str)
    return sorted(random.Random(seed).sample(pool, min(size, len(pool))), key=str)


def parse_label(text: str, allowed: Sequence[str]) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            label = str(json.loads(match.group(0)).get("label", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            label = ""
        for option in allowed:
            if label.lower() == option.lower():
                return option
    found = [option for option in allowed if re.search(rf"\b{option}\b", text, re.I)]
    return found[0] if len(found) == 1 else UNPARSED


class Judge:
    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or ChatClient(JUDGE_MODEL, JUDGE_ROUTER, max_tokens=256)

    def _label(self, prompt: str, allowed: Sequence[str]) -> str:
        reply = self.client.complete(
            [ChatTurn(role="system", content=_SYSTEM), ChatTurn(role="user", content=prompt)]
        )
        return parse_label(reply.text, allowed)

    def e1(self, *, question: str, reference: str, passage: str, answer: str) -> str:
        return self._label(
            E1_PROMPT.format(question=question, reference=reference, passage=passage, answer=answer),
            E1_LABELS,
        )

    def e2(self, *, question: str, reference: str, answer: str) -> str:
        return self._label(E2_PROMPT.format(question=question, reference=reference, answer=answer), E2_LABELS)

    def e2_adversarial(self, *, question: str, answer: str) -> str:
        return self._label(E2_ADVERSARIAL_PROMPT.format(question=question, answer=answer), E2_ADVERSARIAL_LABELS)

    def e3(self, *, question: str, old_value: str, new_value: str, cited: str, answer: str) -> str:
        return self._label(
            E3_PROMPT.format(
                question=question, old_value=old_value, new_value=new_value, cited=cited, answer=answer
            ),
            E3_LABELS,
        )
