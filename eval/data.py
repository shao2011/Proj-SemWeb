"""Dataset loaders: REQuestA (E1, E3) and LoCoMo (E2).

Neither dataset is redistributed. REQuestA is fetched as described in
data/REQuestA/README.md; `fetch_locomo` downloads LoCoMo's released file.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from eval.settings import LOCOMO_JSON, LOCOMO_URL, REQUESTA_PDF_DIR, REQUESTA_XLSX


@dataclass(frozen=True)
class RequestaItem:
    qid: int
    document: str
    page: int | None
    gold_passage: str
    question: str
    answer: str


def pdf_for(document: str) -> Path:
    return REQUESTA_PDF_DIR / f"SRS_{document}.pdf"


def load_requesta(path: Path = REQUESTA_XLSX) -> list[RequestaItem]:
    from openpyxl import load_workbook

    sheet = load_workbook(path, read_only=True).active
    rows = list(sheet.iter_rows(values_only=True))
    header = [str(cell).strip() if cell else "" for cell in rows[0]]
    column = {name: header.index(name) for name in ("Id", "Source", "Text Passage", "Question", "Answer")}
    items: list[RequestaItem] = []
    for row in rows[1:]:
        if row[column["Id"]] is None:
            continue
        source = str(row[column["Source"]]).strip()
        match = re.fullmatch(r"SRS-(.+)-page(\d+)", source)
        if match is None:
            raise ValueError(f"Unexpected REQuestA source {source!r}")
        items.append(
            RequestaItem(
                qid=int(row[column["Id"]]),
                document=match.group(1),
                page=int(match.group(2)),
                gold_passage=str(row[column["Text Passage"]]).strip(),
                question=str(row[column["Question"]]).strip(),
                answer=str(row[column["Answer"]]).strip(),
            )
        )
    return items


@dataclass(frozen=True)
class LocomoMessage:
    dia_id: str
    session: int
    speaker: str
    role: str
    content: str


@dataclass(frozen=True)
class LocomoQuestion:
    qid: str
    question: str
    answer: str | None
    category: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class LocomoObservation:
    session: int
    speaker: str
    text: str
    dia_id: str
    date: str = ""


@dataclass
class LocomoConversation:
    conversation_id: str
    speaker_a: str
    speaker_b: str
    messages: list[LocomoMessage] = field(default_factory=list)
    questions: list[LocomoQuestion] = field(default_factory=list)
    observations: list[LocomoObservation] = field(default_factory=list)

    @property
    def sessions(self) -> list[list[LocomoMessage]]:
        grouped: dict[int, list[LocomoMessage]] = {}
        for message in self.messages:
            grouped.setdefault(message.session, []).append(message)
        return [grouped[key] for key in sorted(grouped)]


def fetch_locomo(path: Path = LOCOMO_JSON) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(LOCOMO_URL, path)
    return path


def _session_numbers(conversation: dict) -> list[int]:
    numbers = {
        int(match.group(1))
        for key, value in conversation.items()
        if (match := re.fullmatch(r"session_(\d+)", key)) and isinstance(value, list)
    }
    return sorted(numbers)


def load_locomo(path: Path = LOCOMO_JSON) -> list[LocomoConversation]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    conversations: list[LocomoConversation] = []
    for sample in raw:
        body = sample["conversation"]
        speaker_a, speaker_b = body["speaker_a"], body["speaker_b"]
        conversation = LocomoConversation(sample["sample_id"], speaker_a, speaker_b)
        for number in _session_numbers(body):
            date = body.get(f"session_{number}_date_time", "")
            for position, turn in enumerate(body[f"session_{number}"]):
                text = f"{turn['speaker']}: {turn['text']}"
                if turn.get("blip_caption"):
                    text += f" [shares a photo: {turn['blip_caption']}]"
                if position == 0 and date:
                    text = f"[{date}] {text}"
                conversation.messages.append(
                    LocomoMessage(
                        dia_id=turn["dia_id"],
                        session=number,
                        speaker=turn["speaker"],
                        role="user" if turn["speaker"] == speaker_a else "assistant",
                        content=text,
                    )
                )
        for index, qa in enumerate(sample["qa"]):
            conversation.questions.append(
                LocomoQuestion(
                    qid=f"{sample['sample_id']}-q{index}",
                    question=qa["question"],
                    answer=None if qa["category"] == 5 else str(qa["answer"]),
                    category=int(qa["category"]),
                    evidence=tuple(qa.get("evidence", [])),
                )
            )
        for key, by_speaker in sample.get("observation", {}).items():
            number = int(re.fullmatch(r"session_(\d+)_observation", key).group(1))
            date = body.get(f"session_{number}_date_time", "")
            for speaker, facts in by_speaker.items():
                for fact in facts:
                    conversation.observations.append(
                        LocomoObservation(number, speaker, fact[0], str(fact[1]), date)
                    )
        conversation.observations.sort(key=lambda o: o.session)
        conversations.append(conversation)
    return conversations
