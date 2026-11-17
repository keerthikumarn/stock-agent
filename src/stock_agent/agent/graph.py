"""LangGraph definition: llm <-> tools loop (same topology as the original app)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from stock_agent.agent.llm import InstrumentedLLM


class State(TypedDict):
    messages: Annotated[list, add_messages]


def build_graph(llm: InstrumentedLLM, tools: Sequence[BaseTool]):
    async def llm_node(state: State):
        return {"messages": [await llm.ainvoke(state["messages"])]}

    builder = StateGraph(State)
    builder.add_node("llm", llm_node)
    builder.add_node("tools", ToolNode(tools=list(tools), handle_tool_errors=True))
    builder.add_edge(START, "llm")
    builder.add_conditional_edges("llm", tools_condition)  # -> "tools" if tool_calls, else END
    builder.add_edge("tools", "llm")
    return builder.compile()
