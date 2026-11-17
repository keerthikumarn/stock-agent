# Stock Research Agent: MCP tools + observability

A LangGraph agent (Ollama `llama3.2`) that answers company and stock questions. Its three tools (Wikipedia, stock CSVs, Python charting) run on a separate **MCP server**. Both services are instrumented with **OpenTelemetry**.

```
                ┌──────────────── one trace (W3C traceparent) ────────────────┐
 client ──HTTP──▶ agent (FastAPI + LangGraph) ──MCP / Streamable HTTP──▶ mcp-server (FastMCP)
                 │  invoke_agent → chat llama3.2 → tools/call X            │  tools/call X
                 └────────────── OTLP (traces, metrics, logs) ─────────────┘
                                        │
                                 OTel Collector ──▶ Tempo (traces) ──▶ service graph → Prometheus
                                        ├────────▶ Prometheus (metrics) ──▶ Alertmanager
                                        └────────▶ Loki (logs)
                                                    └── Grafana: dashboards, log↔trace links
```

## How each requirement is met

| Requirement | Implementation |
|---|---|
| **Centralised logging of MCP requests/responses** | Both ends log every call as structured JSON: `event=mcp.request` and `event=mcp.response`, with tool, arguments, result preview, status, `error_type`, `duration_ms`, `mcp_session_id`, `trace_id` and `span_id`. Logs go to stdout and also over OTLP to **Loki**. Payloads are redacted (keys such as `api_key` and `authorization`, and bearer tokens) and truncated (`LOG_PAYLOAD_MAX_CHARS`). Set `LOG_PAYLOADS=false` to log sizes only. HTTP-level auth failures and 4xx/5xx responses are logged too. |
| **Tool latency and failure rates** | `mcp.server.operation.duration` (server side) and `mcp.client.operation.duration` (agent side, includes network) are histograms labelled by `gen_ai.tool.name` and `error.type`. Failures are classified as follows: `tool_input_error` means the LLM sent bad arguments; an exception class name means the tool itself broke; `connect_error`, `timeout` and `http_*` mean transport problems. Recording rules provide the failure ratio and p50/p95 per tool. |
| **Distributed tracing across agents and MCP servers** | The agent injects W3C trace context into every MCP HTTP request. The server joins that trace through its HTTP span, and falls back to `params._meta`. A single trace contains `POST /v1/query → invoke_agent → chat llama3.2 → tools/call X (client) → POST /mcp → tools/call X (server)`. Tempo derives a service graph from these traces. |
| **Token consumption and API usage** | `gen_ai.client.token.usage` (input/output per model), `gen_ai.client.operation.duration` (LLM latency and call counts), `gen_ai.client.cost` (from configurable prices), and per-request totals: `agent.request.token.usage`, `agent.request.tool_calls`, `agent.request.llm_calls`. Each API response also includes `usage` and `trace_id`. |
| **Dashboards and alerts** | The provisioned Grafana dashboard **AI Agent & MCP — Overview** has 35 panels covering health, tools, LLM, per-request behaviour, the service graph, failed traces and MCP logs. There are 17 Prometheus alerts (below), routed by severity through Alertmanager with inhibition rules, and they are unit-tested with `promtool`. |

Metric and attribute names follow the OpenTelemetry **GenAI** and **MCP** semantic conventions, so the same telemetry works with Datadog, Honeycomb, Grafana Cloud and other backends. To switch backends, only change the collector's exporters.

## Running the app

The whole stack runs with Docker Compose: the agent, the MCP server, the Stock desk web app and the observability stack. The LLM is **Ollama**, running on your machine. These steps were tested on macOS with Docker Desktop; Linux differences are noted where they matter.

### 1. Prerequisites

| Need | Check with | Notes |
|---|---|---|
| Docker with Compose v2 | `docker compose version` | Docker Desktop on macOS/Windows, Docker Engine on Linux. Give Docker at least 6 GB of memory. |
| Ollama | `ollama --version` | https://ollama.com/download |
| The `llama3.2` model | `ollama list` | If missing: `ollama pull llama3.2` |
| Free ports | – | 8088 (Stock desk), 8080 (agent API), 3000 (Grafana), 9090 (Prometheus), 9093 (Alertmanager), 4317/4318 (OTLP) |

### 2. Make sure Docker can reach Ollama

The agent runs in a container and calls Ollama on your machine at `http://host.docker.internal:11434`.

