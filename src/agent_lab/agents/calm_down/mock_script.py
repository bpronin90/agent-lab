"""Deterministic offline 'researcher' for the fixture preview and tests.

Turn 1 searches both sides of the fear. Turn 2 reads every result. Turn 3 returns the
hand-written fixture dossier. It exercises the real tools, runtime and verifier, but it is
not research: its content is a fixture.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

QUERIES = (
    "AI exposure forecast lawyers accountants tasks automation",
    "AI employment decline early-career displacement evidence",
    "AI labor market earnings hours evidence accountants",
    "lawyers sanctions chatgpt hallucination court liability",
    "history automation jobs atm bank tellers complements",
    "regulation ethics lawyers competence accountability",
    "accountants auditors employment projection outlook",
    "knowledge workers consultants productivity experiment frontier",
)


def load_mock_dossier() -> dict:
    text = resources.files("agent_lab.agents.calm_down.fixtures").joinpath("mock_dossier.json").read_text("utf-8")
    return json.loads(text)


def _returns(messages: list[ModelMessage], name: str) -> list[ToolReturnPart]:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart) and p.tool_name == name]


def calm_down_mock_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    searches, reads = _returns(messages, "search_sources"), _returns(messages, "read_source")
    if not searches:
        return ModelResponse(parts=[ToolCallPart("search_sources", {"query": q}) for q in QUERIES])
    if not reads:
        ids = sorted({sid for r in searches for sid in re.findall(r'"source_id": "([^"]+)"', str(r.content))},
                     key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)
        return ModelResponse(parts=[ToolCallPart("read_source", {"source_id": sid}) for sid in ids])
    output = load_mock_dossier()
    if info.output_tools:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])
    return ModelResponse(parts=[TextPart(json.dumps(output))])
