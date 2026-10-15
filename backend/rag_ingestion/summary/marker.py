"""Strip the used-marker out of an answer stream.

The answering model ends its reply with `<!--used:s3,s7-->`, naming the
summary items its answer actually leaned on. Counting that from the model is
the only reliable way to know: a paraphrase ("end of September" for an item
saying "30/9") carries the fact without sharing a single token, so string
matching against the answer under-counts exactly where it matters.

Two rules make it safe to run over a live stream:

* Nothing after a `<!--` is emitted until the closing `-->` decides what it
  was. SSE has no take-backs -- a delta the client already received cannot be
  retracted -- so the bytes are held, not emitted and regretted.
* An unterminated comment is dropped whole. A dangling `<!--` swallows
  everything after it in a markdown renderer, and the common way to get one
  is the user pressing stop mid-marker. Holding the bytes server-side also
  means an aborted turn needs no cleanup: they were never sent.
"""

from __future__ import annotations

import re

_OPEN = "<!--"
_CLOSE = "-->"

_MARKER = re.compile(r"<!--\s*used\s*:([^>]*)-->", re.IGNORECASE)
_ITEM_ID = re.compile(r"^s[1-9][0-9]*$")


def _partial_open(text: str) -> int:
    """How many trailing characters could be the start of `<!--`."""
    for size in range(len(_OPEN) - 1, 0, -1):
        if text.endswith(_OPEN[:size]):
            return size
    return 0


def _parse_ids(raw: str) -> list[str]:
    seen: list[str] = []
    for token in raw.replace(";", ",").split(","):
        candidate = token.strip().casefold()
        # "none" is the model saying the answer leaned on nothing, which is a
        # real signal; anything else unrecognized is a hallucinated id.
        if _ITEM_ID.match(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


class UsedMarkerFilter:
    """Feed stream chunks in, get user-safe text out.

    `saw_marker` distinguishes "the model said the answer used nothing"
    (`ids == []`) from "the model forgot the marker" (`ids is None`). Only the
    first is a measurement; the second is logged and ignored.
    """

    def __init__(self) -> None:
        self.ids: list[str] | None = None
        self.saw_marker = False
        #: Held-back text: either a possible `<!--` prefix or a comment whose
        #: closing `-->` has not arrived.
        self._pending = ""
        self._buffering = False

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        data = self._pending + chunk
        self._pending = ""
        while data:
            if self._buffering:
                end = data.find(_CLOSE)
                if end == -1:
                    self._pending = data
                    break
                comment, data = data[: end + len(_CLOSE)], data[end + len(_CLOSE) :]
                self._buffering = False
                match = _MARKER.fullmatch(comment.strip())
                if match is None:
                    out.append(comment)  # an ordinary comment; the user keeps it
                    continue
                self.saw_marker = True
                self.ids = _parse_ids(match.group(1))
                continue
            start = data.find(_OPEN)
            if start != -1:
                out.append(data[:start])
                data = data[start:]
                self._buffering = True
                continue
            keep = _partial_open(data)
            if keep:
                out.append(data[:-keep])
                self._pending = data[-keep:]
            else:
                out.append(data)
            break
        return "".join(out)

    def close(self) -> str:
        """Whatever is still safe to emit once the stream ends."""
        pending, self._pending = self._pending, ""
        if self._buffering:
            # Unterminated comment, most often a turn stopped mid-marker.
            self._buffering = False
            return ""
        return pending
