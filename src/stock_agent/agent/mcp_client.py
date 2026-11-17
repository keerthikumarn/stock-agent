"""Client-side MCP observability (agent -> MCP server).

Implemented as langchain-mcp-adapters tool-call interceptors:

  TelemetryInterceptor (outermost)  one CLIENT span + log pair + latency metric
                                    per logical tool call; injects W3C trace
                                    context into the HTTP headers so the MCP
                                    server's spans join the same trace.
  RetryInterceptor                  retries only when the request never reached
                                    the server (connect errors), with backoff.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from stock_agent.config import Settings
from stock_agent.observability import current_usage, get_telemetry

log = logging.getLogger("stock_agent.mcp.client")

Handler = Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]]


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_exceptions(sub)]
    out = [exc]
    if exc.__cause__ is not None:
        out.extend(_leaf_exceptions(exc.__cause__))
    return out


def classify_client_error(exc: BaseException) -> str:
    for leaf in _leaf_exceptions(exc):
        if isinstance(leaf, (httpx.ConnectError, ConnectionRefusedError)):
            return "connect_error"
        if isinstance(leaf, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(leaf, httpx.HTTPStatusError):
            return f"http_{leaf.response.status_code}"
        if isinstance(leaf, McpError):
            return "mcp_error"
    return type(exc).__name__


_STEP_RESULT_CHARS = 8000


def _safe_args(args: dict) -> dict:
    """Arguments as returned to API clients: redacted, values bounded."""
    from stock_agent.observability.logging import _redact

    return {k: (v[:4000] if isinstance(v, str) else v) for k, v in _redact(dict(args)).items()}


def _result_text(result: MCPToolCallResult) -> str:
    if isinstance(result, CallToolResult):
        return "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    return str(getattr(result, "content", result))


class TelemetryInterceptor:
    def __init__(self, settings: Settings) -> None:
        parsed = urlparse(settings.mcp_server_url)
        self._server_attrs = {
            "server.address": parsed.hostname or "",
            "server.port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "network.transport": "tcp",
            "network.protocol.name": "http",
        }

    async def __call__(self, request: MCPToolCallRequest, handler: Handler) -> MCPToolCallResult:
        tel = get_telemetry()
        name = request.name
        metric_attrs = {"mcp.method.name": "tools/call", "gen_ai.tool.name": name, "mcp.server.name": request.server_name}
        span_attrs = {**metric_attrs, **self._server_attrs, "gen_ai.operation.name": "execute_tool"}
        usage = current_usage()
        log_ctx = {"event": "mcp.request", "direction": "outbound", "mcp_method": "tools/call", "tool": name,
                   "mcp_server": request.server_name}

        with tel.tracer.start_as_current_span(f"tools/call {name}", kind=SpanKind.CLIENT, attributes=span_attrs) as span:
            headers = dict(request.headers or {})
            propagate.inject(headers)  # traceparent / tracestate / baggage
            log.info("MCP tool call started", extra={**log_ctx, "arguments": tel.sanitize(request.args)})
            step_id = usage.step_start("tool", name, args=_safe_args(request.args)) if usage else None
            start = time.perf_counter()
            try:
                result = await handler(request.override(headers=headers))
            except Exception as exc:  # transport-level failure: degrade gracefully
                elapsed = time.perf_counter() - start
                error_type = classify_client_error(exc)
                span.set_attribute("error.type", error_type)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, error_type))
                tel.instruments.mcp_client_duration.record(elapsed, {**metric_attrs, "error.type": error_type})
                if usage:
                    usage.add_tool(failed=True)
                    usage.step_end(step_id, "error", error_type=error_type,
                                   result=f"Tool '{name}' is currently unavailable ({error_type}).")
                log.error(
                    "MCP tool call failed",
                    exc_info=exc,
                    extra={**log_ctx, "event": "mcp.response", "status": "error", "error_type": error_type,
                           "duration_ms": round(elapsed * 1000, 2)},
                )
                # Let the LLM see a tool error instead of crashing the whole request.
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Tool '{name}' is currently unavailable ({error_type}).")],
                    isError=True,
                )

            elapsed = time.perf_counter() - start
            is_error = isinstance(result, CallToolResult) and bool(result.isError)
            attrs = {**metric_attrs, "error.type": "tool_error"} if is_error else metric_attrs
            tel.instruments.mcp_client_duration.record(elapsed, attrs)
            text = _result_text(result)
            if usage:
                usage.add_tool(failed=is_error)
                usage.step_end(step_id, "error" if is_error else "ok",
                               error_type="tool_error" if is_error else None, result=text[:_STEP_RESULT_CHARS])
            if is_error:
                span.set_attribute("error.type", "tool_error")
                span.set_status(Status(StatusCode.ERROR, "tool_error"))
            span.set_attribute("mcp.response.size", len(text))
            log.log(
                logging.WARNING if is_error else logging.INFO,
                "MCP tool call completed",
                extra={**log_ctx, "event": "mcp.response", "status": "tool_error" if is_error else "ok",
                       "duration_ms": round(elapsed * 1000, 2), "response_chars": len(text),
                       "result": tel.sanitize(text)},
            )
            return result


class RetryInterceptor:
    """Retry only failures where the request provably never reached the server."""

    def __init__(self, retries: int, base_delay_s: float = 0.25) -> None:
        self.retries = retries
        self.base_delay_s = base_delay_s

    async def __call__(self, request: MCPToolCallRequest, handler: Handler) -> MCPToolCallResult:
        attempt = 0
        while True:
            try:
                return await handler(request)
            except Exception as exc:
                if attempt >= self.retries or classify_client_error(exc) != "connect_error":
                    raise
                attempt += 1
                delay = self.base_delay_s * (2 ** (attempt - 1)) * (1 + random.random() * 0.2)  # noqa: S311
                trace.get_current_span().add_event("mcp.retry", {"attempt": attempt, "delay_s": delay})
                log.warning("MCP connect failed, retrying",
                            extra={"event": "mcp.retry", "tool": request.name, "attempt": attempt,
                                   "delay_ms": round(delay * 1000)})
                await asyncio.sleep(delay)


def build_mcp_client(settings: Settings) -> MultiServerMCPClient:
    headers = {}
    if settings.mcp_auth_token is not None:
        headers["Authorization"] = f"Bearer {settings.mcp_auth_token.get_secret_value()}"
    connection = {
        "transport": "streamable_http",
        "url": settings.mcp_server_url,
        "headers": headers,
        "timeout": timedelta(seconds=settings.mcp_tool_timeout_s),
        "sse_read_timeout": timedelta(seconds=settings.mcp_tool_timeout_s),
        "session_kwargs": {"read_timeout_seconds": timedelta(seconds=settings.mcp_tool_timeout_s)},
    }
    return MultiServerMCPClient(
        {settings.mcp_server_name: connection},
        tool_interceptors=[TelemetryInterceptor(settings), RetryInterceptor(settings.mcp_connect_retries)],
    )
