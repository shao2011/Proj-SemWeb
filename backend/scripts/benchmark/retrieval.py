from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TaskSpec:
    alias: str
    mteb_name: str
    multilingual: bool = False


TASK_SPECS = (
    TaskSpec("mteb/AILA_casedocs", "AILACasedocs"),
    TaskSpec("mteb/AILA_statutes", "AILAStatutes"),
    TaskSpec("mteb/LegalQuAD", "LegalQuAD"),
    TaskSpec("mteb/legal_summarization", "LegalSummarization"),
    TaskSpec(
        "mteb/miracl-hard-negatives",
        "MIRACLRetrievalHardNegatives",
        multilingual=True,
    ),
)

SUPPORTED_MIRACL_LANGUAGES = (
    "ara",
    "ben",
    "deu",
    "eng",
    "fas",
    "fin",
    "fra",
    "hin",
    "ind",
    "jpn",
    "kor",
    "rus",
    "spa",
    "swa",
    "tel",
    "tha",
    "yor",
    "zho",
)


@dataclass(frozen=True)
class BenchmarkOptions:
    model: str
    tasks: tuple[str, ...]
    miracl_languages: tuple[str, ...]
    device: str
    batch_size: int
    output: Path


class BenchmarkDependencyError(RuntimeError):
    pass


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="MTEB retrieval benchmark for legal and MIRACL datasets."
    )
    argument_parser.add_argument(
        "--model",
        required=True,
        help="MTEB or Hugging Face embedding model name.",
    )
    argument_parser.add_argument(
        "--task",
        nargs="+",
        choices=[spec.alias for spec in TASK_SPECS],
        help="Dataset aliases to run. Defaults to all five datasets.",
    )
    argument_parser.add_argument(
        "--miracl-language",
        nargs="+",
        choices=SUPPORTED_MIRACL_LANGUAGES,
        default=["eng"],
        help="ISO 639-3 MIRACL languages. Defaults to eng.",
    )
    argument_parser.add_argument(
        "--device",
        default="cpu",
        help="Model device accepted by MTEB, for example cpu or cuda.",
    )
    argument_parser.add_argument(
        "--batch-size",
        type=_positive_integer,
        default=32,
        help="Embedding batch size. Defaults to 32.",
    )
    argument_parser.add_argument(
        "--output",
        type=Path,
        default=Path("backend/scripts/benchmark/results"),
        help="MTEB result cache directory.",
    )
    return argument_parser


def parse_args(arguments: Sequence[str] | None = None) -> BenchmarkOptions:
    parsed = parser().parse_args(arguments)
    selected = set(parsed.task or (spec.alias for spec in TASK_SPECS))
    tasks = tuple(spec.alias for spec in TASK_SPECS if spec.alias in selected)
    return BenchmarkOptions(
        model=parsed.model,
        tasks=tasks,
        miracl_languages=tuple(parsed.miracl_language),
        device=parsed.device,
        batch_size=parsed.batch_size,
        output=parsed.output,
    )


def build_tasks(
    mteb: Any,
    *,
    task_aliases: Sequence[str],
    miracl_languages: Sequence[str],
) -> list[Any]:
    selected = set(task_aliases)
    tasks: list[Any] = []
    for spec in TASK_SPECS:
        if spec.alias not in selected:
            continue
        if spec.multilingual:
            tasks.append(
                mteb.get_task(spec.mteb_name, languages=list(miracl_languages))
            )
        else:
            tasks.append(mteb.get_task(spec.mteb_name))
    return tasks


def load_mteb() -> Any:
    try:
        return import_module("mteb")
    except ModuleNotFoundError as error:
        raise BenchmarkDependencyError from error


def run_benchmark(options: BenchmarkOptions, *, mteb: Any) -> Any:
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks(
        mteb,
        task_aliases=options.tasks,
        miracl_languages=options.miracl_languages,
    )
    model = mteb.get_model(options.model, device=options.device)
    cache = mteb.ResultCache(cache_path=str(output))
    return mteb.evaluate(
        model,
        tasks=tasks,
        cache=cache,
        encode_kwargs={"batch_size": options.batch_size},
    )


def main(
    arguments: Sequence[str] | None = None,
    *,
    mteb_loader: Callable[[], Any] = load_mteb,
) -> int:
    options = parse_args(arguments)
    try:
        mteb = mteb_loader()
    except BenchmarkDependencyError:
        print(
            'MTEB is not installed; run: pip install -e ".[benchmark]"',
            file=sys.stderr,
        )
        return 2
    run_benchmark(options, mteb=mteb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
