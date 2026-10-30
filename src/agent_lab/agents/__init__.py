"""Agent registry. Add new agents here (Phase 2+)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model

from agent_lab.agents import demo


@dataclass(frozen=True)
class AgentSpec:
    """Per-agent model settings live in code so they are reviewed, tested and versioned."""

    build: Callable[[Model], Agent]
    tool_names: frozenset[str]
    instructions: str
    temperature: float = 0.2
    thinking_budget: int = 512


REGISTRY: dict[str, AgentSpec] = {
    demo.AGENT_NAME: AgentSpec(
        build=demo.build_demo_agent,
        tool_names=frozenset(t.__name__ for t in demo.TOOLS),
        instructions=demo.INSTRUCTIONS,
    ),
}
