import pytest

from eval import settings
from eval.data import load_requesta
from eval.e3_changes import Edit, RecordingEmbedder, choose_block, load_edits, parse_sse, replace_value, value_pattern


def test_the_case_study_has_the_configured_number_of_edits() -> None:
    edits = load_edits()

    assert len(edits) == settings.E3_EDIT_COUNT
    assert all(edit.old != edit.new for edit in edits)


@pytest.mark.skipif(not settings.REQUESTA_XLSX.exists(), reason="REQuestA not downloaded")
def test_every_old_value_is_in_its_questions_gold_passage() -> None:
    items = {item.qid: item for item in load_requesta()}

    for edit in load_edits():
        assert value_pattern(edit.old).search(" ".join(items[edit.qid].gold_passage.split())), edit


def test_values_match_across_line_breaks() -> None:
    edit = Edit(1, "the Director, DUSD(L)CALS", "the Program Manager")

    assert replace_value("assigned by the\nDirector, DUSD(L)CALS to DIDs", edit) == "assigned by the Program Manager to DIDs"


def test_the_gold_block_wins_over_other_blocks_with_the_same_value() -> None:
    edit = Edit(22, "15 km/h", "12 km/h")
    blocks = [
        {"block_id": "b1", "text": "The rover drives at 15 km/h on natural terrain."},
        {"block_id": "b2", "text": "Maximum speed on prepared surface: not less than 15 km/h on a smooth, level, prepared surface."},
    ]
    gold = "Maximum Speed on Prepared Surface: not less than 15 km/h on a smooth, level, prepared surface."

    assert choose_block(blocks, edit, gold)["block_id"] == "b2"
    with pytest.raises(LookupError):
        choose_block([{"block_id": "b3", "text": "nothing"}], edit, gold)


def test_parse_sse_reads_named_events() -> None:
    body = 'event: sources\ndata: {"hits": []}\n\nevent: done\ndata: {"content": "8 s"}\n\n'

    assert parse_sse(body) == [("sources", {"hits": []}), ("done", {"content": "8 s"})]


def test_recording_embedder_batches_retries_and_records() -> None:
    calls: list[int] = []

    class Flaky:
        failed = False

        def embed_documents(self, items, profile, *, intent):
            if not Flaky.failed:
                Flaky.failed = True
                raise RuntimeError("timeout")
            calls.append(len(items))
            return [[1.0] for _ in items]

    embedder = RecordingEmbedder(Flaky(), batch=2, sleep=lambda s: None)

    vectors = embedder.embed_documents(["a", "b", "c"], None, intent="document")

    assert vectors == [[1.0]] * 3
    assert calls == [2, 1]
    assert embedder.texts == ["a", "b", "c"]
