"""The used-marker filter that runs over the answer stream.

The answering model ends its reply with `<!--used:s3,s7-->`, naming the
summary items its answer actually leaned on. That marker must never reach the
user's screen, never reach the stored message, and never leave a dangling
`<!--` behind when a turn is cut short.
"""

from __future__ import annotations

from rag_ingestion.summary.marker import UsedMarkerFilter


def _run(chunks: list[str]) -> tuple[str, UsedMarkerFilter]:
    marker = UsedMarkerFilter()
    out = "".join(marker.feed(chunk) for chunk in chunks)
    return out + marker.close(), marker


class TestStripping:
    def test_a_marker_at_the_end_is_swallowed_and_parsed(self) -> None:
        text, marker = _run(["Deadline is 30/9.", "\n<!--used:s3,s7-->"])

        assert text == "Deadline is 30/9.\n"
        assert marker.ids == ["s3", "s7"]
        assert marker.saw_marker is True

    def test_a_marker_split_across_chunks_is_still_caught(self) -> None:
        """Tokens arrive in arbitrary pieces, so every prefix of the opener
        has to be held back rather than emitted and regretted."""
        text, marker = _run(["Answer.", "\n<", "!-", "-us", "ed:s", "1-", "->"])

        assert text == "Answer.\n"
        assert marker.ids == ["s1"]

    def test_an_empty_marker_is_a_real_signal_not_a_missing_one(self) -> None:
        """`none` says the answer leaned on nothing, which is different from
        the model forgetting the marker; only the latter is untrustworthy."""
        _, marker = _run(["Just chatting.<!--used:none-->"])

        assert marker.saw_marker is True
        assert marker.ids == []

    def test_no_marker_at_all_leaves_the_text_untouched(self) -> None:
        text, marker = _run(["Plain answer, no marker."])

        assert text == "Plain answer, no marker."
        assert marker.saw_marker is False
        assert marker.ids is None


class TestPassThrough:
    def test_an_ordinary_html_comment_is_not_eaten(self) -> None:
        """A model writing HTML in a fenced block must keep its comments."""
        text, marker = _run(["```html\n<!-- a real comment -->\n```"])

        assert text == "```html\n<!-- a real comment -->\n```"
        assert marker.saw_marker is False

    def test_a_comment_mid_answer_is_kept_and_a_later_marker_still_parses(
        self,
    ) -> None:
        text, marker = _run(["<!-- keep me -->text<!--used:s2-->"])

        assert text == "<!-- keep me -->text"
        assert marker.ids == ["s2"]

    def test_a_lone_angle_bracket_is_not_held_hostage(self) -> None:
        text, _ = _run(["2 < 3 and 4 > 1"])

        assert text == "2 < 3 and 4 > 1"


class TestTruncation:
    def test_a_marker_cut_off_mid_stream_is_swallowed_whole(self) -> None:
        """Stop pressed while the marker was being written. A dangling
        `<!--` would swallow everything after it in a markdown renderer, so
        the partial comment is dropped rather than flushed."""
        text, marker = _run(["Answer so far.", "\n<!--used:s3"])

        assert text == "Answer so far.\n"
        assert marker.saw_marker is False

    def test_an_unterminated_ordinary_comment_is_also_dropped(self) -> None:
        text, _ = _run(["Answer.", "<!-- never closed"])

        assert text == "Answer."

    def test_nothing_is_emitted_before_a_buffered_comment_resolves(self) -> None:
        """The bytes must be held back, not emitted-then-retracted: SSE has
        no take-backs once a delta reaches the client."""
        marker = UsedMarkerFilter()

        assert marker.feed("Answer.<!--us") == "Answer."
        assert marker.feed("ed:s1-->") == ""


class TestIdParsing:
    def test_whitespace_and_case_are_tolerated(self) -> None:
        _, marker = _run(["x<!-- used: S3 , s7 -->"])

        assert marker.ids == ["s3", "s7"]

    def test_ids_that_are_not_item_shaped_are_dropped(self) -> None:
        _, marker = _run(["x<!--used:s3,nonsense,s7-->"])

        assert marker.ids == ["s3", "s7"]

    def test_duplicate_ids_are_counted_once(self) -> None:
        _, marker = _run(["x<!--used:s3,s3-->"])

        assert marker.ids == ["s3"]
