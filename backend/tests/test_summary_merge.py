import pytest

from rag_ingestion.schemas.summary import SummaryItem, SummaryOp, SummaryOpKind
from rag_ingestion.summary import apply_ops, parse_delta


def _items(*texts: str) -> list[SummaryItem]:
    return [
        SummaryItem(id=f"s{position}", text=text)
        for position, text in enumerate(texts, start=1)
    ]


class TestApplyOps:
    def test_an_item_the_model_never_mentions_survives_verbatim(self) -> None:
        """The whole inversion. Under the old summarizer an unmentioned fact
        was gone; here it must not even be touched."""
        items = _items("User works at FPT", "Deadline is Q3")

        merged, evicted = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, text="User asked about VAT")],
            max_items=16,
        )

        # Compared on text and id: every item ages by a fold, which is
        # bookkeeping, not a change to what the item says.
        assert [(item.id, item.text) for item in merged[:2]] == [
            (items[0].id, items[0].text),
            (items[1].id, items[1].text),
        ]
        assert evicted == []

    def test_add_assigns_the_next_id_and_ignores_a_model_supplied_one(self) -> None:
        """Ids come from the merge, never the model, so a guessed id can
        never overwrite a real item."""
        items = [SummaryItem(id="s1", text="Existing"), SummaryItem(id="s4", text="Later")]

        merged, _ = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, id="s1", text="Brand new fact")],
            max_items=16,
        )

        added = merged[-1]
        assert added.id == "s5"
        assert added.text == "Brand new fact"
        assert merged[0].text == "Existing"

    def test_update_replaces_text_and_moves_the_item_to_the_back(self) -> None:
        items = _items("Deadline is Q3", "Team has 12 people")

        merged, _ = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.UPDATE, id="s1", text="Deadline moved to Q4")],
            max_items=16,
        )

        assert [item.id for item in merged] == ["s2", "s1"]
        assert merged[-1].text == "Deadline moved to Q4"

    def test_drop_removes_exactly_the_named_item(self) -> None:
        items = _items("Kept", "Superseded", "Also kept")

        merged, _ = apply_ops(
            items, [SummaryOp(op=SummaryOpKind.DROP, id="s2")], max_items=16
        )

        assert [item.text for item in merged] == ["Kept", "Also kept"]

    def test_an_unknown_id_is_skipped_without_hurting_the_rest(self) -> None:
        """A hallucinated id must delete nothing, and must not take the
        delta's good operations down with it."""
        items = _items("Only fact")

        merged, _ = apply_ops(
            items,
            [
                SummaryOp(op=SummaryOpKind.DROP, id="s99"),
                SummaryOp(op=SummaryOpKind.UPDATE, id="s42", text="ghost"),
                SummaryOp(op=SummaryOpKind.ADD, text="New fact"),
            ],
            max_items=16,
        )

        assert [item.text for item in merged] == ["Only fact", "New fact"]

    def test_every_surviving_item_ages_by_one_fold(self) -> None:
        items = _items("A", "B")

        merged, _ = apply_ops(
            items, [SummaryOp(op=SummaryOpKind.ADD, text="C")], max_items=16
        )

        aged = {item.text: item.folds for item in merged}
        assert aged["A"] == 1 and aged["B"] == 1
        assert aged["C"] == 0, "an item minted this fold has not survived one yet"

    def test_eviction_spares_the_item_that_gets_used(self) -> None:
        """The failure LRU alone has: a fact that is right the first time is
        never UPDATEd, drifts to the front, and dies before a newer item
        nobody has ever leaned on."""
        old_but_used = SummaryItem(id="s1", text="Deadline is 30/9", used=4, folds=6)
        old_and_ignored = SummaryItem(id="s2", text="It rained", used=0, folds=6)

        merged, evicted = apply_ops(
            [old_but_used, old_and_ignored],
            [SummaryOp(op=SummaryOpKind.ADD, text="New fact")],
            max_items=2,
        )

        assert [item.id for item in evicted] == ["s2"]
        assert "s1" in [item.id for item in merged]

    def test_an_item_minted_this_fold_is_never_the_one_evicted(self) -> None:
        """It has had no chance to be used yet, so its zero score is not
        evidence of anything."""
        established = SummaryItem(id="s1", text="Old", used=0, folds=4)

        merged, evicted = apply_ops(
            [established],
            [
                SummaryOp(op=SummaryOpKind.ADD, text="Brand new"),
                SummaryOp(op=SummaryOpKind.ADD, text="Also brand new"),
            ],
            max_items=2,
        )

        assert [item.id for item in evicted] == ["s1"]
        assert len(merged) == 2

    def test_between_two_unused_items_the_one_with_more_chances_goes(self) -> None:
        young = SummaryItem(id="s1", text="Young", used=0, folds=1)
        veteran = SummaryItem(id="s2", text="Veteran", used=0, folds=9)

        _, evicted = apply_ops(
            [young, veteran],
            [SummaryOp(op=SummaryOpKind.ADD, text="New")],
            max_items=2,
        )

        assert [item.id for item in evicted] == ["s2"]

    def test_an_update_keeps_the_counters_it_earned(self) -> None:
        items = [SummaryItem(id="s1", text="Deadline is Q3", used=5, folds=8)]

        merged, _ = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.UPDATE, id="s1", text="Deadline is Q4")],
            max_items=16,
        )

        assert merged[0].used == 5, "a correction is not a new fact"
        assert merged[0].text == "Deadline is Q4"

    def test_a_restated_add_credits_the_item_it_matched(self) -> None:
        items = [SummaryItem(id="s1", text="Deadline is Q4", used=1, folds=3)]

        merged, _ = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, text="deadline is q4")],
            max_items=16,
        )

        assert len(merged) == 1
        assert merged[0].used == 1

    def test_eviction_takes_from_the_front_and_reports_what_it_took(self) -> None:
        """Over the cap, the least recently touched items go, and they come
        back whole so the caller can log the text. An id alone is untraceable:
        the item is gone from the stored column by the time anyone reads it."""
        items = _items("Oldest", "Middle", "Newest")

        merged, evicted = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, text="Fourth")],
            max_items=3,
        )

        assert [(item.id, item.text) for item in evicted] == [("s1", "Oldest")]
        assert [item.text for item in merged] == ["Middle", "Newest", "Fourth"]

    def test_a_fresh_update_rescues_an_item_from_eviction(self) -> None:
        """Recency of touch, not creation order: updating s1 moves it to the
        back, so the eviction that follows takes s2 instead."""
        items = _items("Old but just corrected", "Old and untouched", "Newer")

        merged, evicted = apply_ops(
            items,
            [
                SummaryOp(op=SummaryOpKind.UPDATE, id="s1", text="Corrected"),
                SummaryOp(op=SummaryOpKind.ADD, text="Fourth"),
            ],
            max_items=3,
        )

        assert [item.id for item in evicted] == ["s2"]
        assert [item.id for item in merged] == ["s3", "s1", "s4"]

    def test_an_add_that_restates_a_live_item_becomes_a_touch(self) -> None:
        """The mechanical duplicate guard.

        The curator is meant to UPDATE rather than re-ADD a fact it already
        holds, but an UPDATE naming an id that does not exist is skipped by
        design -- so the next fold sees the fact missing and ADDs it again.
        That loop is what mints duplicates, and no model quality fixes it.
        """
        items = _items("Deadline is Q4", "Team has 12 people")

        merged, evicted = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, text="  deadline IS   q4  ")],
            max_items=16,
        )

        assert [item.text for item in merged] == ["Team has 12 people", "Deadline is Q4"]
        assert evicted == []

    def test_a_genuinely_new_add_still_lands(self) -> None:
        items = _items("Deadline is Q4")

        merged, _ = apply_ops(
            items,
            [SummaryOp(op=SummaryOpKind.ADD, text="Deadline is Q3")],
            max_items=16,
        )

        assert [item.text for item in merged] == ["Deadline is Q4", "Deadline is Q3"]

    def test_no_ops_changes_no_content(self) -> None:
        items = _items("A", "B")

        merged, evicted = apply_ops(items, [], max_items=16)

        assert [item.text for item in merged] == ["A", "B"]
        assert all(item.folds == 1 for item in merged), "a fold still counts"
        assert evicted == []


