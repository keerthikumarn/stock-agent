"""Metric instruments.

Names and attributes follow the OpenTelemetry semantic conventions for
GenAI (`gen_ai.*`) and MCP (`mcp.*`) so any OTel-compatible backend
understands them. In Prometheus they appear as e.g.
`mcp_client_operation_duration_seconds_bucket` and
`gen_ai_client_token_usage_bucket`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

# Semconv-recommended bucket boundaries.
DURATION_BUCKETS_S = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92, 163.84]
TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]
COUNT_BUCKETS = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30]

_START = time.monotonic()


@dataclass
class Instruments:
    # MCP
    mcp_client_duration: metrics.Histogram
    mcp_server_duration: metrics.Histogram
    mcp_server_active: metrics.UpDownCounter
    # LLM
    llm_duration: metrics.Histogram
    llm_tokens: metrics.Histogram
    llm_cost: metrics.Counter
    # Agent request level
    agent_request_duration: metrics.Histogram
    agent_request_tokens: metrics.Histogram
    agent_request_tool_calls: metrics.Histogram
    agent_request_llm_calls: metrics.Histogram
    agent_active: metrics.UpDownCounter
    _keep: list = field(default_factory=list)


def _uptime(_: CallbackOptions):
    yield Observation(time.monotonic() - _START)


def create_instruments(meter_name: str = "stock_agent") -> Instruments:
    m = metrics.get_meter(meter_name)
    uptime = m.create_observable_gauge(
        "process.uptime", callbacks=[_uptime], unit="s", description="Seconds since process start (liveness heartbeat)"
    )
    return Instruments(
        mcp_client_duration=m.create_histogram(
            "mcp.client.operation.duration",
            unit="s",
            description="Duration of MCP requests as observed by the agent (includes network)",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        ),
        mcp_server_duration=m.create_histogram(
            "mcp.server.operation.duration",
            unit="s",
            description="Duration of MCP requests as processed by the MCP server",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        ),
        mcp_server_active=m.create_up_down_counter(
            "mcp.server.active_requests", unit="{request}", description="In-flight MCP requests on the server"
        ),
        llm_duration=m.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="LLM call duration",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        ),
        llm_tokens=m.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Tokens per LLM call, split by gen_ai.token.type (input|output)",
            explicit_bucket_boundaries_advisory=TOKEN_BUCKETS,
        ),
        llm_cost=m.create_counter(
            "gen_ai.client.cost", unit="{USD}", description="Estimated LLM spend in USD (from configured prices)"
        ),
        agent_request_duration=m.create_histogram(
            "agent.request.duration",
            unit="s",
            description="End-to-end agent request duration",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        ),
        agent_request_tokens=m.create_histogram(
            "agent.request.token.usage",
            unit="{token}",
            description="Total tokens consumed by one agent request",
            explicit_bucket_boundaries_advisory=TOKEN_BUCKETS,
        ),
        agent_request_tool_calls=m.create_histogram(
            "agent.request.tool_calls",
            unit="{call}",
            description="Tool calls made while serving one agent request",
            explicit_bucket_boundaries_advisory=COUNT_BUCKETS,
        ),
        agent_request_llm_calls=m.create_histogram(
            "agent.request.llm_calls",
            unit="{call}",
            description="LLM round-trips made while serving one agent request",
            explicit_bucket_boundaries_advisory=COUNT_BUCKETS,
        ),
        agent_active=m.create_up_down_counter(
            "agent.active_requests", unit="{request}", description="In-flight agent requests"
        ),
        _keep=[uptime],
    )
