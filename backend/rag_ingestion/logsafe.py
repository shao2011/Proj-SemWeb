"""Making client-chosen text safe to put in a log line."""

from __future__ import annotations

import unicodedata


def loggable(text: str) -> str:
    """Replace control, format and line/paragraph separator characters with `?`.

    Filenames come from the client, and a bare LF survives multipart parsing,
    so logging one raw lets an upload write fake log lines.
    """
    return "".join(
        "?"
        if unicodedata.category(character)[0] == "C"
        or unicodedata.category(character) in ("Zl", "Zp")
        else character
        for character in text
    )
