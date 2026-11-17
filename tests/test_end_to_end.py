"""Agent -> MCP server over real HTTP, with in-memory telemetry backends.

Verifies the observability contract: one distributed trace across both
services, latency/failure metrics with the right attributes, and token usage.
"""

from __future__ import annotations

import httpx
from conftest import metric_points
from fakes import ScriptedToolModel
from opentelemetry.trace import SpanKind, StatusCode

from stock_agent.agent.service import AgentService


def _agent(settings, script):
    return AgentService(settings, model_factory=lambda _: ScriptedToolModel(script=script))


async def test_trace_spans_agent_and_mcp_server(settings, mcp_server, spans):
    agent = _agent(settings, [("stock_data_tool", {"company_ticker": "AAPL", "num_days": 30})])
    result = await agent.run("How did AAPL do?", session_id="s-1")

    assert "Stock performance data" in result.answer
    finished = spans.get_finished_spans()
    by_name = {}
    for s in finished:
        by_name.setdefault((s.name, s.kind), []).append(s)

    root = by_name[("invoke_agent stock-research-agent", SpanKind.INTERNAL)][0]
    client = by_name[("tools/call stock_data_tool", SpanKind.CLIENT)][0]
    server = by_name[("tools/call stock_data_tool", SpanKind.SERVER)][0]
    chats = by_name[("chat fake-llm", SpanKind.CLIENT)]

    trace_id = root.context.trace_id
    assert format(trace_id, "032x") == result.trace_id
    # One trace across the agent and the MCP server.
    assert {client.context.trace_id, server.context.trace_id, *(c.context.trace_id for c in chats)} == {trace_id}
    # The server span is a descendant of the agent's client span (via the HTTP spans).
    span_ids = {s.context.span_id: s for s in finished}
    node, chain = server, []
    while node.parent is not None and node.parent.span_id in span_ids:
        node = span_ids[node.parent.span_id]
        chain.append(node)
    assert client in chain and root in chain
    assert server.attributes["gen_ai.tool.name"] == "stock_data_tool"
    assert server.attributes["mcp.method.name"] == "tools/call"
    assert chats[0].attributes["gen_ai.usage.input_tokens"] > 0
    assert root.attributes["agent.tool_calls"] == 1
    assert result.usage["llm_calls"] == 2 and result.usage["total_tokens"] > 0


async def test_input_errors_are_classified_and_not_fatal(settings, mcp_server, spans):
    agent = _agent(settings, [("stock_data_tool", {"company_ticker": "../../etc/passwd", "num_days": 3})])
    result = await agent.run("hack")

    assert result.usage["tool_errors"] == 1
    server = next(s for s in spans.get_finished_spans()
                  if s.name == "tools/call stock_data_tool" and s.kind == SpanKind.SERVER)
    assert server.status.status_code == StatusCode.ERROR
    assert server.attributes["error.type"] == "tool_input_error"
    points = metric_points("mcp.server.operation.duration")
    assert any(p.attributes.get("error.type") == "tool_input_error"
               and p.attributes.get("gen_ai.tool.name") == "stock_data_tool" for p in points)


async def test_token_and_latency_metrics_recorded(settings, mcp_server):
    await _agent(settings, [("stock_data_tool", {"company_ticker": "AAPL", "num_days": 5})]).run("q")

    tokens = metric_points("gen_ai.client.token.usage")
    assert {p.attributes["gen_ai.token.type"] for p in tokens} == {"input", "output"}
    assert sum(p.sum for p in tokens) > 0
    client = metric_points("mcp.client.operation.duration")
    assert any(p.attributes.get("gen_ai.tool.name") == "stock_data_tool" and "error.type" not in p.attributes
               for p in client)
    assert metric_points("agent.request.duration")
    assert metric_points("agent.request.token.usage")


async def test_python_tool_runs_sandboxed(settings, mcp_server):
    code = "print(sum(range(10)))"
    result = await _agent(settings, [("python_repl_tool", {"code": code})]).run("q")
    assert "45" in result.answer


async def test_mcp_endpoint_requires_token(settings, mcp_server):
    async with httpx.AsyncClient() as c:
        r = await c.post(mcp_server, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


async def test_unreachable_mcp_server_degrades_gracefully(settings, mcp_server, spans):
    agent = _agent(settings, [("stock_data_tool", {"company_ticker": "AAPL", "num_days": 5})])
    await agent.initialise()
    # Point the already-built tools at a dead port by swapping the client connection.
    for conn in agent._client.connections.values():
        conn["url"] = "http://127.0.0.1:1/mcp"
    result = await agent.run("q")
    tool_msg = next(m for m in result.messages if m.type == "tool")
    assert "currently unavailable (connect_error)" in str(tool_msg.content)
    assert result.usage["tool_errors"] == 1
    client = next(s for s in spans.get_finished_spans()
                  if s.name == "tools/call stock_data_tool" and s.kind == SpanKind.CLIENT)
    assert client.attributes["error.type"] == "connect_error"
