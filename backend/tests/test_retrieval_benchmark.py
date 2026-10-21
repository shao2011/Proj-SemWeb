from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.benchmark import retrieval


class FakeMteb:
    def __init__(self) -> None:
        self.task_calls: list[tuple[str, dict[str, Any]]] = []
        self.model_calls: list[tuple[str, dict[str, Any]]] = []
        self.cache_paths: list[str] = []
        self.evaluate_calls: list[dict[str, Any]] = []

    def get_task(self, name: str, **kwargs: Any) -> str:
        self.task_calls.append((name, kwargs))
        return f"task:{name}"

    def get_model(self, name: str, **kwargs: Any) -> str:
        self.model_calls.append((name, kwargs))
        return f"model:{name}"

    def ResultCache(self, *, cache_path: str) -> str:  # noqa: N802
        self.cache_paths.append(cache_path)
        return f"cache:{cache_path}"

    def evaluate(self, model: object, **kwargs: Any) -> list[str]:
        self.evaluate_calls.append({"model": model, **kwargs})
        return ["result"]


def test_default_suite_builds_five_tasks_and_limits_miracl_to_english() -> None:
    options = retrieval.parse_args(["--model", "example/model"])
    mteb = FakeMteb()

    tasks = retrieval.build_tasks(
        mteb,
        task_aliases=options.tasks,
        miracl_languages=options.miracl_languages,
    )

    assert len(tasks) == 5
    assert mteb.task_calls == [
        ("AILACasedocs", {}),
        ("AILAStatutes", {}),
        ("LegalQuAD", {}),
        ("LegalSummarization", {}),
        ("MIRACLRetrievalHardNegatives", {"languages": ["eng"]}),
    ]


def test_selected_tasks_use_stable_order_and_requested_miracl_languages() -> None:
    options = retrieval.parse_args(
        [
            "--model",
            "example/model",
            "--task",
            "mteb/miracl-hard-negatives",
            "mteb/AILA_casedocs",
            "--miracl-language",
            "deu",
            "eng",
        ]
    )
    mteb = FakeMteb()

    retrieval.build_tasks(
        mteb,
        task_aliases=options.tasks,
        miracl_languages=options.miracl_languages,
    )

    assert mteb.task_calls == [
        ("AILACasedocs", {}),
        ("MIRACLRetrievalHardNegatives", {"languages": ["deu", "eng"]}),
    ]


def test_miracl_languages_do_not_change_fixed_language_tasks() -> None:
    options = retrieval.parse_args(
        [
            "--model",
            "example/model",
            "--task",
            "mteb/LegalQuAD",
            "--miracl-language",
            "zho",
        ]
    )
    mteb = FakeMteb()

    retrieval.build_tasks(
        mteb,
        task_aliases=options.tasks,
        miracl_languages=options.miracl_languages,
    )

    assert mteb.task_calls == [("LegalQuAD", {})]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--model", "example/model", "--task", "unknown"], "invalid choice"),
        (
            [
                "--model",
                "example/model",
                "--miracl-language",
                "vie",
            ],
            "invalid choice",
        ),
        (["--model", "example/model", "--batch-size", "0"], "positive integer"),
    ],
)
def test_invalid_arguments_are_rejected_before_mteb_load(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        retrieval.parse_args(arguments)

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_run_benchmark_passes_model_cache_tasks_and_batch_size(tmp_path: Path) -> None:
    output = tmp_path / "results"
    options = retrieval.parse_args(
        [
            "--model",
            "example/model",
            "--task",
            "mteb/LegalQuAD",
            "--device",
            "cuda",
            "--batch-size",
            "8",
            "--output",
            str(output),
        ]
    )
    mteb = FakeMteb()

    result = retrieval.run_benchmark(options, mteb=mteb)

    assert result == ["result"]
    assert output.is_dir()
    assert mteb.model_calls == [("example/model", {"device": "cuda"})]
    assert mteb.cache_paths == [str(output.resolve())]
    assert mteb.evaluate_calls == [
        {
            "model": "model:example/model",
            "tasks": ["task:LegalQuAD"],
            "cache": f"cache:{output.resolve()}",
            "encode_kwargs": {"batch_size": 8},
        }
    ]


def test_missing_dependency_returns_install_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_mteb() -> object:
        raise retrieval.BenchmarkDependencyError

    exit_code = retrieval.main(
        ["--model", "example/model"],
        mteb_loader=missing_mteb,
    )

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "pip install -e \".[benchmark]\"" in error
    assert "Traceback" not in error


def test_help_does_not_load_mteb(capsys: pytest.CaptureFixture[str]) -> None:
    def unexpected_load() -> object:
        raise AssertionError("MTEB must not load for --help")

    with pytest.raises(SystemExit) as error:
        retrieval.main(["--help"], mteb_loader=unexpected_load)

    assert error.value.code == 0
    assert "MTEB retrieval benchmark" in capsys.readouterr().out