class TestSummaryOpValidation:
    def test_update_and_drop_require_an_id(self) -> None:
        with pytest.raises(ValueError):
            SummaryOp(op=SummaryOpKind.UPDATE, text="no target")
        with pytest.raises(ValueError):
            SummaryOp(op=SummaryOpKind.DROP)

    def test_add_and_update_require_text(self) -> None:
        with pytest.raises(ValueError):
            SummaryOp(op=SummaryOpKind.ADD)
        with pytest.raises(ValueError):
            SummaryOp(op=SummaryOpKind.UPDATE, id="s1", text="   ")

    def test_extra_keys_from_the_model_are_ignored_not_fatal(self) -> None:
        op = SummaryOp.model_validate(
            {"op": "ADD", "text": "fact", "reason": "models invent fields"}
        )

        assert op.op is SummaryOpKind.ADD


class TestParseDelta:
    def test_plain_json_parses(self) -> None:
        ops = parse_delta('{"ops": [{"op": "ADD", "text": "A fact"}]}')

        assert len(ops) == 1
        assert ops[0].op is SummaryOpKind.ADD

    def test_code_fenced_json_parses(self) -> None:
        raw = '```json\n{"ops": [{"op": "DROP", "id": "s2"}]}\n```'

        ops = parse_delta(raw)

        assert len(ops) == 1
        assert ops[0].id == "s2"

    def test_one_malformed_op_does_not_discard_the_others(self) -> None:
        raw = (
            '{"ops": ['
            '{"op": "UPDATE"},'
            '{"op": "ADD", "text": "good"},'
            '{"op": "TRANSMUTE", "id": "s1"}'
            "]}"
        )

        ops = parse_delta(raw)

        assert [op.text for op in ops] == ["good"]

    def test_unparseable_text_raises_for_the_caller_to_fall_back(self) -> None:
        with pytest.raises(ValueError):
            parse_delta("I could not produce JSON, sorry.")

    def test_ops_beyond_the_cap_are_dropped(self) -> None:
        payload = {"ops": [{"op": "ADD", "text": f"fact {n}"} for n in range(50)]}
        import json

        ops = parse_delta(json.dumps(payload))

        assert len(ops) == 20
