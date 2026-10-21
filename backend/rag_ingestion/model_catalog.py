"""The chat-model catalog and the context-gauge arithmetic.

The catalog is a bundled JSON file rather than env vars because it is a list
of structured records, and rather than a database table because it changes on
deploy, not at runtime. `load_catalog` reads it once per process.

All gauge math lives here so the server computes the percentage the frontend
renders; the client never re-derives budgets from raw token counts.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rag_ingestion.schemas.model_catalog import ModelCatalog, ModelSpec

_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "models.json"

#: Prompt overhead the char-count never sees: role tokens, tool schemas, and
#: provider delimiters. Subtracted from the usable budget rather than added to
#: every estimate.
SAFETY_FLOOR_TOKENS = 2_000


@lru_cache(maxsize=1)
def load_catalog(path: str | None = None) -> ModelCatalog:
    raw = json.loads(Path(path or _CATALOG_PATH).read_text(encoding="utf-8"))
    return ModelCatalog.model_validate(raw)


def usable_tokens(spec: ModelSpec) -> int:
    """The input budget one turn can actually spend.

    `max_tokens` is a hard cap the provider enforces on thinking + response
    together, and it shares the window with the input, so the whole output
    reservation comes off the top. What remains is the gauge's denominator
    and the summary trigger's threshold.
    """
    return max(1, spec.context_tokens - spec.max_output_tokens - SAFETY_FLOOR_TOKENS)


def context_percent(prompt_tokens: int | None, spec: ModelSpec) -> int | None:
    """Server-computed gauge value: 0-100, or None before any measurement.

    Clamped at 100 rather than allowed to overflow: after a switch from a
    long-context model, the recorded prompt can exceed the new model's whole
    budget, and the gauge should read "full", not break the UI with 512%.
    """
    if prompt_tokens is None:
        return None
    return min(100, max(0, round(100 * prompt_tokens / usable_tokens(spec))))
