"""Ceilings a file has to fit under before a parser spends real work on it."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from rag_ingestion.config import IngestionLimits

#: Parts smaller than this skip the ratio check. A few KB of blank styling
#: compresses past 100x in ordinary files, and a part this small cannot hurt;
#: the total-size cap still counts it.
_RATIO_GRACE_BYTES = 1024 * 1024


class DocumentLimitError(ValueError):
    """A file over an ingestion limit. The message is safe to show the uploader."""


def check_zip(path: Path, limits: IngestionLimits) -> None:
    """Refuse a zip bomb from its central directory, before anything inflates.

    The declared sizes can be trusted as a ceiling: `zipfile` stops inflating
    an entry at its declared `file_size` and fails the CRC if the data ran
    on, and openpyxl reads through the same module.
    """
    with ZipFile(path) as archive:
        entries = archive.infolist()
    if len(entries) > limits.max_zip_entries:
        raise DocumentLimitError(
            f"This file holds more than the {limits.max_zip_entries:,}-entry limit"
        )
    if sum(entry.file_size for entry in entries) > limits.max_unzipped_bytes:
        raise DocumentLimitError(
            f"This file unpacks to more than the {limits.max_unzipped_bytes:,}-byte limit"
        )
    for entry in entries:
        if (
            entry.file_size >= _RATIO_GRACE_BYTES
            and entry.file_size > entry.compress_size * limits.max_zip_ratio
        ):
            raise DocumentLimitError(
                f"A part of this file expands more than {limits.max_zip_ratio}× "
                "when unpacked and was refused as a possible zip bomb"
            )
