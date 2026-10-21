"""Fetch and load the MTRAG multi-turn RAG benchmark.

MTRAG (IBM, Apache-2.0) ships in BEIR format: a passage corpus, per-domain
qrels, and three query variants that share one id per conversation turn.

    lastturn   the final user message alone      the floor
    rewrite    a human-written standalone query  the ceiling
    questions  every user turn concatenated      the raw context

Those three are the whole reason this benchmark is worth wiring up here: they
isolate query rewriting on identical qrels, which is exactly what
`UnderstandMessage` does. Scoring our own rewrite against the same labels
places it between the floor and the ceiling instead of leaving it unmeasured.

Corpora come from `passage_level/` rather than `document_level/`, because the
qrels reference passage ids with chunk offsets already baked in
(`10171-0-2129`). Verified against the published files: 479 of 479 distinct
FiQA qrels ids resolve directly, so nothing has to reconstruct an id.
"""

from __future__ import annotations

import csv
import json
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_RAW = "https://raw.githubusercontent.com/IBM/mt-rag-benchmark/main"
_BLOB = "https://github.com/IBM/mt-rag-benchmark/raw/main"

DOMAINS = ("clapnq", "cloud", "fiqa", "govt")
QUERY_VARIANTS = ("lastturn", "rewrite", "questions")

#: Every user turn carries this marker in the query files. Stripped before the
#: text reaches a retriever: it is MTRAG's transcript notation, not something a
#: user would ever type.
_TURN_MARKER = "|user|:"

DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "mtrag"


@dataclass(frozen=True, slots=True)
class Passage:
    passage_id: str
    text: str
    title: str


@dataclass(frozen=True, slots=True)
class Query:
    query_id: str
    text: str


def corpus_url(domain: str) -> str:
    return f"{_BLOB}/corpora/passage_level/{domain}.jsonl.zip"


def query_url(domain: str, variant: str) -> str:
    return f"{_RAW}/mtrag-human/retrieval_tasks/{domain}/{domain}_{variant}.jsonl"


def qrels_url(domain: str) -> str:
    return f"{_RAW}/mtrag-human/retrieval_tasks/{domain}/qrels/dev.tsv"


def _download(url: str, target: Path) -> bool:
    """Fetch one file. Returns True when it was downloaded, False when cached.

    Skipping an existing file keeps the corpora (about 100 MB zipped) from
    being pulled again on every run.
    """
    if target.exists() and target.stat().st_size > 0:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
        partial.write_bytes(response.read())
    # Rename only once the body is complete, so an interrupted run cannot leave
    # a truncated file that later looks cached.
    partial.replace(target)
    return True


def fetch(
    domains: tuple[str, ...] = DOMAINS,
    *,
    root: Path = DEFAULT_ROOT,
) -> list[tuple[Path, bool]]:
    """Download corpora, queries and qrels for the given domains."""
    results: list[tuple[Path, bool]] = []
    for domain in domains:
        targets = {
            root / "corpora" / f"{domain}.jsonl.zip": corpus_url(domain),
            root / domain / "qrels.tsv": qrels_url(domain),
        }
        for variant in QUERY_VARIANTS:
            targets[root / domain / f"{variant}.jsonl"] = query_url(domain, variant)
        for target, url in targets.items():
            results.append((target, _download(url, target)))
    return results


def read_corpus(path: Path) -> Iterator[Passage]:
    """Stream passages out of a zipped BEIR corpus.

    Streamed rather than returned as a list: ClapNQ alone is 43 MB zipped and
    the caller indexes in batches anyway.
    """
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                yield Passage(
                    passage_id=row["_id"],
                    text=row.get("text", ""),
                    title=row.get("title", ""),
                )


def strip_turn_markers(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    cleaned = [
        line.removeprefix(_TURN_MARKER).strip() if line.startswith(_TURN_MARKER) else line
        for line in lines
    ]
    return "\n".join(part for part in cleaned if part)


def read_queries(path: Path) -> list[Query]:
    queries: list[Query] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        queries.append(
            Query(query_id=row["_id"], text=strip_turn_markers(row["text"]))
        )
    return queries


def read_qrels(path: Path) -> dict[str, dict[str, int]]:
    """BEIR qrels: `{query_id: {passage_id: relevance}}`.

    Rows scoring 0 are dropped. They mark judged-and-irrelevant, which every
    metric here treats the same as unjudged, and keeping them would inflate the
    denominator of recall.
    """
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            score = int(row["score"])
            if score <= 0:
                continue
            qrels.setdefault(row["query-id"], {})[row["corpus-id"]] = score
    return qrels
