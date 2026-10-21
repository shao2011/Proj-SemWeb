"""Script-based language labeling and Thai word segmentation.

Labeling is deterministic codepoint counting, not statistical detection:
tokenizer routing and sentence rules are questions of *script*, which the
characters answer exactly, while guessing the *language* of a three-word
table cell is where detectors fail. Only same-script distinctions rely on
heuristics (Vietnamese diacritics vs plain Latin; kana vs ideographs).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from nlpo3 import load_dict, segment

_THAI = re.compile(r"[฀-๿]")
_HANGUL = re.compile(r"[가-힯]")
_KANA = re.compile(r"[぀-ヿ]")
_IDEOGRAPH = re.compile(r"[㐀-䶿一-鿿]")
_LATIN = re.compile(r"[a-z]")
# Letters that exist in Vietnamese orthography but not in plain ASCII text:
# the base extras plus every tone-marked vowel.
_VIETNAMESE = re.compile(
    r"[ăâđêôơưàảãạáằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]"
)

#: Below this many script-bearing characters a label is a guess, not a fact.
_MIN_LETTERS = 4

# PyThaiNLP's newmm wordlist (Apache-2.0), vendored because nlpo3 ships the
# algorithm without a dictionary.
_DICT_PATH = Path(__file__).resolve().parent / "data" / "words_th.txt"
_DICT_NAME = "rag-th"


def detect_language(text: str) -> str | None:
    """Majority-script label for one block of text, or None when unsure."""
    lowered = text.casefold()
    thai = len(_THAI.findall(lowered))
    hangul = len(_HANGUL.findall(lowered))
    kana = len(_KANA.findall(lowered))
    ideographs = len(_IDEOGRAPH.findall(lowered))
    latin = len(_LATIN.findall(lowered))
    total = thai + hangul + kana + ideographs + latin
    if total < _MIN_LETTERS:
        return None
    if thai / total > 0.5:
        return "th"
    if hangul / total > 0.5:
        return "ko"
    # Any kana marks Japanese; ideographs alone read as Chinese.
    if (kana + ideographs) / total > 0.5:
        return "ja" if kana else "zh"
    if latin / total > 0.5:
        return "vi" if _VIETNAMESE.search(lowered) else "en"
    return None


@lru_cache(maxsize=1)
def _thai_dict_loaded() -> bool:
    load_dict(str(_DICT_PATH), _DICT_NAME)
    return True


def segment_thai(text: str) -> list[str]:
    """Dictionary word segmentation for a run of Thai text."""
    _thai_dict_loaded()
    return [word for word in segment(text, _DICT_NAME) if word.strip()]
