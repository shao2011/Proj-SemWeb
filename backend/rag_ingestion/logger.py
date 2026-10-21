"""Logging setup.

One entry point, `configure_logging`, called once from `api/app.py`. Settings
arrive through `RuntimeConfig` rather than being read here, so `config.py` stays
the single place the environment is touched.

Two things worth knowing before raising a level:

* `httpcore` is pinned below DEBUG on purpose. At DEBUG it can log raw
  connection bytes, and those include the `Authorization` header, so a provider
  key would end up in the log file. `httpx` at DEBUG gives one line per request,
  which is the useful part, without the headers.
* SQLAlchemy statement logging is its own switch. `sqlalchemy.engine` at INFO
  prints statements and bound parameters; at DEBUG it also prints every result
  row, which buries everything else.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TextIO

#: Marks the handlers this module owns, so a second call replaces its own work
#: instead of stacking another handler and printing every line twice. Uvicorn's
#: reloader imports the app module more than once.
_MARKER = "rag_ingestion"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Loggers whose own handlers have to be removed. Uvicorn installs handlers with
#: `propagate = False`, so without this its lines keep their own format and none
#: of them reach a log file.
_ADOPTED = ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi")

#: Kept below DEBUG whatever the root level is. See the module docstring.
_CAPPED = {"httpcore": logging.INFO, "urllib3": logging.INFO}

DEFAULT_LEVEL = "DEBUG"


def resolve_level(name: str | None) -> int:
    """Level from a name, falling back to DEBUG on anything unrecognised.

    A typo in `LOG_LEVEL` must not stop the process. Debugging is the reason
    this exists, so the fallback is the loud end rather than the quiet one.
    """
    if not name:
        return logging.getLevelNamesMapping()[DEFAULT_LEVEL]
    resolved = logging.getLevelNamesMapping().get(name.strip().upper())
    if resolved is None:
        logging.getLogger(__name__).warning(
            "Unknown log level %r; using %s", name, DEFAULT_LEVEL
        )
        return logging.getLevelNamesMapping()[DEFAULT_LEVEL]
    return resolved


def _owned(handler: logging.Handler) -> bool:
    return getattr(handler, "_rag_owner", None) == _MARKER


def _claim(handler: logging.Handler, level: int) -> logging.Handler:
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    handler.setLevel(level)
    handler._rag_owner = _MARKER  # type: ignore[attr-defined]
    return handler


def configure_logging(
    *,
    level: str | int | None = None,
    log_file: str | Path | None = None,
    log_sql: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Install handlers on the root logger and return the level used.

    Idempotent: handlers this module added are removed first, so calling it
    again re-reads the settings rather than doubling every line.
    """
    numeric = level if isinstance(level, int) else resolve_level(level)
    root = logging.getLogger()

    for handler in [handler for handler in root.handlers if _owned(handler)]:
        root.removeHandler(handler)
        handler.close()

    root.setLevel(numeric)
    root.addHandler(_claim(logging.StreamHandler(stream or sys.stderr), numeric))

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotating rather than plain: at DEBUG this grows fast, and a debug aid
        # that fills the disk is its own outage.
        root.addHandler(
            _claim(
                logging.handlers.RotatingFileHandler(
                    path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
                ),
                numeric,
            )
        )

    for name in _ADOPTED:
        adopted = logging.getLogger(name)
        adopted.handlers.clear()
        adopted.propagate = True
        adopted.setLevel(numeric)

    for name, ceiling in _CAPPED.items():
        logging.getLogger(name).setLevel(max(numeric, ceiling))

    # Off unless asked for: statement logging is useful and voluminous, and
    # mixing it into a default run hides everything else.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if log_sql else logging.WARNING
    )
    return numeric


def get_logger(name: str) -> logging.Logger:
    """Module logger. A thin wrapper so callers do not import `logging`."""
    return logging.getLogger(name)
