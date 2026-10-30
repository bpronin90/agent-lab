"""Structured output and run-record schemas shared by all agents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentAnswer(BaseModel):
    """Structured output every demo-style agent must return."""

    answer: str = Field(description="Concise final answer for the user.")
    confidence: Literal["low", "medium", "high"] = Field(description="Self-assessed confidence.")


class ToolCallRecord(BaseModel):
    tool_name: str
    args: dict[str, Any] | str | None = None


ErrorClass = Literal["config", "transient", "limit", "validation", "tool", "internal"]


class AttemptRecord(BaseModel):
    model: str
    error_class: ErrorClass | None = None
    error: str | None = None


class RunRecord(BaseModel):
    """One agent run. Mirrors the proposed agent_lab.runs table."""

    run_id: str
    agent_name: str
    provider: str
    model: str
    prompt: str
    status: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    output: dict[str, Any] | None = None
    error: str | None = None
    error_class: ErrorClass | None = None
    served_model: str | None = Field(default=None, description="Model name reported by the provider response.")
    git_sha: str = "unknown"
    config_hash: str = ""
    attempts: list[AttemptRecord] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list, description="Derived from executed tool calls, not model claims.")
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
