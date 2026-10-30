"""Every experimental setting, in one place. The experiments read them from here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from eval import REPO_ROOT

DATA_ROOT = REPO_ROOT / "data"
REQUESTA_DIR = DATA_ROOT / "REQuestA"
REQUESTA_XLSX = REQUESTA_DIR / "REQuestA-Cleaned-Dataset.xlsx"
REQUESTA_PDF_DIR = REQUESTA_DIR / "pdf"
LOCOMO_JSON = DATA_ROOT / "LoCoMo" / "locomo10.json"
LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
RESULTS_DIR = Path(os.environ.get("EVAL_RESULTS_DIR", REPO_ROOT / "eval" / "results"))


@dataclass(frozen=True)
class AnswerModel:
    """One answer model: its catalog id, the gateway that serves it, its display name."""

    model_id: str
    router: str
    display_name: str


# Models: two open-weight models, temperature 0.
ANSWER_MODELS = (
    AnswerModel("nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter", "Nemotron~3 Ultra"),
    AnswerModel("tencent/hy4-preview-free", "orcarouter", "Hy4"),
)
TEMPERATURE = 0.0
# query rewriting and summaries use the same answer model.
HELPERS_USE_ANSWER_MODEL = True

# Embedding and reranking in all experiments.
EMBEDDING_MODEL = "jina-embeddings-v4"
RERANK_MODEL = "jina-reranker-v3"

# Answer labels: GPT-4o judges a random sample per experiment.
JUDGE_MODEL = "openai/gpt-4o"
JUDGE_ROUTER = "openrouter"
JUDGE_SAMPLE_SIZE = 50
SAMPLE_SEED = 2026

# Retrieval relevance: a chunk is relevant if it holds this share of the
# gold passage's tokens; Hit@5 and MRR@10.
RELEVANCE_TOKEN_SHARE = 0.50
HIT_AT = 5
MRR_AT = 10
# The system's default `retrieval_limit` (api/schemas.py ThreadMessageRequest).
RETRIEVAL_LIMIT = 10

# E1 chunking.
FIXED_WINDOW_WORDS = 256  # C1
SEMANTIC_BREAK_PERCENTILE = 95  # C2
STRUCTURE_MIN_WORDS = 100  # C3 (system ChunkingConfig)
STRUCTURE_TARGET_WORDS = 256
STRUCTURE_MAX_WORDS = 512
LATE_CHUNKING_WINDOW_TOKENS = 32_000  # C4 (jina-embeddings-v4 input window)
AUTO_MERGE_TOKEN_BUDGET = 4_000  # C5 (system RetrievalConfig.token_budget)
E1_CONFIGS = ("C1", "C2", "C3", "C4", "C5")
# Only C5 runs Auto-Merge; the others are compared on plain ranked leaves.
AUTO_MERGE_CONFIGS = ("C5",)

# E2 long conversations.
HISTORY_WINDOW_MESSAGES = 100  # H2-H5: the opener plus the newest 100
RECURSIVE_SUMMARY_MAX_CHARS = 5_120  # H3: same budget as H5 (32 items x 160)
ITEMISED_SUMMARY_MAX_ITEMS = 32  # H5 (system defaults)
ITEMISED_SUMMARY_ITEM_CHARS = 160
FOLD_MAX_MESSAGES = 40  # system DEFAULT_SUMMARY_MAX_FOLD_MESSAGES
FOLD_MAX_CHARS = 32_000  # system DEFAULT_SUMMARY_MAX_FOLD_CHARS
HISTORY_RETRIEVAL_TOP_K = 10  # H4
MEMORY_TOP_K = 5  # LoCoMo-RAG, Mem0 and LangMem
E2_METHODS = ("H1", "H2", "H3", "H4", "H5", "LoCoMo-RAG", "Mem0", "LangMem")
LOCOMO_ANSWERED_CATEGORIES = (1, 2, 3, 4)
LOCOMO_ADVERSARIAL_CATEGORY = 5

# E3 changing requirements.
E3_EDIT_COUNT = 5
E3_SITUATIONS = ("S1", "S2", "S3")
