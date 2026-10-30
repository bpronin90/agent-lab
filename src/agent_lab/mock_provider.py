"""Deterministic offline model built on PydanticAI's FunctionModel.

It behaves like a tiny tool-calling LLM: on the first turn it picks tools based
on keywords in the prompt, then it reads the tool results and returns the
structured output through the output tool. No network, no randomness.
"""

from __future__ import annotations

import re

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

MOCK_MODEL_NAME = "mock-tool-caller"

_ARITHMETIC = re.compile(r"[-+*/().\d\s]*\d[-+*/().\d\s]*[-+*/][-+*/().\d\s]*\d[-+*/().\d\s]*")


def _latest_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]


def _plan_tool_calls(prompt: str, available: set[str]) -> list[ToolCallPart]:
    lowered = prompt.lower()
    calls: list[ToolCallPart] = []
    match = _ARITHMETIC.search(prompt)
    if match and "calculate" in available:
        calls.append(ToolCallPart("calculate", {"expression": match.group(0).strip()}))
    if "current_time" in available and any(word in lowered for word in ("time", "date", "today", "now")):
        calls.append(ToolCallPart("current_time", {"tz_name": "UTC"}))
    if "lookup_note" in available:
        for topic in ("docker", "kubernetes", "k3s", "pydanticai"):
            if topic in lowered:
                calls.append(ToolCallPart("lookup_note", {"topic": topic}))
                break
    return calls


def mock_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = _tool_returns(messages)
    if not returns:
        calls = _plan_tool_calls(_latest_prompt(messages), {t.name for t in info.function_tools})
        if calls:
            return ModelResponse(parts=calls)

    summary = "; ".join(f"{r.tool_name}: {r.content}" for r in returns) or "No tool was needed."
    output = {"answer": summary, "confidence": "high" if returns else "low"}
    if info.output_tools:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])
    return ModelResponse(parts=[TextPart(summary)])


def build_mock_model() -> FunctionModel:
    return FunctionModel(mock_response, model_name=MOCK_MODEL_NAME)
