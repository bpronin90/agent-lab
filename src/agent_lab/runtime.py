"""Run an agent with hard bounds and record what happened, including every failure."""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic_ai import UsageLimits, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.usage import RunUsage

from agent_lab.agents import REGISTRY, AgentSpec
from agent_lab.config import Settings
from agent_lab.models import ProviderConfigError, build_model, build_model_settings, model_names
from agent_lab.schemas import AttemptRecord, ErrorClass, RunRecord, ToolCallRecord
from agent_lab.tooling import ToolExecutionError, track_tools

MAX_PROMPT_LENGTH = 4000
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0

# Indirection so tests can use a fake clock.
_sleep = time.sleep
_monotonic = time.monotonic


def classify(exc: BaseException) -> ErrorClass:
    if isinstance(exc, ProviderConfigError):
        return "config"
    if isinstance(exc, ToolExecutionError):
        return "tool"
    if isinstance(exc, UsageLimitExceeded):
        return "limit"
    if isinstance(exc, ModelHTTPError):
        if exc.status_code in (408, 429) or exc.status_code >= 500:
            return "transient"
        return "config"  # 400/401/403/404: bad request, bad key, or retired model
    if isinstance(exc, UnexpectedModelBehavior):
        return "validation"
    if isinstance(exc, (ModelAPIError, httpx.TransportError, TimeoutError, ConnectionError)):
        return "transient"
    return "internal"


def _is_model_not_found(exc: BaseException) -> bool:
    return isinstance(exc, ModelHTTPError) and exc.status_code == 404


def _backoff(exc: BaseException, attempt: int) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        # The provider's requested wait is honored in full, never shortened.
        return float(retry_after)
    # Only our own exponential backoff is capped.
    return min(BACKOFF_BASE_SECONDS * 2**attempt + random.uniform(0, 1), BACKOFF_MAX_SECONDS)


def config_hash(settings: Settings, agent_name: str, spec: AgentSpec) -> str:
    payload = {
        "settings": settings.public_dict(),
        "agent": agent_name,
        "instructions": spec.instructions,
        "temperature": spec.temperature,
        "thinking_budget": spec.thinking_budget,
        "tools": sorted(spec.tool_names),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _tool_calls(messages: list[ModelMessage], tool_names: frozenset[str]) -> list[ToolCallRecord]:
    # Only the agent's own function tools; output tools are excluded whatever they are named.
    return [
        ToolCallRecord(tool_name=part.tool_name, args=part.args)
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name in tool_names
    ]


def _served_model(messages: list[ModelMessage]) -> str | None:
    names = [m.model_name for m in messages if isinstance(m, ModelResponse) and m.model_name]
    return names[-1] if names else None


def run_agent(prompt: str, settings: Settings, agent_name: str = "demo") -> RunRecord:
    """Run once. Never raises for run failures: every outcome is returned as a RunRecord."""
    if agent_name not in REGISTRY:
        raise KeyError(f"Unknown agent {agent_name!r}. Known: {sorted(REGISTRY)}")
    if not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError(f"prompt must be 1..{MAX_PROMPT_LENGTH} characters")

    spec = REGISTRY[agent_name]
    names = model_names(settings)
    base = dict(
        run_id=str(uuid.uuid4()),
        agent_name=agent_name,
        provider=settings.provider,
        model=names[0],
        prompt=prompt,
        started_at=datetime.now(timezone.utc),
        git_sha=settings.git_sha,
        config_hash=config_hash(settings, agent_name, spec),
    )
    limits = UsageLimits(request_limit=settings.max_requests, tool_calls_limit=settings.max_tool_calls)
    # One usage counter for the whole run: request, tool-call and token totals are enforced and
    # recorded across retries and fallback, not reset per attempt.
    usage = RunUsage()
    deadline = _monotonic() + settings.deadline_seconds
    attempts: list[AttemptRecord] = []
    messages: list[ModelMessage] = []
    executed: list[str] = []
    last_exc: BaseException | None = None
    tool_ran = False

    for model_name in names:
        for attempt in range(settings.max_attempts):
            if _monotonic() >= deadline:
                attempts.append(AttemptRecord(model=model_name, error_class="limit", error="deadline exceeded"))
                return _failed(base, attempts, messages, executed, spec, "limit", "deadline exceeded", usage)
            if usage.requests >= settings.max_requests:
                return _failed(base, attempts, messages, executed, spec, "limit", "request limit exhausted across attempts", usage)
            requests_before = usage.requests
            try:
                with track_tools() as executed, capture_run_messages() as messages:
                    agent = spec.build(build_model(settings, model_name))
                    result = agent.run_sync(
                        prompt,
                        usage_limits=limits,
                        usage=usage,
                        model_settings=build_model_settings(settings, spec.temperature, spec.thinking_budget),
                    )
            except Exception as exc:  # noqa: BLE001 - every failure must become a record
                last_exc = exc
                error_class = classify(exc)
                tool_ran = tool_ran or bool(executed)
                # A provider call that failed before reporting usage still spent API quota; count it
                # (conservatively, for transport errors too). Local config errors sent nothing.
                if usage.requests == requests_before and (isinstance(exc, ModelHTTPError) or error_class == "transient"):
                    usage.requests += 1
                attempts.append(AttemptRecord(model=model_name, error_class=error_class, error=_describe(exc)))
                # Retry only transient failures, and only if no tool has run yet.
                if error_class == "transient" and not tool_ran and attempt + 1 < settings.max_attempts:
                    wait = _backoff(exc, attempt)
                    if wait > deadline - _monotonic():
                        # Retrying sooner than asked would just be rejected again; stop and let the
                        # caller retry later (exit code 3).
                        error = f"retry wait {wait:g}s exceeds remaining deadline; last error: {_describe(exc)}"
                        return _failed(base, attempts, messages, executed, spec, "transient", error, usage)
                    _sleep(wait)
                    continue
                break
            attempts.append(AttemptRecord(model=model_name))
            all_messages = result.all_messages()
            return RunRecord(
                **base,
                status="succeeded",
                finished_at=datetime.now(timezone.utc),
                output=result.output.model_dump(),
                served_model=_served_model(all_messages),
                attempts=attempts,
                tools_used=sorted(set(executed)),
                tool_calls=_tool_calls(all_messages, spec.tool_names),
                **_usage_fields(usage),
            )
        # Fall back only when the primary model was unavailable and no tool has run yet:
        # the same no-repeat guarantee as retries.
        assert last_exc is not None
        fallback_ok = not tool_ran and (classify(last_exc) == "transient" or _is_model_not_found(last_exc))
        if not fallback_ok:
            break

    assert last_exc is not None
    return _failed(base, attempts, messages, executed, spec, classify(last_exc), _describe(last_exc), usage)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _usage_fields(usage: RunUsage) -> dict[str, int]:
    return dict(requests=usage.requests, input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0)


def _failed(base, attempts, messages, executed, spec, error_class, error, usage) -> RunRecord:
    return RunRecord(
        **base,
        status="failed",
        finished_at=datetime.now(timezone.utc),
        error=error,
        error_class=error_class,
        served_model=_served_model(messages),
        attempts=attempts,
        tools_used=sorted(set(executed)),
        tool_calls=_tool_calls(messages, spec.tool_names),
        **_usage_fields(usage),
    )


def save_record(record: RunRecord, directory: Path) -> Path:
    """Append the record to a local JSONL file (Supabase persistence is P2.4)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "runs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.model_dump(mode="json")) + "\n")
    return path
