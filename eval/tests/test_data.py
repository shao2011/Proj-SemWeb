import json

import pytest

from eval import settings
from eval.data import load_locomo, load_requesta, pdf_for

_LOCOMO = [
    {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Ann",
            "speaker_b": "Bob",
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_1": [
                {"speaker": "Ann", "dia_id": "D1:1", "text": "Hi Bob"},
                {
                    "speaker": "Bob",
                    "dia_id": "D1:2",
                    "text": "Look at this",
                    "blip_caption": "a red bike",
                },
            ],
            "session_2_date_time": "2:00 pm on 2 May, 2023",
            "session_2": [{"speaker": "Ann", "dia_id": "D2:1", "text": "I moved to Oslo"}],
        },
        "observation": {
            "session_1_observation": {"Ann": [["Ann greets Bob.", "D1:1"]]},
            "session_2_observation": {"Ann": [["Ann moved to Oslo.", "D2:1"]]},
        },
        "qa": [
            {"question": "Where did Ann move?", "answer": "Oslo", "category": 4, "evidence": ["D2:1"]},
            {"question": "What did Ann sell?", "adversarial_answer": "a bike", "category": 5, "evidence": []},
        ],
    }
]


@pytest.fixture()
def locomo_file(tmp_path):
    path = tmp_path / "locomo10.json"
    path.write_text(json.dumps(_LOCOMO), encoding="utf-8")
    return path


def test_locomo_messages_keep_order_speaker_date_and_photo_caption(locomo_file) -> None:
    [conversation] = load_locomo(locomo_file)

    assert conversation.conversation_id == "conv-1"
    assert [m.dia_id for m in conversation.messages] == ["D1:1", "D1:2", "D2:1"]
    assert [m.role for m in conversation.messages] == ["user", "assistant", "user"]
    assert conversation.messages[0].content == "[1:00 pm on 1 May, 2023] Ann: Hi Bob"
    assert conversation.messages[1].content == "Bob: Look at this [shares a photo: a red bike]"
    assert conversation.messages[2].session == 2
    assert [len(s) for s in conversation.sessions] == [2, 1]


def test_locomo_questions_and_observations(locomo_file) -> None:
    [conversation] = load_locomo(locomo_file)

    answered, adversarial = conversation.questions
    assert (answered.category, answered.answer) == (4, "Oslo")
    assert adversarial.category == 5 and adversarial.answer is None
    assert [o.text for o in conversation.observations] == ["Ann greets Bob.", "Ann moved to Oslo."]
    assert conversation.observations[1].date == "2:00 pm on 2 May, 2023"


@pytest.mark.skipif(not settings.REQUESTA_XLSX.exists(), reason="REQuestA not downloaded")
def test_requesta_has_159_questions_over_six_documents() -> None:
    items = load_requesta()

    assert len(items) == 159
    assert len({item.document for item in items}) == 6
    assert all(pdf_for(item.document).exists() for item in items)
    assert items[0].question and items[0].answer and items[0].gold_passage
