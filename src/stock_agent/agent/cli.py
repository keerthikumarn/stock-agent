"""Command-line entry point (equivalent of the original `__main__` block)."""

from __future__ import annotations

import argparse
import asyncio

from stock_agent.agent.service import AgentService
from stock_agent.config import get_settings
from stock_agent.observability import init_telemetry

DEFAULT_QUERY = "Give me a short summary of Apple Inc. and its stock performance over the last 30 days."


async def _run(query: str) -> None:
    settings = get_settings()
    telemetry = init_telemetry("stock-agent-cli", settings)
    try:
        result = await AgentService(settings).run(query)
        for message in result.messages:
            message.pretty_print()
        print(f"\ntrace_id={result.trace_id} usage={result.usage} duration_ms={result.duration_ms}")
    finally:
        telemetry.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one query through the stock research agent")
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    asyncio.run(_run(parser.parse_args().query))


if __name__ == "__main__":
    main()
