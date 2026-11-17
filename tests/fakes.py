"""Deterministic tool-calling chat model for tests (no Ollama required)."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedToolModel(BaseChatModel):
    """Calls the scripted tools in order, then answers with a summary."""

    script: list[tuple[str, dict[str, Any]]] = []
    model: str = "fake-llm"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        done = sum(isinstance(m, ToolMessage) for m in messages)
        usage = {"input_tokens": 100 + 10 * len(messages), "output_tokens": 20, "total_tokens": 120 + 10 * len(messages)}
        if done < len(self.script):
            name, args = self.script[done]
            msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}],
                            usage_metadata=usage, response_metadata={"model": self.model, "done_reason": "stop"})
        else:
            results = " | ".join(str(m.content)[:60] for m in messages if isinstance(m, ToolMessage))
            msg = AIMessage(content=f"Final answer based on: {results}", usage_metadata=usage,
                            response_metadata={"model": self.model, "done_reason": "stop"})
        return ChatResult(generations=[ChatGeneration(message=msg)])
