"""Tool implementations (business logic only — no telemetry code here).

Instrumentation is applied centrally by ObservedFastMCP, so adding a tool
never requires remembering to add logging/metrics/tracing.

Error contract:
  * ToolInputError  -> caller supplied bad input (counted as error.type=tool_input_error)
  * any other error -> tool/system failure     (counted as error.type=<ExceptionClass>)
Both are returned to the agent as MCP `isError` results so the LLM can react.
"""

from __future__ import annotations

import asyncio
import os
import re
import resource
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import pandas as pd
import wikipedia
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from stock_agent.config import get_settings

_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class ToolInputError(ToolError):
    """Invalid arguments from the caller (not a server fault)."""


# --------------------------------------------------------------------------- #
# Wikipedia
# --------------------------------------------------------------------------- #
def _wikipedia_lookup(query: str) -> str:
    wikipedia.set_user_agent("Multi Agent System for Stock Data Retrieval")
    results = wikipedia.search(query)
    if not results:
        return "No results found on Wikipedia."
    summary = wikipedia.summary(results[0], sentences=8, auto_suggest=False, redirect=True)
    return f"Wikipedia summary: {summary}"


async def wikipedia_tool(
    query: Annotated[str, Field(description="The Wikipedia search to execute to find key summary information.")],
) -> str:
    """Use this to search Wikipedia for factual summary information about a company or topic."""
    if not query.strip():
        raise ToolInputError("query must not be empty")
    return await asyncio.to_thread(_wikipedia_lookup, query)


# --------------------------------------------------------------------------- #
# Stock data
# --------------------------------------------------------------------------- #
def _stock_lookup(data_dir: Path, ticker: str, num_days: int) -> str:
    file_path = data_dir / f"{ticker}.csv"
    if not file_path.is_file():
        raise ToolInputError(
            f"Data for company {ticker} is not available. Please try Apple, Amazon, Meta, Microsoft, Netflix, or Tesla."
        )
    stock_df = pd.read_csv(file_path, index_col="Date", parse_dates=True)
    stock_df.index = stock_df.index.date
    max_num_days = (stock_df.index.max() - stock_df.index.min()).days
    if num_days > max_num_days:
        raise ToolInputError(
            f"This time period exceeds the data available ({max_num_days} days). Please reduce it to continue."
        )
    final_date = stock_df.index.max()
    filtered_df = stock_df[stock_df.index > (final_date - pd.Timedelta(days=num_days))]
    return (
        f"Stock performance data for the last *{num_days} days* for company **{ticker}**:\n\n"
        f"{filtered_df.to_markdown()}"
    )


async def stock_data_tool(
    company_ticker: Annotated[
        str, Field(description="The ticker symbol of the company to retrieve their stock performance data.")
    ],
    num_days: Annotated[
        int, Field(description="The number of business days of stock data required to respond to the user query.")
    ],
) -> str:
    """Use this to look-up stock performance data for companies to retrieve a table from a CSV. You may need to
    convert company names into ticker symbols to call this function, e.g, Apple Inc. -> AAPL, and you may need
    to convert weeks, months, and years, into days."""
    ticker = company_ticker.strip().upper()
    if not _TICKER.match(ticker):  # also blocks path traversal via the file name
        raise ToolInputError(f"'{company_ticker}' is not a valid ticker symbol")
    if num_days < 1:
        raise ToolInputError("num_days must be a positive integer")
    return await asyncio.to_thread(_stock_lookup, get_settings().data_dir, ticker, num_days)


# --------------------------------------------------------------------------- #
# Python execution (sandboxed in a child process)
# --------------------------------------------------------------------------- #
def _limit_resources() -> None:  # pragma: no cover - runs in the child
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))  # 1 GiB address space
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))  # 50 MiB per written file


def _run_python(code: str, timeout_s: float, max_chars: int, charts_dir: Path) -> str:
    charts_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(charts_dir / ".mpl"),
        "HOME": str(charts_dir),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    try:
        proc = subprocess.run(  # noqa: S603 - executing model-written code is the purpose of this tool
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=charts_dir,
            env=env,
            preexec_fn=_limit_resources if sys.platform == "linux" else None,  # noqa: PLW1509
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Python execution exceeded {timeout_s:.0f}s") from exc
    out = (proc.stdout or "")[-max_chars:]
    if proc.returncode != 0:
        err = (proc.stderr or "")[-max_chars:]
        raise RuntimeError(f"Python exited with code {proc.returncode}:\n{err}")
    return f"Code output:\n```\n{out}```"


async def python_repl_tool(
    code: Annotated[str, Field(description="The python code to execute to generate your chart.")],
) -> str:
    """Use this to execute python code. If you want to see the output of a value, you should print it out with
    `print(...)`. To produce a chart, save it with `plt.savefig('<name>.png')` (the working directory is the
    charts folder) and print the file name."""
    s = get_settings()
    return await asyncio.to_thread(
        _run_python, code, s.python_exec_timeout_s, s.python_exec_max_output_chars, s.charts_dir.resolve()
    )


ALL_TOOLS = [wikipedia_tool, stock_data_tool, python_repl_tool]
