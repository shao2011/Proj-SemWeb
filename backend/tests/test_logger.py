import io
import logging
from pathlib import Path

import pytest

from rag_ingestion.config import RuntimeConfig
from rag_ingestion.logger import DEFAULT_LEVEL, configure_logging, resolve_level


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Logging is process-global, so each test puts it back the way it was."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


# --- level ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("  Warning  ", logging.WARNING),
        ("ERROR", logging.ERROR),
    ],
)
def test_level_names_are_case_and_space_insensitive(name: str, expected: int) -> None:
    assert resolve_level(name) == expected


@pytest.mark.parametrize("name", ["", None, "LOUD", "12"])
def test_an_unusable_level_falls_back_to_the_default(name) -> None:
    """A typo must not stop the process, and the fallback is the loud end.

    Debugging is the reason this module exists, so failing quiet would hide the
    very thing the operator was trying to see.
    """
    assert resolve_level(name) == logging.getLevelNamesMapping()[DEFAULT_LEVEL]


# --- handlers ---------------------------------------------------------------


def test_configuring_twice_does_not_duplicate_output() -> None:
    """Uvicorn's reloader imports the app module more than once."""
    first = io.StringIO()
    configure_logging(level="INFO", stream=first)
    second = io.StringIO()
    configure_logging(level="INFO", stream=second)

    logging.getLogger("rag_ingestion.probe").info("once")

    assert second.getvalue().count("once") == 1
    assert "once" not in first.getvalue()


def test_records_carry_the_time_level_and_logger_name() -> None:
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)

    logging.getLogger("rag_ingestion.probe").warning("something happened")

    written = stream.getvalue()
    assert "WARNING" in written
    assert "rag_ingestion.probe" in written
    assert "something happened" in written


def test_uvicorn_output_is_adopted_rather_than_left_on_its_own_handler() -> None:
    uvicorn = logging.getLogger("uvicorn.error")
    uvicorn.addHandler(logging.StreamHandler(io.StringIO()))
    uvicorn.propagate = False
    stream = io.StringIO()

    configure_logging(level="INFO", stream=stream)
    uvicorn.info("server started")

    assert uvicorn.handlers == []
    assert uvicorn.propagate is True
    assert "server started" in stream.getvalue()


def test_httpcore_stays_above_debug_so_auth_headers_cannot_be_logged() -> None:
    """At DEBUG, httpcore can log raw connection bytes, headers included.

    That would put a provider API key in the log file, so the ceiling holds even
    when the root logger is turned all the way up.
    """
    configure_logging(level="DEBUG", stream=io.StringIO())

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpcore").level >= logging.INFO
    assert logging.getLogger("urllib3").level >= logging.INFO


def test_sql_logging_is_off_unless_asked_for() -> None:
    configure_logging(level="DEBUG", stream=io.StringIO())
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    configure_logging(level="DEBUG", stream=io.StringIO(), log_sql=True)
    assert logging.getLogger("sqlalchemy.engine").level == logging.INFO


def test_sql_logging_never_reaches_debug_where_it_prints_result_rows() -> None:
    configure_logging(level="DEBUG", stream=io.StringIO(), log_sql=True)

    assert logging.getLogger("sqlalchemy.engine").level > logging.DEBUG


# --- file sink --------------------------------------------------------------


def test_a_log_file_is_written_and_its_directory_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "api.log"

    configure_logging(level="INFO", stream=io.StringIO(), log_file=target)
    logging.getLogger("rag_ingestion.probe").info("to disk")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert target.is_file()
    assert "to disk" in target.read_text(encoding="utf-8")


def test_without_a_log_file_nothing_is_written(tmp_path: Path) -> None:
    configure_logging(level="INFO", stream=io.StringIO())

    assert list(tmp_path.iterdir()) == []


# --- configuration ----------------------------------------------------------


def test_settings_come_from_the_environment_through_runtime_config() -> None:
    config = RuntimeConfig.from_environment(
        {
            "QDRANT_URL": "http://localhost:6333",
            "QDRANT_COLLECTION_PREFIX": "rag",
            "LOG_LEVEL": "warning",
            "LOG_FILE": "logs/api.log",
            "LOG_SQL": "true",
        },
        read_env_file=False,
    )

    assert config.logging.level == "warning"
    assert config.logging.file == "logs/api.log"
    assert config.logging.sql is True
    assert resolve_level(config.logging.level) == logging.WARNING


def test_logging_defaults_to_debug_with_no_file_and_no_sql() -> None:
    config = RuntimeConfig.from_environment(
        {"QDRANT_URL": "http://localhost:6333", "QDRANT_COLLECTION_PREFIX": "rag"},
        read_env_file=False,
    )

    assert config.logging.level == DEFAULT_LEVEL
    assert config.logging.file is None
    assert config.logging.sql is False
