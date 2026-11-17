"""Generates deploy/grafana/dashboards/agent-mcp-overview.json (dashboards as code).

Run:  python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

TOOL = 'gen_ai_tool_name=~"$tool"'
ENV = 'deployment_environment_name=~"$env"'
MODEL = 'gen_ai_request_model=~"$model"'

_panels: list[dict] = []
_id = 0
_y = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def row(title: str) -> None:
    global _y
    _panels.append({"type": "row", "title": title, "id": _next_id(), "collapsed": False,
                    "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y}, "panels": []})
    _y += 1


def _target(expr: str, legend: str = "", ref: str = "A", **kw) -> dict:
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref, "range": True, **kw}


def stat(title, expr, x, w=4, h=4, unit="short", thresholds=None, desc="", decimals=None):
    steps = [{"color": "green", "value": None}] + [{"color": c, "value": v} for v, c in (thresholds or [])]
    defaults = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}, "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    _panels.append({
        "type": "stat", "title": title, "description": desc, "id": _next_id(), "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": _y},
        "targets": [_target(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "value"},
    })


def ts(title, targets, x, w=12, h=8, unit="short", desc="", stack=False, bars=False, max_=None):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 60 if bars else 10,
              "showPoints": "never", "spanNulls": True,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if max_ is not None:
        defaults["max"] = max_
    _panels.append({
        "type": "timeseries", "title": title, "description": desc, "id": _next_id(), "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": _y},
        "targets": [_target(e, l, chr(65 + i), exemplar=True) for i, (e, l) in enumerate(targets)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    })


def advance(h: int) -> None:
    global _y
    _y += h


# --------------------------------------------------------------------------- #
row("Service health")
stat("Agent requests / min", f"sum(rate(agent_request_duration_seconds_count{{{ENV}}}[5m])) * 60", 0, unit="reqpm", decimals=1)
stat("Agent error rate", f"(sum(rate(agent_request_duration_seconds_count{{{ENV},error_type!=\"\"}}[5m])) or vector(0)) / sum(rate(agent_request_duration_seconds_count{{{ENV}}}[5m]))",
     4, unit="percentunit", thresholds=[(0.01, "orange"), (0.05, "red")])
stat("Agent p95 latency", f"histogram_quantile(0.95, sum by (le) (rate(agent_request_duration_seconds_bucket{{{ENV}}}[5m])))",
     8, unit="s", thresholds=[(30, "orange"), (60, "red")])
stat("Tool calls / min", f"sum(rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\"}}[5m])) * 60", 12, unit="short", decimals=1)
stat("Tool failure rate (system)",
     f"(sum(rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",error_type!=\"\",error_type!=\"tool_input_error\"}}[5m])) or vector(0)) / sum(rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\"}}[5m]))",
     16, unit="percentunit", thresholds=[(0.01, "orange"), (0.05, "red")], desc="Excludes tool_input_error (bad arguments produced by the LLM).")
stat("Tokens / min", f"sum(rate(gen_ai_client_token_usage_sum{{{ENV}}}[5m])) * 60", 20, unit="short", decimals=0)
advance(4)
stat("MCP server up", f"count(process_uptime_seconds{{service_name=\"mcp-tools\",{ENV}}}) or vector(0)", 0, w=3, unit="none",
     thresholds=[], desc="Instances sending a heartbeat")
_panels[-1]["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
stat("Agent up", f"count(process_uptime_seconds{{service_name=\"stock-agent\",{ENV}}}) or vector(0)", 3, w=3, unit="none")
_panels[-1]["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
stat("In-flight agent requests", f"sum(agent_active_requests{{{ENV}}})", 6, w=3)
stat("Tokens (24h)", f"sum(increase(gen_ai_client_token_usage_sum{{{ENV}}}[24h]))", 9, w=4, unit="short", decimals=0)
stat("Est. LLM cost (24h)", f"sum(increase(gen_ai_client_cost_total{{{ENV}}}[24h])) or vector(0)", 13, w=4, unit="currencyUSD", decimals=2,
     desc="From LLM_COST_PER_1K_* settings; 0 for self-hosted Ollama.")
_panels.append({"type": "alertlist", "title": "Firing alerts", "id": _next_id(),
                "gridPos": {"h": 4, "w": 7, "x": 17, "y": _y},
                "options": {"alertInstanceLabelFilter": "", "datasource": "Prometheus", "groupMode": "default",
                            "maxItems": 10, "showInstances": True, "sortOrder": 3,
                            "stateFilter": {"firing": True, "pending": True, "error": True, "normal": False},
                            "viewMode": "list"}})
advance(4)

# --------------------------------------------------------------------------- #
row("MCP tools — latency & failures")
ts("Tool calls / s by tool", [
    (f"sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval]))", "{{gen_ai_tool_name}}"),
], 0, unit="reqps")
ts("Tool failures / s by error type", [
    (f"sum by (gen_ai_tool_name, error_type) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",{TOOL},error_type!=\"\"}}[$__rate_interval]))",
     "{{gen_ai_tool_name}} · {{error_type}}"),
], 12, unit="reqps", stack=True, bars=True, desc="tool_input_error = LLM sent invalid arguments; anything else is a tool/system failure.")
advance(8)
ts("Tool failure ratio (system errors)", [
    (f"(sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",{TOOL},error_type!=\"\",error_type!=\"tool_input_error\"}}[5m])) "
     f"or sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[5m])) * 0) "
     f"/ sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[5m]))", "{{gen_ai_tool_name}}"),
], 0, unit="percentunit", max_=1)
ts("Tool latency (server) p50 / p95 / p99", [
    (f"histogram_quantile(0.50, sum by (le, gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_bucket{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval])))", "p50 {{gen_ai_tool_name}}"),
    (f"histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_bucket{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval])))", "p95 {{gen_ai_tool_name}}"),
    (f"histogram_quantile(0.99, sum by (le, gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_bucket{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval])))", "p99 {{gen_ai_tool_name}}"),
], 12, unit="s")
advance(8)
ts("p95 latency: agent-observed vs server (network + MCP overhead)", [
    (f"histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate(mcp_client_operation_duration_seconds_bucket{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval])))", "client {{gen_ai_tool_name}}"),
    (f"histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_bucket{{{ENV},mcp_method_name=\"tools/call\",{TOOL}}}[$__rate_interval])))", "server {{gen_ai_tool_name}}"),
], 0, unit="s")
ts("Agent → MCP transport errors", [
    (f"sum by (gen_ai_tool_name, error_type) (rate(mcp_client_operation_duration_seconds_count{{{ENV},error_type!=\"\",error_type!=\"tool_error\"}}[$__rate_interval]))", "{{gen_ai_tool_name}} · {{error_type}}"),
    (f"sum by (mcp_method_name) (rate(mcp_server_operation_duration_seconds_count{{{ENV},mcp_method_name!=\"tools/call\"}}[$__rate_interval]))", "server {{mcp_method_name}} / s"),
], 12, unit="reqps", desc="connect_error / timeout / http_* = the request never completed at the transport level.")
advance(8)

# --------------------------------------------------------------------------- #
row("LLM — tokens, latency, errors")
ts("Tokens / s by model & type", [
    (f"sum by (gen_ai_request_model, gen_ai_token_type) (rate(gen_ai_client_token_usage_sum{{{ENV},{MODEL}}}[$__rate_interval]))", "{{gen_ai_request_model}} · {{gen_ai_token_type}}"),
], 0, unit="short", stack=True)
ts("Tokens per LLM call (p50 / p95)", [
    (f"histogram_quantile(0.50, sum by (le, gen_ai_token_type) (rate(gen_ai_client_token_usage_bucket{{{ENV},{MODEL}}}[$__rate_interval])))", "p50 {{gen_ai_token_type}}"),
    (f"histogram_quantile(0.95, sum by (le, gen_ai_token_type) (rate(gen_ai_client_token_usage_bucket{{{ENV},{MODEL}}}[$__rate_interval])))", "p95 {{gen_ai_token_type}}"),
], 12, unit="short")
advance(8)
ts("LLM latency p50 / p95", [
    (f"histogram_quantile(0.50, sum by (le, gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_bucket{{{ENV},{MODEL}}}[$__rate_interval])))", "p50 {{gen_ai_request_model}}"),
    (f"histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_bucket{{{ENV},{MODEL}}}[$__rate_interval])))", "p95 {{gen_ai_request_model}}"),
], 0, w=8, unit="s")
ts("LLM calls / s (API usage)", [
    (f"sum by (gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_count{{{ENV},{MODEL},error_type=\"\"}}[$__rate_interval]))", "ok {{gen_ai_request_model}}"),
    (f"sum by (gen_ai_request_model, error_type) (rate(gen_ai_client_operation_duration_seconds_count{{{ENV},{MODEL},error_type!=\"\"}}[$__rate_interval]))", "error {{error_type}}"),
], 8, w=8, unit="reqps", stack=True)
ts("Estimated spend (USD / hour)", [
    (f"sum by (gen_ai_request_model) (rate(gen_ai_client_cost_total{{{ENV}}}[$__rate_interval])) * 3600", "{{gen_ai_request_model}}"),
], 16, w=8, unit="currencyUSD")
advance(8)

# --------------------------------------------------------------------------- #
row("Agent behaviour (per request)")
ts("Tokens per agent request", [
    (f"histogram_quantile(0.50, sum by (le) (rate(agent_request_token_usage_bucket{{{ENV}}}[$__rate_interval])))", "p50"),
    (f"histogram_quantile(0.95, sum by (le) (rate(agent_request_token_usage_bucket{{{ENV}}}[$__rate_interval])))", "p95"),
], 0, w=8, unit="short")
ts("Tool & LLM calls per request (p95)", [
    (f"histogram_quantile(0.95, sum by (le) (rate(agent_request_tool_calls_bucket{{{ENV}}}[$__rate_interval])))", "tool calls p95"),
    (f"histogram_quantile(0.95, sum by (le) (rate(agent_request_llm_calls_bucket{{{ENV}}}[$__rate_interval])))", "LLM calls p95"),
], 8, w=8, unit="short", desc="A sustained rise means the agent is looping.")
ts("Agent outcomes", [
    (f"sum(rate(agent_request_duration_seconds_count{{{ENV},error_type=\"\"}}[$__rate_interval]))", "ok"),
    (f"sum by (error_type) (rate(agent_request_duration_seconds_count{{{ENV},error_type!=\"\"}}[$__rate_interval]))", "{{error_type}}"),
], 16, w=8, unit="reqps", stack=True, bars=True)
advance(8)

# --------------------------------------------------------------------------- #
row("Traces & logs")
_panels.append({
    "type": "nodeGraph", "title": "Service graph (from traces)", "id": _next_id(), "datasource": TEMPO,
    "gridPos": {"h": 10, "w": 10, "x": 0, "y": _y},
    "targets": [{"datasource": TEMPO, "queryType": "serviceMap", "refId": "A"}],
})
_panels.append({
    "type": "table", "title": "Recent failed / slow traces", "id": _next_id(), "datasource": TEMPO,
    "gridPos": {"h": 10, "w": 14, "x": 10, "y": _y},
    "targets": [{"datasource": TEMPO, "queryType": "traceql", "limit": 20, "tableType": "traces", "refId": "A",
                 "query": '{ resource.service.name =~ "stock-agent|mcp-tools" && (status = error || duration > 30s) }'}],
})
advance(10)
_panels.append({
    "type": "logs", "title": "MCP requests & responses", "id": _next_id(), "datasource": LOKI,
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": _y},
    "targets": [{"datasource": LOKI, "refId": "A",
                 "expr": '{service_name=~"stock-agent|mcp-tools"} | event=~"mcp.request|mcp.response"'}],
    "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True, "sortOrder": "Descending",
                "dedupStrategy": "none", "prettifyLogMessage": False},
})
advance(12)
_panels.append({
    "type": "logs", "title": "Errors & warnings (all services)", "id": _next_id(), "datasource": LOKI,
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": _y},
    "targets": [{"datasource": LOKI, "refId": "A",
                 "expr": '{service_name=~"stock-agent|mcp-tools"} | severity_text=~"(?i)error|warn.*|critical"'}],
    "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True, "sortOrder": "Descending"},
})
advance(10)


def _var(name, label, query, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": PROM, "refresh": 2,
            "query": {"query": query, "refId": f"{name}-var"}, "definition": query,
            "includeAll": include_all, "multi": True, "allValue": ".*",
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}


dashboard = {
    "uid": "agent-mcp-overview",
    "title": "AI Agent & MCP — Overview",
    "description": "Tool latency/failures, token usage, LLM behaviour, traces and logs for the stock research agent.",
    "tags": ["ai-agent", "mcp", "llm", "opentelemetry"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "templating": {"list": [
        _var("env", "Environment", "label_values(target_info, deployment_environment_name)"),
        _var("tool", "Tool", 'label_values(mcp_server_operation_duration_seconds_count{mcp_method_name="tools/call"}, gen_ai_tool_name)'),
        _var("model", "Model", "label_values(gen_ai_client_token_usage_count, gen_ai_request_model)"),
    ]},
    "links": [{"title": "Explore traces", "type": "link", "url": "/explore?left=%7B%22datasource%22:%22tempo%22%7D", "targetBlank": True}],
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
                              "hide": True, "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": _panels,
}

out = Path(__file__).resolve().parents[1] / "deploy/grafana/dashboards/agent-mcp-overview.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(_panels)} panels)")