```bash
curl -s http://localhost:11434/api/tags | head -c 100        # Ollama is running
docker run --rm curlimages/curl -s http://host.docker.internal:11434/api/tags | head -c 100   # containers can reach it
```

Both commands should print JSON listing your models.

- **macOS (Docker Desktop):** this normally works as is. If the second command fails, run `launchctl setenv OLLAMA_HOST "0.0.0.0"`, quit Ollama from the menu bar, and open it again.
- **Linux:** by default Ollama listens only on `127.0.0.1`, which containers can't reach. Make it listen on all interfaces:
  ```bash
  sudo systemctl edit ollama        # add:  [Service]  Environment="OLLAMA_HOST=0.0.0.0"
  sudo systemctl restart ollama
  ```
- **No local Ollama?** Run it inside the stack: in step 3 set `OLLAMA_BASE_URL=http://ollama:11434`. In step 5 start with `docker compose --profile ollama up -d --build`, then pull the model with `docker compose exec ollama ollama pull llama3.2`.

### 3. Create your `.env`

From the project root (the folder containing `docker-compose.yml`):

```bash
cp .env.example .env
openssl rand -hex 32        # copy the output
```

Edit `.env` and set the two required values:

```
MCP_AUTH_TOKEN=<the hex string from openssl>
GRAFANA_ADMIN_PASSWORD=<a password you choose>
```

| Setting | Default | Change it when |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama runs elsewhere |
| `OLLAMA_MODEL` | `llama3.2:latest` | You want another tool-capable model |
| `LOG_PAYLOADS` | `true` | Prompts or tool output must not be stored in logs (`false`) |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | High traffic: keep a fraction of traces, e.g. `0.1` |
| `LLM_COST_PER_1K_*_TOKENS` | `0` | You switch to a paid model and want cost tracking |

Compose refuses to start until `MCP_AUTH_TOKEN` and `GRAFANA_ADMIN_PASSWORD` are set.

### 4. Add the stock price data

`stock_data_tool` reads one CSV per company from `data/`. Each file is named after the ticker, in capitals, with these columns:

```
data/AAPL.csv   data/AMZN.csv   data/META.csv   data/MSFT.csv   data/NFLX.csv   data/TSLA.csv

Date,Open,High,Low,Close,Volume
2026-11-13,236.10,238.42,235.07,237.88,51234000
```

There are two ways to get them.

**Real prices (recommended).** This downloads 2 years of daily history using `yfinance`. It overwrites existing files with the same name.

```bash
python3 -m venv /tmp/yf && /tmp/yf/bin/pip install -q yfinance pandas
/tmp/yf/bin/python - <<'EOF'
import yfinance as yf
for t in ["AAPL", "AMZN", "META", "MSFT", "NFLX", "TSLA"]:
    df = yf.download(t, period="2y", auto_adjust=True, multi_level_index=False, progress=False)
    df.index.name = "Date"
    df[["Open", "High", "Low", "Close", "Volume"]].round(2).to_csv(f"data/{t}.csv")
    print(t, len(df), "rows, last date", df.index[-1].date())
EOF
```

**Demo data.** If you were given `stock-demo-data.zip` (synthetic random-walk prices for testing only, not real market data), unzip it without overwriting your own files:

```bash
unzip -n stock-demo-data.zip '*.csv' -d data/
```

`data/` is linked live into the MCP server container (read-only), so you can add or replace CSVs at any time without a rebuild or restart. The tool can only answer for tickers that have a file.

### 5. Build and start

```bash
docker compose up -d --build
```

The first run downloads about 2 GB of images and builds the app image, which takes a few minutes. Then check:

```bash
docker compose ps
```

Expected state:

| Service | State |
|---|---|
| `mcp-server`, `agent` | running **(healthy)** |
| `ui`, `otel-collector`, `prometheus`, `alertmanager`, `tempo`, `loki`, `grafana` | running |
| `tempo-init` | **exited (0)**. This is normal: it runs once to set storage permissions. |

### 6. Check the agent is ready

```bash
curl localhost:8080/healthz      # {"status":"ok"}
curl localhost:8080/readyz       # {"status":"ready"} once tools are loaded from the MCP server (10–20 s)
```

You can also check from inside the MCP server that it sees your CSVs:

```bash
docker compose exec mcp-server ls /app/data
```

### 7. Use Stock desk

Open **http://localhost:8088**. Ask a question or pick a suggestion. The first answer can take up to a minute while Ollama loads the model into memory.

