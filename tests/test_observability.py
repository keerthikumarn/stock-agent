from __future__ import annotations

import json
import logging

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stock_agent.agent.mcp_client import classify_client_error
from stock_agent.mcp_server.instrumentation import classify_error
from stock_agent.mcp_server.tools import ToolInputError
from stock_agent.observability.logging import REDACTED, JsonFormatter, PayloadSanitizer


class TestPayloadSanitizer:
    def test_redacts_secret_keys_recursively(self):
        out = PayloadSanitizer()({"query": "x", "api_key": "abc", "nested": {"Authorization": "Bearer abc"}})
        assert json.loads(out) == {"query": "x", "api_key": REDACTED, "nested": {"Authorization": REDACTED}}

    def test_redacts_bearer_tokens_in_strings(self):
        assert "s3cr3t-value-xyz" not in PayloadSanitizer()("header was Bearer s3cr3t-value-xyz ok")

    def test_does_not_redact_lookalike_keys(self):
        assert json.loads(PayloadSanitizer()({"max_tokens": 5, "num_days": 3})) == {"max_tokens": 5, "num_days": 3}

    def test_truncates(self):
        out = PayloadSanitizer(max_chars=100)("a" * 1000)
        assert out.startswith("a" * 100) and "truncated 900 chars" in out

    def test_disabled_omits_content(self):
        assert PayloadSanitizer(enabled=False)("secret prompt") == "<omitted: 13 chars>"


def test_json_formatter_includes_extras_and_trace(spans):
    from opentelemetry import trace

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    record.event = "mcp.request"
    with trace.get_tracer("t").start_as_current_span("s") as span:
        doc = json.loads(JsonFormatter("svc").format(record))
    assert doc["event"] == "mcp.request"
    assert doc["trace_id"] == format(span.get_span_context().trace_id, "032x")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ToolError("Error executing tool x: bad"), "ToolError"),
        (ToolInputError("bad ticker"), "tool_input_error"),
        (ToolError("Unknown tool: nope"), "unknown_tool"),
    ],
)
def test_server_error_classification(exc, expected):
    assert classify_error(exc) == expected


def test_server_error_classification_uses_cause():
    try:
        try:
            raise TimeoutError("slow")
        except TimeoutError as inner:
            raise ToolError("Error executing tool x") from inner
    except ToolError as outer:
        assert classify_error(outer) == "TimeoutError"


def test_client_error_classification_unwraps_groups():
    req = httpx.Request("POST", "http://mcp")
    group = ExceptionGroup("tg", [httpx.ConnectError("refused", request=req)])
    assert classify_client_error(group) == "connect_error"
    resp = httpx.Response(503, request=req)
    assert classify_client_error(httpx.HTTPStatusError("x", request=req, response=resp)) == "http_503"
