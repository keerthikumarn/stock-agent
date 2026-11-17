"""Server-side MCP observability.

* ObservedFastMCP  - wraps every tools/call and tools/list with a SERVER span,
                     structured request/response logs and duration metrics.
                     The span joins the caller's trace (W3C traceparent from the
                     HTTP request, or from params._meta as per the MCP semconv).
* McpHttpMiddleware - ASGI middleware: optional bearer-token auth, access log
                     with the JSON-RPC method, and hand-off of the HTTP server
                     span context to the MCP handler.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode

from stock_agent.mcp_server.tools import ToolInputError
from stock_agent.observability import get_telemetry

log = logging.getLogger("stock_agent.mcp.server")

_STATE_OTEL_CTX = "otel_context"


def classify_error(exc: BaseException) -> str:
    """Low-cardinality error.type for metrics/spans."""
    cause = exc.__cause__ or exc
    if isinstance(cause, ToolInputError) or isinstance(exc, ToolInputError):
        return "tool_input_error"
    if isinstance(exc, ToolError) and exc.__cause__ is None and str(exc).startswith("Unknown tool"):
        return "unknown_tool"
    return type(cause).__name__


def _serialise_result(result: Any) -> Any:
    if isinstance(result, (list, tuple)):
        return [getattr(r, "model_dump", lambda **_: r)(mode="json", exclude_none=True) for r in result]
    return result


class ObservedFastMCP(FastMCP):
    """FastMCP with built-in telemetry for every tool — no per-tool decorators to forget."""

    def _parent_context(self) -> tuple[otel_context.Context | None, dict[str, Any]]:
        attrs: dict[str, Any] = {}
        try:
            rc = self.get_context().request_context
        except (LookupError, ValueError):
            return None, attrs

        attrs["jsonrpc.request.id"] = str(rc.request_id)
        carrier: dict[str, str] = {}
        request = rc.request
        if request is not None:
            headers = getattr(request, "headers", {})
            if sid := headers.get("mcp-session-id"):
                attrs["mcp.session.id"] = sid
            if pv := headers.get("mcp-protocol-version"):
                attrs["mcp.protocol.version"] = pv
            # Preferred parent: the HTTP server span created by the ASGI instrumentation.
            state = getattr(request, "scope", {}).get("state") or {}
            if (ctx := state.get(_STATE_OTEL_CTX)) is not None:
                return ctx, attrs
            carrier.update({k.lower(): v for k, v in headers.items()})
        # MCP semconv: trace context may also travel in params._meta.
        if rc.meta is not None:
            extra = rc.meta.model_dump(exclude_none=True)
            carrier.update({k: str(v) for k, v in extra.items() if k in ("traceparent", "tracestate", "baggage")})
        return (propagate.extract(carrier) if carrier else None), attrs

    async def _observe(
        self, method: str, target: str | None, arguments: Any, call: Callable[[], Awaitable[Any]]
    ) -> Any:
        tel = get_telemetry()
        inst = tel.instruments
        parent, attrs = self._parent_context()
        span_name = f"{method} {target}" if target else method
        metric_attrs: dict[str, Any] = {"mcp.method.name": method}
        attrs["mcp.method.name"] = method
        if target:
            metric_attrs["gen_ai.tool.name"] = target
            attrs["gen_ai.tool.name"] = target
            attrs["gen_ai.operation.name"] = "execute_tool"

        log_ctx = {
            "event": "mcp.request",
            "direction": "inbound",
            "mcp_method": method,
            "tool": target,
            "mcp_session_id": attrs.get("mcp.session.id"),
            "jsonrpc_id": attrs.get("jsonrpc.request.id"),
        }
        with tel.tracer.start_as_current_span(span_name, context=parent, kind=SpanKind.SERVER, attributes=attrs) as span:
            if arguments is not None:
                log.info("MCP request received", extra={**log_ctx, "arguments": tel.sanitize(arguments)})
            inst.mcp_server_active.add(1, metric_attrs)
            start = time.perf_counter()
            try:
                result = await call()
            except Exception as exc:
                elapsed = time.perf_counter() - start
                error_type = classify_error(exc)
                span.set_attribute("error.type", error_type)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:256]))
                inst.mcp_server_duration.record(elapsed, {**metric_attrs, "error.type": error_type})
                log.log(
                    logging.INFO if error_type == "tool_input_error" else logging.ERROR,
                    "MCP request failed",
                    extra={
                        **log_ctx,
                        "event": "mcp.response",
                        "status": "error",
                        "error_type": error_type,
                        "error": tel.sanitize(str(exc)),
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )
                raise
            else:
                elapsed = time.perf_counter() - start
                inst.mcp_server_duration.record(elapsed, metric_attrs)
                payload = tel.sanitize(_serialise_result(result))
                log.info(
                    "MCP response sent",
                    extra={
                        **log_ctx,
                        "event": "mcp.response",
                        "status": "ok",
                        "duration_ms": round(elapsed * 1000, 2),
                        "result": payload if target else None,
                    },
                )
                return result
            finally:
                inst.mcp_server_active.add(-1, metric_attrs)

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        return await self._observe("tools/call", name, arguments, lambda: super(ObservedFastMCP, self).call_tool(name, arguments))

    async def list_tools(self):  # type: ignore[override]
        return await self._observe("tools/list", None, None, lambda: super(ObservedFastMCP, self).list_tools())


# --------------------------------------------------------------------------- #
# ASGI middleware
# --------------------------------------------------------------------------- #
_MAX_SNIFF = 64 * 1024


class McpHttpMiddleware:
    """Bearer auth + access log + span hand-off for the MCP HTTP endpoint."""

    def __init__(self, app, mcp_path: str = "/mcp", auth_token: str | None = None) -> None:
        self.app = app
        self.mcp_path = mcp_path.rstrip("/")
        self._token = auth_token.encode() if auth_token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].rstrip("/").startswith(self.mcp_path):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if self._token is not None:
            supplied = headers.get("authorization", "").removeprefix("Bearer ").encode()
            if not hmac.compare_digest(supplied, self._token):
                log.warning(
                    "MCP request rejected: unauthorised",
                    extra={"event": "mcp.http", "status_code": 401, "client": (scope.get("client") or ("?",))[0]},
                )
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return

        # Hand the current (HTTP server span) context to the MCP handler task.
        scope.setdefault("state", {})[_STATE_OTEL_CTX] = otel_context.get_current()

        body = bytearray()
        status = {"code": 0}

        async def _receive():
            message = await receive()
            if message["type"] == "http.request" and len(body) < _MAX_SNIFF:
                body.extend(message.get("body", b"")[: _MAX_SNIFF - len(body)])
            return message

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, _receive, _send)
        finally:
            rpc_methods: list[str] = []
            if body:
                try:
                    parsed = json.loads(body)
                    items = parsed if isinstance(parsed, list) else [parsed]
                    rpc_methods = [str(i.get("method", "response")) for i in items if isinstance(i, dict)]
                except ValueError:
                    rpc_methods = ["<unparseable>"]
            code = status["code"]
            log.log(
                logging.WARNING if code >= 400 or code == 0 else logging.DEBUG,
                "MCP HTTP exchange",
                extra={
                    "event": "mcp.http",
                    "http_method": scope["method"],
                    "status_code": code,
                    "jsonrpc_methods": ",".join(rpc_methods) or None,
                    "mcp_session_id": headers.get("mcp-session-id"),
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
