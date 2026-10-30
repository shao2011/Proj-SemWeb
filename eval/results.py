"""Append-only JSON Lines results, so an interrupted run resumes where it
stopped instead of paying for the same calls again."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path


class ResultStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self._rows[record["key"]] = record["row"]

    @staticmethod
    def _key(parts: tuple) -> str:
        return json.dumps(list(parts), ensure_ascii=False)

    def has(self, parts: tuple) -> bool:
        return self._key(parts) in self._rows

    def put(self, parts: tuple, row: dict) -> None:
        key = self._key(parts)
        self._rows[key] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "row": row}, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict]:
        return list(self._rows.values())


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def summarise(rows: list[dict]) -> dict:
    """Table cells: means of numeric metrics and label shares (in %) per model."""
    summary: dict = {}
    retrieval = [row for row in rows if row.get("kind") == "retrieval"]
    if retrieval:
        summary["retrieval"] = {
            name: _mean(row[name] for row in retrieval) for name in ("hit", "mrr", "context_tokens")
        }
        summary["retrieval"]["questions"] = len(retrieval)
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("kind") == "answer":
            by_model[row["model"]].append(row)
    for model, answers in by_model.items():
        cell: dict = {"answers": len(answers)}
        for name in ("f1", "bleu1", "prompt_tokens", "seconds"):
            present = [row[name] for row in answers if row.get(name) is not None]
            if present:
                cell[name] = _mean(present)
        for field in ("label", "adversarial_label"):
            labels = [row[field] for row in answers if field in row]
            if labels:
                counts = Counter(labels)
                cell[field] = {k: round(100 * v / len(labels), 2) for k, v in sorted(counts.items())}
                cell[f"{field}_n"] = len(labels)
        summary[model] = cell
    return summary
