from rag_ingestion.storage.threads import ThreadMessageRole

from eval import settings
from eval.data import LocomoConversation, LocomoMessage, LocomoQuestion
from eval.e2_history import (
    FoldingThread,
    RecursiveSummarizer,
    _chunk,
    retrieved_history,
    score_row,
    thread_messages,
)


def _conversation(count: int) -> LocomoConversation:
    conversation = LocomoConversation("conv-26", "Ann", "Bob")
    for index in range(count):
        speaker = "Ann" if index % 2 == 0 else "Bob"
        conversation.messages.append(
            LocomoMessage(f"D1:{index + 1}", 1, speaker, "user" if speaker == "Ann" else "assistant", f"{speaker}: message {index}")
        )
    return conversation


class CountingSummarizer:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def summarize(self, *, previous, turns, **_):
        self.calls.append(len(turns))
        return f"{previous or ''}|{len(turns)}"


def test_thread_messages_keep_order_and_roles() -> None:
    messages = thread_messages(_conversation(3))

    assert [m.position for m in messages] == [1, 2, 3]
    assert [m.role for m in messages] == [ThreadMessageRole.USER, ThreadMessageRole.ASSISTANT, ThreadMessageRole.USER]
    assert messages[0].message_id == "conv-26-D1:1"


def test_sliding_window_keeps_the_opener_and_the_newest_100() -> None:
    messages = thread_messages(_conversation(300))

    window = FoldingThread(summarizer=None).window(messages)

    assert window[0].position == 1
    assert [m.position for m in window[1:]] == list(range(201, 301))


def test_folding_never_exceeds_the_cap_and_folds_in_bounded_steps() -> None:
    messages = thread_messages(_conversation(300))
    summarizer = CountingSummarizer()
    thread = FoldingThread(summarizer=summarizer)

    thread.replay(messages)
    window = thread.window(messages)

    assert thread.folds == len(summarizer.calls) > 3
    assert all(size <= settings.FOLD_MAX_MESSAGES for size in summarizer.calls)
    assert len(window) <= settings.HISTORY_WINDOW_MESSAGES + 1
    assert window[0].position == 1
    # Everything between the opener and the window is covered by the summary.
    assert thread.watermark >= window[1].position - 1
    assert thread.summary.count("|") == thread.folds


def test_recursive_summary_is_capped_at_the_h5_budget() -> None:
    summarizer = RecursiveSummarizer(lambda turns: "x" * 10_000)

    summary = summarizer.summarize(previous=None, turns=[])

    assert len(summary) == settings.RECURSIVE_SUMMARY_MAX_CHARS == 5_120


def test_history_retrieval_skips_the_window_and_restores_order() -> None:
    conversation = _conversation(30)
    messages = thread_messages(conversation)
    window = messages[-5:]
    hits = [_chunk("conv-26", m.dia_id, m.content, "h4") for m in reversed(conversation.messages)]

    picked = retrieved_history(hits, window, messages, k=3)

    assert [m.position for m in picked] == [23, 24, 25]


def test_lexical_scores_skip_adversarial_questions() -> None:
    answered = LocomoQuestion("q1", "Where?", "Oslo", 4, ())
    adversarial = LocomoQuestion("q2", "What?", None, 5, ())

    assert score_row(answered, "Oslo")["f1"] == 1.0
    assert score_row(adversarial, "anything") == {}
