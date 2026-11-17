"""Agent runtime: tool discovery, graph construction and instrumented execution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langgraph.errors import GraphRecursionError
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from stock_agent.agent.graph import build_graph
from stock_agent.agent.llm import InstrumentedLLM, build_chat_model
from stock_agent.agent.mcp_client import build_mcp_client, classify_client_error
from stock_agent.config import Settings
from stock_agent.observability import get_telemetry, track_request_usage
from stock_agent.observability.context import StepListener

log = logging.getLogger("stock_agent.agent")
AGENT_NAME = "stock-research-agent"


class AgentNotReady(RuntimeError):
    pass


class AgentError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class AgentResult:
    answer: str
    messages: list[BaseMessage]
    trace_id: str
    usage: dict[str, float | int]
    duration_ms: float
    steps: list[dict] = field(default_factory=list)


class AgentService:
    def __init__(self, settings: Settings, model_factory: Callable[[Settings], BaseChatModel] = build_chat_model):
        self.settings = settings
        self._model_factory = model_factory
        self._client = build_mcp_client(settings)
        self._graph = None
        self._lock = asyncio.Lock()
        self.tools: list[dict[str, str]] = []

    @property
    def ready(self) -> bool:
        return self._graph is not None

    async def initialise(self) -> None:
        """Discover MCP tools and compile the graph (idempotent, safe to retry)."""
        if self._graph is not None:
            return
        async with self._lock:
            if self._graph is not None:
                return
            tel = get_telemetry()
            with tel.tracer.start_as_current_span(
                "tools/list", kind=trace.SpanKind.CLIENT,
                attributes={"mcp.method.name": "tools/list", "mcp.server.name": self.settings.mcp_server_name},
            ) as span:
                start = time.perf_counter()
                try:
                    tools = await self._client.get_tools()
                except Exception as exc:
                    error_type = classify_client_error(exc)
                    span.set_status(Status(StatusCode.ERROR, error_type))
                    span.set_attribute("error.type", error_type)
                    tel.instruments.mcp_client_duration.record(
                        time.perf_counter() - start, {"mcp.method.name": "tools/list", "error.type": error_type}
                    )
                    raise AgentNotReady(f"MCP tool discovery failed: {error_type}") from exc
                tel.instruments.mcp_client_duration.record(time.perf_counter() - start, {"mcp.method.name": "tools/list"})
            llm = InstrumentedLLM(self._model_factory(self.settings), tools, self.settings)
            self._graph = build_graph(llm, tools)
            self.tools = [{"name": t.name, "description": (t.description or "").strip()} for t in tools]
            log.info("agent ready", extra={"event": "agent.ready", "tools": ",".join(t.name for t in tools)})

    async def initialise_with_retry(self, max_delay_s: float = 30.0) -> None:
        delay = 1.0
        while not self.ready:
            try:
                await self.initialise()
            except AgentNotReady as exc:
                log.warning("agent not ready, retrying", extra={"event": "agent.init_retry", "error": str(exc),
                                                                 "retry_in_s": delay})
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay_s)

    async def run(
        self,
        query: str,
        session_id: str | None = None,
        history: list[tuple[str, str]] | None = None,
        on_event: StepListener | None = None,
    ) -> AgentResult:
        """Run one agent turn.

        history:  prior (role, content) turns supplied by the client (stateless server).
        on_event: called synchronously with step_start/step_end events (used for streaming).
        """
        if self._graph is None:
            await self.initialise()  # raises AgentNotReady if MCP is still down
        tel = get_telemetry()
        inst = tel.instruments
        base_attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT_NAME}
        span_attrs = dict(base_attrs)
        if session_id:
            span_attrs["session.id"] = session_id

        with track_request_usage(on_event) as usage, tel.tracer.start_as_current_span(
            f"invoke_agent {AGENT_NAME}", attributes=span_attrs,
            record_exception=False, set_status_on_exception=False,  # handled explicitly below
        ) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            if on_event is not None:
                on_event({"event": "start", "trace_id": trace_id})
            inst.agent_active.add(1, base_attrs)
            start = time.perf_counter()
            error_type: str | None = None
            failure: BaseException | None = None
            log.info("agent request started", extra={"event": "agent.request", "session_id": session_id,
                                                      "query": tel.sanitize(query)})
            try:
                async with asyncio.timeout(self.settings.agent_request_timeout_s):
                    state = await self._graph.ainvoke(
                        {"messages": [*(history or []), ("user", query)]},
                        config={"recursion_limit": self.settings.agent_recursion_limit},
                    )
            except GraphRecursionError as exc:
                error_type, failure = "recursion_limit", exc
                raise AgentError(error_type, "Agent exceeded its step budget without finishing") from exc
            except TimeoutError as exc:
                error_type, failure = "timeout", exc
                raise AgentError(error_type, "Agent request timed out") from exc
            except Exception as exc:
                error_type, failure = type(exc).__name__, exc
                raise AgentError(error_type, "Agent failed to complete the request") from exc
            finally:
                elapsed = time.perf_counter() - start
                inst.agent_active.add(-1, base_attrs)
                status_attrs = {**base_attrs, **({"error.type": error_type} if error_type else {})}
                inst.agent_request_duration.record(elapsed, status_attrs)
                inst.agent_request_tokens.record(usage.total_tokens, base_attrs)
                inst.agent_request_tool_calls.record(usage.tool_calls, base_attrs)
                inst.agent_request_llm_calls.record(usage.llm_calls, base_attrs)
                span.set_attributes({
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                    "agent.tool_calls": usage.tool_calls,
                    "agent.tool_errors": usage.tool_errors,
                    "agent.llm_calls": usage.llm_calls,
                })
                if error_type:
                    if failure is not None:
                        span.record_exception(failure)
                    span.set_attribute("error.type", error_type)
                    span.set_status(Status(StatusCode.ERROR, error_type))
                log.log(
                    logging.ERROR if error_type else logging.INFO,
                    "agent request finished",
                    exc_info=failure if error_type not in (None, "recursion_limit", "timeout") else None,
                    extra={"event": "agent.response", "status": "error" if error_type else "ok",
                           "error_type": error_type, "session_id": session_id,
                           "duration_ms": round(elapsed * 1000, 2), **usage.as_dict()},
                )

            final = state["messages"][-1]
            return AgentResult(
                answer=str(final.content),
                messages=state["messages"],
                trace_id=trace_id,
                usage=usage.as_dict(),
                duration_ms=round(elapsed * 1000, 2),
                steps=[dict(st) for st in usage.steps],
            )
