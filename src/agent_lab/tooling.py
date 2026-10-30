"""Tool wrapper that makes tool execution observable and its failures classifiable."""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from pydantic_ai import ModelRetry

# Names of tools that actually executed during the current run.
_executed: ContextVar[list[str] | None] = ContextVar("agent_lab_executed_tools", default=None)


class ToolExecutionError(RuntimeError):
    """A tool failed with an unexpected exception (not a ModelRetry)."""

    def __init__(self, tool_name: str, cause: BaseException):
        super().__init__(f"tool {tool_name!r} failed: {type(cause).__name__}: {cause}")
        self.tool_name = tool_name


def safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        executed = _executed.get()
        if executed is not None:
            executed.append(fn.__name__)
        try:
            return fn(*args, **kwargs)
        except ModelRetry:
            raise
        except Exception as exc:
            raise ToolExecutionError(fn.__name__, exc) from exc

    return wrapper


class track_tools:
    """Context manager collecting the names of tools executed inside it."""

    def __enter__(self) -> list[str]:
        self._names: list[str] = []
        self._token = _executed.set(self._names)
        return self._names

    def __exit__(self, *exc: object) -> None:
        _executed.reset(self._token)