Each answer shows:
- the price chart (when price data was used)
- the answer
- the **run tape**, a timeline of model turns and tool calls (click a segment for its details)
- run facts: time, tokens, calls and failures
- the **Distributed trace** tab, with an **Open in Grafana** link

The **System health** panel on the right refreshes every 15 seconds. On narrow screens it opens from the icon at the top right.

To call the API directly instead:

```bash
curl -s localhost:8080/v1/query -H 'content-type: application/json' \
  -d '{"query":"Give me a short summary of Apple Inc. and its stock performance over the last 30 days."}' \
  | python3 -m json.tool
```

### 8. Open Grafana

Go to **http://localhost:3000** and log in with:
- **Username:** `admin`
- **Password:** the `GRAFANA_ADMIN_PASSWORD` from your `.env`

Grafana stores the password the first time it starts; changing `.env` later does not change it. To reset it:

```bash
docker compose exec grafana grafana cli admin reset-admin-password 'NewPassword'
```

The home page is the **AI Agent & MCP — Overview** dashboard (also under Dashboards → *AI Agents*). Panels fill about 30 seconds after the first questions. To follow one request, paste its `trace_id` into **Explore → Tempo → TraceQL**. From any span, use **Logs for this span** to see the matching log lines from both services.

### 9. Check the alerts

1. **Rules load:** http://localhost:9090/alerts lists 17 alerts, all green (*Inactive*).
2. **Rule unit tests:** `make test-rules` should print `SUCCESS`.
3. **A real alert:** stop the MCP server and watch `McpServerDown` go from *Pending* (about 3 min) to *Firing* (about 4 min) at http://localhost:9090/alerts. It also appears at http://localhost:9093 and in Stock desk's alerts list.
   ```bash
   docker compose stop mcp-server
   # …wait ~4 minutes, check the pages above…
   docker compose start mcp-server
   ```
