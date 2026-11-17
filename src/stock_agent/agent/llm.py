"""LLM wrapper that records spans, latency, token usage and cost for every call."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from opentelemetry.trace import SpanKind, Status, StatusCode

from stock_agent.config import Settings
from stock_agent.observability import current_usage, get_telemetry

log = logging.getLogger("stock_agent.llm")


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider != "ollama":
        raise ValueError(f"Unsupported llm_provider: {settings.llm_provider}")
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=settings.llm_temperature,
        client_kwargs={"timeout": settings.llm_timeout_s},
    )


class InstrumentedLLM:
    """Wraps a tool-bound chat model; one `chat <model>` span per call (OTel GenAI semconv)."""

    def __init__(self, model: BaseChatModel, tools: Sequence[BaseTool], settings: Settings) -> None:
        self._runnable = model.bind_tools(list(tools))
        self._settings = settings
        self._model_name = getattr(model, "model", None) or getattr(model, "model_name", "unknown")
        parsed = urlparse(settings.ollama_base_url)
        self._base_attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": settings.llm_provider,
            "gen_ai.request.model": self._model_name,
        }
        self._span_attrs = {
            **self._base_attrs,
            "gen_ai.request.temperature": settings.llm_temperature,
            "server.address": parsed.hostname or "",
            "server.port": parsed.port or 80,
        }

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        s = self._settings
        return input_tokens / 1000 * s.llm_cost_per_1k_input_tokens + output_tokens / 1000 * s.llm_cost_per_1k_output_tokens

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        tel = get_telemetry()
        inst = tel.instruments
        with tel.tracer.start_as_current_span(
            f"chat {self._model_name}", kind=SpanKind.CLIENT, attributes=self._span_attrs
        ) as span:
            req = current_usage()
            step_id = req.step_start("llm", self._model_name) if req is not None else None
            start = time.perf_counter()
            try:
                response: AIMessage = await self._runnable.ainvoke(list(messages))
            except Exception as exc:
                elapsed = time.perf_counter() - start
                error_type = type(exc).__name__
                if req is not None and step_id is not None:
                    req.step_end(step_id, "error", error_type=error_type)
                span.set_attribute("error.type", error_type)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, error_type))
                inst.llm_duration.record(elapsed, {**self._base_attrs, "error.type": error_type})
                log.error("LLM call failed", exc_info=exc,
                          extra={"event": "llm.call", "status": "error", "error_type": error_type,
                                 "model": self._model_name, "duration_ms": round(elapsed * 1000, 2)})
                raise

            elapsed = time.perf_counter() - start
            usage = response.usage_metadata or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            meta = response.response_metadata or {}
            response_model = meta.get("model") or meta.get("model_name") or self._model_name
            finish_reason = meta.get("done_reason") or meta.get("finish_reason") or ("tool_calls" if response.tool_calls else "stop")
            tool_names = [tc["name"] for tc in response.tool_calls]
            cost = self._cost(input_tokens, output_tokens)

            attrs = {**self._base_attrs, "gen_ai.response.model": response_model}
            inst.llm_duration.record(elapsed, attrs)
            inst.llm_tokens.record(input_tokens, {**attrs, "gen_ai.token.type": "input"})
            inst.llm_tokens.record(output_tokens, {**attrs, "gen_ai.token.type": "output"})
            if cost:
                inst.llm_cost.add(cost, attrs)
            if req is not None and step_id is not None:
                req.add_llm(input_tokens, output_tokens, cost)
                req.step_end(step_id, "ok", input_tokens=input_tokens, output_tokens=output_tokens,
                             requested_tools=tool_names, finish_reason=str(finish_reason))

            span.set_attributes({
                "gen_ai.response.model": response_model,
                "gen_ai.response.finish_reasons": [str(finish_reason)],
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
                "gen_ai.response.tool_calls": tool_names,
            })
            log.info(
                "LLM call completed",
                extra={"event": "llm.call", "status": "ok", "model": response_model,
                       "input_tokens": input_tokens, "output_tokens": output_tokens,
                       "duration_ms": round(elapsed * 1000, 2), "finish_reason": str(finish_reason),
                       "requested_tools": ",".join(tool_names) or None},
            )
            if log.isEnabledFor(logging.DEBUG):
                log.debug("LLM completion", extra={"event": "llm.completion", "content": tel.sanitize(response.content)})
            return response
