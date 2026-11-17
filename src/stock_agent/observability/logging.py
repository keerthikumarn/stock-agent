"""Structured logging with trace correlation and payload sanitisation.

* stdout: one JSON object per line (12-factor; picked up by any log shipper).
* OTLP:   the same records are exported to the collector -> Loki, carrying the
          active trace/span ids so Grafana can jump log <-> trace.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

# Attributes every LogRecord has; anything else came in via `extra=`.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName", "color_message"}

_SECRET_KEY = re.compile(
    r"(?i)^(.*[_-])?(password|passwd|secret|token|api[_-]?key|authorization|cookie|credentials?|bearer)$"
)
_SECRET_VALUE = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}")
REDACTED = "***REDACTED***"


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.match(k) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUE.sub(r"\1" + REDACTED, obj)
    return obj


class PayloadSanitizer:
    """Turns arbitrary tool args/results into a bounded, redacted string."""

    def __init__(self, enabled: bool = True, max_chars: int = 2000) -> None:
        self.enabled = enabled
        self.max_chars = max_chars

    def __call__(self, payload: Any) -> str:
        if isinstance(payload, str):
            text = _redact(payload)
        else:
            try:
                text = json.dumps(_redact(payload), default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                text = _redact(repr(payload))
        if not self.enabled:
            return f"<omitted: {len(text)} chars>"
        if len(text) > self.max_chars:
            return f"{text[: self.max_chars]}...[truncated {len(text) - self.max_chars} chars]"
        return text


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            doc["trace_id"] = format(ctx.trace_id, "032x")
            doc["span_id"] = format(ctx.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                doc[key] = _json_safe(value)
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        ctx = trace.get_current_span().get_span_context()
        tid = f" trace={format(ctx.trace_id, '032x')[:12]}" if ctx.is_valid else ""
        return f"{base}{tid} {extras}" if extras else f"{base}{tid}"


def configure_logging(service_name: str, level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(service_name))
    else:
        handler.setFormatter(ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.set_name("stdout")

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "stdout"]
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party noise: keep warnings, drop per-request chatter.
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access", "mcp.client.streamable_http",
                  "mcp.server.lowlevel.server", "mcp.server.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
