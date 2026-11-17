from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
sys.path.insert(0, str(TESTS))

# In-memory telemetry backends, installed before the app initialises telemetry.
os.environ["OTEL_ENABLED"] = "false"
os.environ["LOG_FORMAT"] = "json"
os.environ["MCP_AUTH_TOKEN"] = "test-token-0123456789"

from opentelemetry import metrics, trace  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
_tp = TracerProvider()
_tp.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_tp)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("data")
    shutil.copy(TESTS / "AAPL_sample.csv", d / "AAPL.csv")
    return d


@pytest.fixture(scope="session")
def settings(data_dir, tmp_path_factory):
    from stock_agent.config import Settings, get_settings

    port = _free_port()
    os.environ.update({
        "DATA_DIR": str(data_dir),
        "CHARTS_DIR": str(tmp_path_factory.mktemp("charts")),
        "MCP_PORT": str(port),
        "MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp",
        "MCP_CONNECT_RETRIES": "0",
    })
    get_settings.cache_clear()
    s = get_settings()
    from stock_agent.observability import init_telemetry

    init_telemetry("test", s)
    assert isinstance(s, Settings)
    return s


@pytest.fixture(scope="session")
def mcp_server(settings):
    import uvicorn

    from stock_agent.mcp_server.app import build_app

    server = uvicorn.Server(uvicorn.Config(build_app(settings), host="127.0.0.1", port=settings.mcp_port,
                                           log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield settings.mcp_server_url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def spans():
    SPANS.clear()
    return SPANS


def metric_points(name: str) -> list:
    data = METRICS.get_metrics_data()
    out = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out
