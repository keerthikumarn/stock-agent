"""Per-request usage accumulator (tokens, LLM calls, tool calls).

LangGraph propagates contextvars into node/tool execution, so every LLM call
and MCP call made while serving a request adds to the same RequestUsage.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator

log = logging.getLogger(__name__)
StepListener = Callable[[dict[str, Any]], None]


@dataclass
class RequestUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    cost_usd: float = 0.0
    # Ordered record of what the agent did (LLM turns, tool calls) for the API/UI.
    steps: list[dict[str, Any]] = field(default_factory=list)
    listener: StepListener | None = None

    def __post_init__(self) -> None:
        self._lock = Lock()
        self._t0 = time.perf_counter()
        self._next_id = 0

    def _offset_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def _notify(self, event: dict[str, Any]) -> None:
        if self.listener is not None:
            try:
                self.listener(event)
            except Exception:  # noqa: BLE001 - a slow/broken consumer must never break the agent
                log.warning("step listener failed", exc_info=True)

    def step_start(self, kind: str, name: str, **data: Any) -> int:
        """Record the start of an LLM turn or tool call; returns the step id."""
        with self._lock:
            step_id = self._next_id
            self._next_id += 1
            step = {"id": step_id, "type": kind, "name": name, "start_ms": self._offset_ms(), "status": "running", **data}
            self.steps.append(step)
        self._notify({"event": "step_start", **step})
        return step_id

    def step_end(self, step_id: int, status: str, **data: Any) -> None:
        with self._lock:
            step = next((s for s in self.steps if s["id"] == step_id), None)
            if step is None:
                return
            step.update(status=status, duration_ms=round(self._offset_ms() - step["start_ms"], 1), **data)
            snapshot = dict(step)
        self._notify({"event": "step_end", **snapshot})

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_llm(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cost_usd += cost_usd

    def add_tool(self, failed: bool) -> None:
        with self._lock:
            self.tool_calls += 1
            self.tool_errors += int(failed)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "cost_usd": round(self.cost_usd, 6),
        }


_current: ContextVar[RequestUsage | None] = ContextVar("request_usage", default=None)


def current_usage() -> RequestUsage | None:
    return _current.get()


@contextlib.contextmanager
def track_request_usage(listener: StepListener | None = None) -> Iterator[RequestUsage]:
    usage = RequestUsage(listener=listener)
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)
