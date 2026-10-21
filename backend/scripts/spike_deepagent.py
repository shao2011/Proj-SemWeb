"""Deep-agent spike: verifies the three acceptance criteria before any
production integration (see IMPLEMENT.md "Deep Agent Spike").

Run:  uv sync --extra agent && OPENROUTER_KEY=sk-... uv run python backend/scripts/spike_deepagent.py

1. Tool loop works with tenant-scoped tools (fake model, no network).
2. Isolation: the default virtual filesystem cannot reach the host disk.
3. Live: a free OpenRouter model calls the search tool and answers from it
   (skipped unless OPENROUTER_KEY is set).
"""

from __future__ import annotations

import os
import time

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


class ToolFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def make_tools(owner_user_id: str, project_id: str, calls: list):
    """Tools closed over the tenant, the same shape the real route would use."""

    @tool
    def search_project_documents(query: str) -> str:
        """Search this project's uploaded documents and return numbered evidence passages."""
        calls.append(("search", owner_user_id, project_id, query))
        return "[1] Article 5 (travel-policy.docx): Travel requests require code ZX-42."

    @tool
    def save_project_memory(key: str, value: str) -> str:
        """Remember a durable fact the user stated, scoped to this project."""
        calls.append(("save_project", owner_user_id, project_id, key, value))
        return "saved"

    @tool
    def save_user_memory(key: str, value: str) -> str:
        """Remember a durable fact about the user that applies across projects."""
        calls.append(("save_user", owner_user_id, key, value))
        return "saved"

    return [search_project_documents, save_project_memory, save_user_memory]


def check_tool_loop() -> None:
    calls: list = []
    messages = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_project_documents",
                        "args": {"query": "travel code"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="Travel needs code ZX-42 [1]."),
        ]
    )
    agent = create_deep_agent(
        model=ToolFakeModel(messages=messages),
        tools=make_tools("user-a", "prj_spike", calls),
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "what code?"}]})
    assert calls == [("search", "user-a", "prj_spike", "travel code")], calls
    assert "ZX-42" in result["messages"][-1].content
    print("1. tool loop: PASS")


def check_isolation() -> None:
    messages = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "/etc/passwd"}, "id": "c1"},
                    {"name": "ls", "args": {"path": "/"}, "id": "c2"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_deep_agent(model=ToolFakeModel(messages=messages), tools=[])
    result = agent.invoke({"messages": [{"role": "user", "content": "read server files"}]})
    outputs = [str(m.content) for m in result["messages"] if m.type == "tool"]
    assert any("not found" in text for text in outputs), outputs
    assert not any("root:" in text for text in outputs), "host /etc/passwd leaked!"
    print("2. isolation (virtual FS, no host disk): PASS")


def check_live_openrouter() -> None:
    key = os.environ.get("OPENROUTER_KEY", "").strip()
    if not key:
        print("3. live OpenRouter: SKIPPED (set OPENROUTER_KEY to run)")
        return
    from langchain_openai import ChatOpenAI

    calls: list = []
    agent = create_deep_agent(
        model=ChatOpenAI(
            model="openrouter/free",
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0,
        ),
        tools=make_tools("user-a", "prj_spike", calls),
        system_prompt=(
            "Answer from project documents. Search before answering document "
            "questions. Save durable user facts with the memory tools."
        ),
    )
    start = time.time()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "What code do travel requests need?"}]}
    )
    assert any(call[0] == "search" for call in calls), calls
    assert "ZX-42" in result["messages"][-1].content
    print(f"3. live OpenRouter tool-calling: PASS ({time.time() - start:.1f}s)")


if __name__ == "__main__":
    check_tool_loop()
    check_isolation()
    check_live_openrouter()
