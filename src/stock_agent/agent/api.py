"""HTTP API for the agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, Field

from stock_agent.agent.service import AgentError, AgentNotReady, AgentService
from stock_agent.config import Settings, get_settings
from stock_agent.observability import init_telemetry

SERVICE_NAME = "stock-agent"
log = logging.getLogger("stock_agent.api")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=8000)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    # Earlier turns of the conversation, oldest first (the server keeps no chat state).
    history: list[Turn] = Field(default_factory=list, max_length=20)

    def history_tuples(self) -> list[tuple[str, str]]:
        return [(t.role, t.content) for t in self.history]


class QueryResponse(BaseModel):
    answer: str
    trace_id: str
    duration_ms: float
    usage: dict[str, float | int]
    steps: list[dict[str, Any]]


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['event']}\ndata: {json.dumps(event, default=str)}\n\n"


def create_app(settings: Settings | None = None, service: AgentService | None = None) -> FastAPI:
    settings = settings or get_settings()
    telemetry = init_telemetry(SERVICE_NAME, settings)
    agent = service or AgentService(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # Don't block start-up on the MCP server; /readyz reports when tools are loaded.
        init_task = asyncio.create_task(agent.initialise_with_retry())
        try:
            yield
        finally:
            init_task.cancel()
            telemetry.shutdown()

    app = FastAPI(title="Stock Research Agent", version=settings.service_version, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response):
        if not agent.ready:
            response.status_code = 503
            return {"status": "initialising"}
        return {"status": "ready"}

    @app.get("/v1/info")
    async def info():
        return {"ready": agent.ready, "model": settings.ollama_model, "agent": "stock-research-agent",
                "tools": agent.tools, "version": settings.service_version}

    @app.post("/v1/query", response_model=QueryResponse)
    async def query(body: QueryRequest, response: Response):
        try:
            result = await agent.run(body.query, body.session_id, body.history_tuples())
        except AgentNotReady as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AgentError as exc:
            status = {"timeout": 504, "recursion_limit": 422}.get(exc.error_type, 502)
            raise HTTPException(status_code=status, detail={"error": exc.error_type, "message": str(exc)}) from exc
        response.headers["X-Trace-Id"] = result.trace_id
        return QueryResponse(answer=result.answer, trace_id=result.trace_id,
                             duration_ms=result.duration_ms, usage=result.usage, steps=result.steps)

    @app.post("/v1/query/stream")
    async def query_stream(body: QueryRequest, request: Request) -> StreamingResponse:
        """Server-Sent Events: start -> step_start/step_end ... -> final | error."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def runner() -> None:
            try:
                result = await agent.run(body.query, body.session_id, body.history_tuples(), on_event=queue.put_nowait)
                queue.put_nowait({"event": "final", "answer": result.answer, "trace_id": result.trace_id,
                                  "duration_ms": result.duration_ms, "usage": result.usage, "steps": result.steps})
            except AgentNotReady as exc:
                queue.put_nowait({"event": "error", "status": 503, "error": "not_ready", "message": str(exc)})
            except AgentError as exc:
                status = {"timeout": 504, "recursion_limit": 422}.get(exc.error_type, 502)
                queue.put_nowait({"event": "error", "status": status, "error": exc.error_type, "message": str(exc)})
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(runner())

        async def events() -> AsyncIterator[str]:
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        return
                    yield _sse(event)
                    if await request.is_disconnected():
                        return
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz", exclude_spans=["receive", "send"])
    return app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.agent_host,
        port=settings.agent_port,
        log_config=None,
        proxy_headers=True,
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
