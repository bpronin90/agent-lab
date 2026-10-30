"""Demonstration agent with three safe, side-effect-free tools."""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from agent_lab.schemas import AgentAnswer
from agent_lab.tooling import safe_tool

AGENT_NAME = "demo"

INSTRUCTIONS = (
    "You are a careful lab assistant. Use the provided tools for arithmetic, the current time, "
    "and lab notes instead of guessing. Call each tool at most once per fact you need. "
    "Return a concise answer."
)

NOTES = {
    "docker": "An image is a read-only template; a container is a running instance of an image.",
    "kubernetes": "A Deployment keeps N replicas of a Pod running and replaces failed ones.",
    "k3s": "K3s is a lightweight, single-binary Kubernetes distribution suited to one small node.",
    "pydanticai": "PydanticAI validates model output against a Pydantic schema and retries on mismatch.",
}

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
MAX_EXPRESSION_LENGTH = 200


def safe_eval(expression: str) -> float:
    """Evaluate + - * / and parentheses only. No names, calls, or exponentiation."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("expression too long")

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError(f"unsupported syntax: {type(node).__name__}")

    return walk(ast.parse(expression, mode="eval"))


def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression using + - * / and parentheses."""
    try:
        result = safe_eval(expression)
    except (ValueError, SyntaxError, ZeroDivisionError) as exc:
        raise ModelRetry(f"Cannot evaluate {expression!r}: {exc}") from exc
    return f"{expression} = {result:g}"


def current_time(tz_name: str = "UTC") -> str:
    """Return the current date and time in an IANA timezone such as 'UTC' or 'America/New_York'."""
    try:
        tz = timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ModelRetry(f"Unknown timezone {tz_name!r}") from exc
    return datetime.now(tz).isoformat(timespec="seconds")


def lookup_note(topic: str) -> str:
    """Look up a short lab note. Known topics: docker, kubernetes, k3s, pydanticai."""
    note = NOTES.get(topic.strip().lower())
    if note is None:
        raise ModelRetry(f"No note for {topic!r}. Known topics: {', '.join(sorted(NOTES))}")
    return note


TOOLS = (calculate, current_time, lookup_note)


def build_demo_agent(model: Model) -> Agent[None, AgentAnswer]:
    agent = Agent(model, output_type=AgentAnswer, instructions=INSTRUCTIONS, name=AGENT_NAME, retries=2)
    for tool in TOOLS:
        agent.tool_plain(safe_tool(tool))
    return agent

