"""L2 thread summary: the turns that scrolled out of the window, as a list
of items the code owns.

The old summarizer handed the model the whole summary and stored whatever
came back. Each fold therefore re-wrote every fact through the model, under
a word ceiling, with orders to compress the oldest material first -- so a
fact from turn 3 had to survive every subsequent rewrite and usually did
not. ACE (ICLR 2026) names the failure context collapse and its case study
measures it: one full rewrite took a context from 18,282 tokens to 122.

Here the model only proposes ADD/UPDATE/DROP operations against item ids
(`prompts.summarization`), and `merge.apply_ops` applies them
deterministically. An item the delta never names is never touched, so
retention is a property of the code, not of the model's diligence.

Still incremental, still off the reply path, and any failure -- provider
error, non-JSON reply, empty delta -- keeps the previous summary rather
than clearing it, exactly as before.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.config import DEFAULT_SUMMARY_MAX_ITEMS, DEFAULT_SUMMARY_MAX_OPS
from rag_ingestion.logger import get_logger
from rag_ingestion.prompts.summarization import (
    CURATE_SYSTEM_PROMPT,
    CURATE_USER_TEMPLATE,
    CURATE_USER_TEMPLATE_FIRST,
    EXTRACT_INSTRUCTIONS_TEMPLATE,
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_USER_TEMPLATE,
)
from rag_ingestion.summary.items import parse_items, render_for_prompt, render_items
from rag_ingestion.summary.merge import apply_ops, parse_delta, parse_facts

_log = get_logger(__name__)

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System"}


@runtime_checkable
class Summarizer(Protocol):
    def summarize(
        self,
        *,
        previous: str | None,
        turns: list[ChatTurn],
        sticky_key: str | None = None,
        instructions: str | None = None,
        #: The session's picked model: the fold runs on whatever the user is
        #: chatting with, so its quota and its quirks stay one model's.
        model: str | None = None,
        router: str | None = None,
    ) -> str | None: ...


def _render_turns(turns: list[ChatTurn]) -> str:
    return "\n".join(
        f"{_ROLE_LABELS.get(turn.role, turn.role)}: {turn.content}" for turn in turns
    )


class LLMSummarizer:
    def __init__(
        self,
        chat: ChatCompleter,
        *,
        max_tokens: int = 400,
        max_items: int = DEFAULT_SUMMARY_MAX_ITEMS,
        max_ops: int = DEFAULT_SUMMARY_MAX_OPS,
    ) -> None:
        self._chat = chat
        self._max_tokens = max_tokens
        self._max_items = max_items
        self._max_ops = max_ops

    def _ask(
        self,
        system: str,
        user: str,
        sticky_key: str | None,
        model: str | None,
        router: str | None,
    ) -> str:
        return self._chat.complete(
            [
                ChatTurn(role="system", content=system),
                ChatTurn(role="user", content=user),
            ],
            max_tokens=self._max_tokens,
            sticky_key=sticky_key,
            model=model,
            router=router,
        )

    def summarize(
        self,
        *,
        previous: str | None,
        turns: list[ChatTurn],
        sticky_key: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> str | None:
        if not turns:
            # Nothing dropped out since the last run, so there is nothing to
            # fold in and no reason to spend a call.
            return previous
        items = parse_items(previous)
        extract_user = EXTRACT_USER_TEMPLATE.format(turns=_render_turns(turns))
        if instructions:
            extract_user += EXTRACT_INSTRUCTIONS_TEMPLATE.format(
                instructions=instructions
            )
        try:
            # Phase one: what is worth remembering, reading the turns alone.
            facts = parse_facts(
                self._ask(
                    EXTRACT_SYSTEM_PROMPT,
                    extract_user,
                    sticky_key,
                    model,
                    router,
                ),
                max_facts=self._max_ops,
            )
        except Exception as error:
            # Deliberately broad, as before: a provider error, a timeout, or
            # a non-JSON reply must all leave the caller with the summary it
            # already had.
            _log.warning("Fact extraction failed: %s", error)
            return previous
        if not facts:
            # Nothing came out of those turns, so there is nothing to curate
            # and no reason to spend the second call.
            return previous

        rendered_facts = "\n".join(f"- {fact}" for fact in facts)
        try:
            # Phase two: where each fact belongs, reading the items alone.
            ops = parse_delta(
                self._ask(
                    CURATE_SYSTEM_PROMPT,
                    (
                        CURATE_USER_TEMPLATE.format(
                            items=render_for_prompt(items), facts=rendered_facts
                        )
                        if items
                        else CURATE_USER_TEMPLATE_FIRST.format(facts=rendered_facts)
                    ),
                    sticky_key,
                    model,
                    router,
                ),
                max_ops=self._max_ops,
            )
        except Exception as error:
            _log.warning("Summary curation failed: %s", error)
            return previous
        if not ops:
            return previous
        merged, evicted = apply_ops(items, ops, max_items=self._max_items)
        if evicted:
            # The one place the summary still loses information, so it is the
            # one place that says so -- with the text, not just the id. An id
            # alone is untraceable: the row it named is already gone from the
            # column by the time anyone reads the line, so there is nothing
            # left to look it up in.
            _log.info(
                "Summary evicted %d item(s) over the %d cap: %s",
                len(evicted),
                self._max_items,
                " | ".join(f"{item.id}={item.text!r}" for item in evicted),
            )
        return render_items(merged)
