"""Runtime settings, read from environment variables only.

Nothing here has a secret default. The mock provider is the default so that
every command works offline with no credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("mock", "gemini")


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key: str | None = None
    # Used only after the primary model fails with a transient or model-not-found error.
    gemini_fallback_model: str | None = None
    # Bounded reasoning loop: hard caps on model requests and tool calls per run.
    max_requests: int = 6
    max_tool_calls: int = 8
    max_output_tokens: int = 2048
    # Transient failures (429, 5xx, transport) are retried up to max_attempts per model,
    # within an overall wall-clock deadline.
    max_attempts: int = 3
    deadline_seconds: int = 120
    git_sha: str = "unknown"
    run_log_dir: Path = Path("runs")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        provider = env.get("AGENT_LAB_PROVIDER", "mock").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"AGENT_LAB_PROVIDER must be one of {PROVIDERS}, got {provider!r}")
        return cls(
            provider=provider,
            gemini_model=env.get("AGENT_LAB_GEMINI_MODEL", cls.gemini_model),
            gemini_api_key=env.get("GEMINI_API_KEY") or None,
            gemini_fallback_model=env.get("AGENT_LAB_GEMINI_FALLBACK_MODEL") or None,
            max_requests=_positive_int(env, "AGENT_LAB_MAX_REQUESTS", cls.max_requests),
            max_tool_calls=_positive_int(env, "AGENT_LAB_MAX_TOOL_CALLS", cls.max_tool_calls),
            max_output_tokens=_positive_int(env, "AGENT_LAB_MAX_OUTPUT_TOKENS", cls.max_output_tokens),
            max_attempts=_positive_int(env, "AGENT_LAB_MAX_ATTEMPTS", cls.max_attempts),
            deadline_seconds=_positive_int(env, "AGENT_LAB_DEADLINE_SECONDS", cls.deadline_seconds),
            git_sha=env.get("AGENT_LAB_GIT_SHA") or env.get("GITHUB_SHA") or cls.git_sha,
            run_log_dir=Path(env.get("AGENT_LAB_RUN_LOG_DIR", str(cls.run_log_dir))),
        )


    def public_dict(self) -> dict[str, object]:
        """Settings that shape a run, without secrets. Used for the run's config_hash."""
        data = {k: v for k, v in self.__dict__.items() if k not in ("gemini_api_key", "git_sha", "run_log_dir")}
        return {k: str(v) if isinstance(v, Path) else v for k, v in data.items()}


def _positive_int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{key} must be >= 1, got {value}")
    return value
