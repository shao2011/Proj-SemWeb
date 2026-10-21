from rag_ingestion.language import detect_language, segment_thai


def test_thai_text_detects_th() -> None:
    assert detect_language("ผู้ประกอบการต้องยื่นแบบแสดงรายการภาษี") == "th"


def test_vietnamese_diacritics_detect_vi() -> None:
    assert detect_language("Người nộp thuế phải kê khai đầy đủ") == "vi"


def test_plain_latin_detects_en() -> None:
    assert detect_language("Travel requests require approval") == "en"


def test_chinese_detects_zh_and_japanese_kana_detects_ja() -> None:
    assert detect_language("增值税适用于进口货物") == "zh"
    assert detect_language("消費税は輸入品に適用されます") == "ja"


def test_korean_detects_ko() -> None:
    assert detect_language("부가가치세는 수입품에 적용됩니다") == "ko"


def test_numbers_and_short_text_stay_unlabeled() -> None:
    assert detect_language("42 7.5% 2026") is None
    assert detect_language("ZX") is None
    assert detect_language("") is None


def test_mixed_text_labels_by_majority_script() -> None:
    assert detect_language("ภาษีมูลค่าเพิ่ม (VAT) สำหรับสินค้านำเข้า") == "th"


def test_segment_thai_returns_dictionary_words() -> None:
    words = segment_thai("ผู้ประกอบการต้องยื่นแบบแสดงรายการภาษี")
    assert "ภาษี" in words
    assert "ผู้ประกอบการ" in words
    assert all(word.strip() for word in words)