4. **Delivery:** alerts reach no one until you configure a receiver (see [Alerts](#alerts) → *Alert delivery*). To send a test alert:
   ```bash
   docker compose exec alertmanager amtool alert add alertname=ManualTest severity=critical \
     --annotation=summary="Test alert" --alertmanager.url=http://localhost:9093
   ```

### 10. Everyday commands

| Task | Command |
|---|---|
| Follow agent + MCP logs | `docker compose logs -f agent mcp-server` |
| Restart after editing `.env` | `docker compose up -d` |
| Apply code changes (`src/`) | `docker compose up -d --build` |
| Apply UI changes (`ui/`) | Just reload the browser; the files are mounted live |
| Stop everything (keep data) | `docker compose down` |
| Stop and delete all metrics, traces, logs and Grafana state | `docker compose down -v` |

### 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `set MCP_AUTH_TOKEN in .env` when starting | `.env` is missing or a required value is empty; see step 3 |
| `pull access denied for stock-agent` | You're using an old `docker-compose.yml`. Both `mcp-server` and `agent` need `build: .` and `pull_policy: build`. As a stopgap: `docker compose build mcp-server && docker compose up -d`. |
| `/readyz` stays at `initialising` | The agent can't load tools from the MCP server: `docker compose logs mcp-server --tail 50` |
| Answer fails with "language model isn't reachable", or HTTP 502 `ConnectError` | Ollama isn't reachable from Docker; redo step 2 |
| Tool step fails: `Data for company XYZ is not available` | `data/XYZ.csv` is missing or misnamed (it must be the ticker in capitals); see step 4 |
| Answer quotes prices that don't match the chart | The model invented figures after a tool error. Check the failed step in the run tape and fix its cause. |
| `wikipedia_tool` always fails with `ConnectionError` | The MCP server container has no internet access (VPN, proxy or firewall) |
| Grafana rejects the password | It was set on first start; reset it as in step 8 |
| Dashboards or System health show "No data" | Ask a few questions and wait about 30 s; rate-based panels need two data points |
| `McpServerDown` takes ~13 min to fire | Old collector config: set `metric_expiration: 1m` in `deploy/otel-collector/config.yaml` and run `docker compose restart otel-collector` |
| Port already in use | Change the left-hand port in `docker-compose.yml`, e.g. `"8089:8080"` for the UI |
| Stock desk opens but Grafana links are wrong | Grafana isn't at `localhost:3000` for your browser; edit `ui/config.js` |

For anything else, collect `docker compose ps` and `docker compose logs agent mcp-server --tail 100`.

### Running without Docker (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export MCP_AUTH_TOKEN=dev-token DATA_DIR=./data OTEL_ENABLED=false LOG_FORMAT=console

stock-agent-mcp          # terminal 1: MCP server on :8000
stock-agent-api          # terminal 2 (same exports): agent API on :8080
stock-agent-cli          # or: run the original sample query once and print the messages
pytest -q                # 19 tests, no Ollama needed
```

To send telemetry from these local processes to the Docker stack, leave out `OTEL_ENABLED=false` and set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` instead.

### Service URLs

| What | URL |
|---|---|
| **Stock desk (web app)** | **http://localhost:8088** |
| Agent API (OpenAPI docs) | http://localhost:8080/docs |
| Grafana | http://localhost:3000 (`admin` / `GRAFANA_ADMIN_PASSWORD`) |
| Prometheus | http://localhost:9090 |
| Alertmanager | http://localhost:9093 |

## Stock desk web app

A plain HTML/CSS/JS front end (`ui/`), with no build step and no framework, served by nginx (`deploy/ui/nginx.conf`) on **http://localhost:8088**.

![Answer with price chart, run tape and steps](docs/stock-desk-run.png)

- **Live runs:** answers stream over Server-Sent Events. Each model turn and tool call appears as it happens.
- **Run tape:** a proportional timeline of the run, with model work in blue, tool calls in green and failures in red. Click a segment to open that step's arguments, result, token counts and timing.
- **Price chart:** drawn from the actual `stock_data_tool` result, with a hover readout. Charts saved by the Python tool are shown inline.
- **Distributed trace:** the full agent→MCP trace from Tempo, drawn as a waterfall inside the answer. It links to Grafana and can copy the trace ID.
- **System health panel:** questions per minute, failure rate, p95 latency, tokens per minute, a token sparkline, per-tool failure rates and latency, service heartbeats and firing alerts, all read from Prometheus.
- **Conversations:** follow-up questions send the earlier turns as context. History is kept in the browser's `localStorage`; the server stays stateless.

![Distributed trace inside the answer](docs/stock-desk-trace.png)

nginx serves the static files and is a same-origin reverse proxy, so the browser never needs CORS:

| Path | Goes to | Exposed |
|---|---|---|
| `/api/*` | agent `:8080` | everything (buffering off for streaming) |
| `/prom/api/v1/{query,query_range,alerts}` | Prometheus | GET only; all other `/prom/*` paths return 404 |
| `/tempo/api/traces/<32-hex id>` | Tempo | GET only |
| `/charts/<name>.png` | shared `charts` volume | read-only |

If you open Grafana from somewhere other than `localhost:3000`, edit `ui/config.js`.

## API

`POST /v1/query` `{"query": "...", "session_id": "optional", "history": [{"role": "user|assistant", "content": "..."}]}` returns `{answer, trace_id, duration_ms, usage{input_tokens, output_tokens, total_tokens, llm_calls, tool_calls, tool_errors, cost_usd}, steps[...]}`. Each step is an LLM turn or tool call with timing, tokens or arguments, and the result.

`POST /v1/query/stream` takes the same body and returns Server-Sent Events: `start` (with `trace_id`), then `step_start` / `step_end` for each step, then `final` or `error`. `GET /v1/info` returns readiness, the model and the tool list.
Errors: `503` if the MCP tools are not loaded yet, `504` on timeout, `422` if the agent exceeded its step budget (`AGENT_RECURSION_LIMIT`), `502` on an LLM or other failure. `GET /healthz` is the liveness probe; `GET /readyz` is the readiness probe and only returns 200 once MCP tools are discovered.

## Alerts

| Alert | Fires when | Severity |
|---|---|---|
| McpServerDown / AgentDown | No heartbeat (`process.uptime`), fires about 4 min after the service stops | critical |
| McpToolHighFailureRate / …Critical | System failure ratio for a tool > 5% / 25% (min. 5 calls) | warning / critical |
| McpToolInputErrorSpike | > 30% of calls rejected as bad LLM input (prompt or model regression) | warning |
| McpToolLatencyHigh | Tool p95 > 10s for 10m | warning |
| McpClientTransportErrors | Agent can't reach MCP (connect, timeout or HTTP errors) | critical |
| TokenUsageSpike | 5m token rate > 3× the trailing hour and > 50 tok/s | warning |
| DailyTokenBudgetExceeded | > 5M tokens in 24h (tune to your budget) | warning |
| LlmErrorRateHigh / LlmLatencyHigh | LLM errors > 5% / p95 > 30s | critical / warning |
| AgentErrorRateHigh / AgentLatencyHigh | Agent errors > 5% / p95 > 60s | critical / warning |
| AgentRunawayToolLoop | p95 tool calls per request > 8, or repeated recursion-limit hits | warning |
| AgentTokensPerRequestHigh | p95 tokens per request > 20k | warning |
| OtelCollectorDown / OtelCollectorExportFailing | Telemetry pipeline is broken | critical / warning |

**Alert delivery:** put your Slack webhook in `deploy/alertmanager/secrets/slack_webhook_url` and uncomment the `slack_configs` blocks in `deploy/alertmanager/alertmanager.yml`. PagerDuty is set up the same way. Thresholds are starting points: tune them after a week of baseline traffic.

### Runbook: McpServerDown
1. `docker compose ps mcp-server` / `docker compose logs mcp-server --tail 100`.
2. If the container is healthy but the alert still fires, the collector is not receiving its telemetry. Check `OtelCollectorExportFailing`.
3. While the server is down, agent requests still succeed, but every tool returns "currently unavailable". The `McpClientTransportErrors` alert is inhibited during this time.

### Runbook: McpToolHighFailureRate
1. On the dashboard, open **Tool failures / s by error type** to see which `error_type` is failing.
2. Open **Recent failed / slow traces**, pick a trace, and open the server span's exception event for the stack trace.
3. `ConnectionError` or `HTTPError` on `wikipedia_tool` means outbound internet access or Wikipedia itself is the problem. `TimeoutError` or `RuntimeError` on `python_repl_tool` means the model-generated code is failing.

## Configuration

All settings are environment variables; see `src/stock_agent/config.py` and `.env.example`. The standard `OTEL_*` variables are also honoured (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLER(_ARG)`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SDK_DISABLED`).

## Changes from the original script

- **Tools now run behind an MCP server** (`stock_agent.mcp_server`). The agent discovers them through `langchain-mcp-adapters`, and telemetry is attached centrally (`ObservedFastMCP`, client interceptors), so new tools are instrumented automatically.
- **`python_repl_tool` runs in a child process** with a timeout, a memory cap and a minimal environment, inside a non-root, read-only container. It no longer runs in-process in the agent. `plt.show()` cannot display anything on a server, so the tool description tells the model to `plt.savefig()` into the charts volume.
- **`stock_data_tool` validates the ticker.** The original built a file path from LLM output, which allowed path traversal such as `../../etc/passwd`.
- Tool failures are raised as MCP errors instead of being returned as strings, so failure rates can be measured. The LLM still sees the error text and can recover.
- The agent is async and has a step budget (`AGENT_RECURSION_LIMIT`), a request timeout and graceful degradation when MCP is unavailable.
- The MCP endpoint requires a bearer token (`MCP_AUTH_TOKEN`) and has DNS-rebinding protection (`MCP_ALLOWED_HOSTS`).

## Tests

```bash
make test        # 19 tests: agent ↔ real MCP server over HTTP, asserting one trace across services,
                 # metric attributes, token accounting, auth, sandboxing, graceful degradation
make check-configs   # promtool rule checks and alert unit tests, amtool, collector validate, compose config
```

## Production notes

- **Kubernetes:** run the same image with `stock-agent-api` and `stock-agent-mcp` as two Deployments, using `/healthz` and `/readyz` as liveness and readiness probes. The MCP server is stateless (`stateless_http`), so it scales horizontally behind a Service. Ship telemetry to an OTel Collector DaemonSet or gateway, and keep `deploy/otel-collector/config.yaml` as the base config.
- **Storage:** Tempo and Loki use local disk here. Point them at S3, GCS or Azure for durability, or swap both for Grafana Cloud or another OTLP backend by changing only the collector exporters.
- **Sampling:** at high volume, set `OTEL_TRACES_SAMPLER_ARG=0.1`. Metrics and logs are unaffected, so alerts stay accurate.
- **PII:** prompts and tool output are logged by default for debuggability. Set `LOG_PAYLOADS=false` wherever retention rules forbid storing them.
- **Python tool:** executing model-written code is inherently risky. For multi-tenant use, move it to a dedicated sandbox (gVisor, Firecracker, or a separate network-less pod).