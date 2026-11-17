"""MCP server exposing the stock tools over Streamable HTTP."""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from stock_agent.config import Settings, get_settings
from stock_agent.mcp_server.instrumentation import McpHttpMiddleware, ObservedFastMCP
from stock_agent.mcp_server.tools import ALL_TOOLS
from stock_agent.observability import init_telemetry

SERVICE_NAME = "mcp-tools"
log = logging.getLogger("stock_agent.mcp.server")


def build_mcp(settings: Settings) -> ObservedFastMCP:
    mcp = ObservedFastMCP(
        name="stock-tools",
        instructions="Tools for company research: Wikipedia summaries, historical stock prices, and Python charting.",
        # Stateless + JSON responses: every request is self-contained, so the
        # server scales horizontally behind any load balancer.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=[],
        ),
    )
    for fn in ALL_TOOLS:
        mcp.add_tool(fn)
    return mcp


def build_app(settings: Settings | None = None):
    settings = settings or get_settings()
    mcp = build_mcp(settings)
    starlette_app = mcp.streamable_http_app()

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(_: Request) -> JSONResponse:
        ready = settings.data_dir.is_dir()
        return JSONResponse({"status": "ready" if ready else "data_dir_missing"}, status_code=200 if ready else 503)

    starlette_app.router.routes.extend([Route("/healthz", healthz), Route("/readyz", readyz)])

    token = settings.mcp_auth_token.get_secret_value() if settings.mcp_auth_token else None
    app = McpHttpMiddleware(starlette_app, mcp_path="/mcp", auth_token=token)
    # Outermost: HTTP server spans (extracts W3C traceparent sent by the agent).
    return OpenTelemetryMiddleware(app, excluded_urls="healthz,readyz", exclude_spans=["receive", "send"])


def main() -> None:
    settings = get_settings()
    telemetry = init_telemetry(SERVICE_NAME, settings)
    if settings.mcp_auth_token is None:
        log.warning("MCP_AUTH_TOKEN is not set: MCP endpoint is unauthenticated", extra={"event": "security.config"})
    try:
        uvicorn.run(
            build_app(settings),
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_config=None,  # use our structured logging
            proxy_headers=True,
            timeout_graceful_shutdown=20,
        )
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
