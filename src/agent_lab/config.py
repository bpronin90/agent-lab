"""Runtime settings, read from environment variables only.

Nothing here has a secret default. The mock provider is the default so that
every command works offline with no credentials.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# "claude-cli" is accepted only by the Calm Down research pipeline (#21); every other
# entry point refuses it in the model factory before any subprocess or network activity.
PROVIDERS = ("mock", "gemini", "claude-cli")

# Values accepted by `claude --effort <level>` per the installed CLI's help (`claude --help`):
# low, medium, high, xhigh, max. Unset omits the argument.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


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
    # Minimum gap between model requests (Gemini free tier allows ~5 per minute). 0 = no pacing.
    min_request_interval_seconds: float = 0
    # Claude CLI (Calm Down only). Settings.from_env never defaults a model; the `calm-down`
    # research command supplies its own command-level defaults (pipeline.RESEARCH_COMMAND_DEFAULTS).
    # Output allocations are per phase and checked after each response, not service-enforced.
    claude_model: str | None = None
    claude_plan_output_tokens: int = 1000
    claude_synthesis_output_tokens: int = 20000
    claude_effort: str | None = None
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
            min_request_interval_seconds=_non_negative_float(
                env, "AGENT_LAB_MIN_REQUEST_INTERVAL_SECONDS", cls.min_request_interval_seconds),
            claude_model=(env.get("AGENT_LAB_CLAUDE_MODEL") or "").strip() or None,
            claude_plan_output_tokens=_positive_int(
                env, "AGENT_LAB_CLAUDE_PLAN_OUTPUT_TOKENS", cls.claude_plan_output_tokens),
            claude_synthesis_output_tokens=_positive_int(
                env, "AGENT_LAB_CLAUDE_SYNTHESIS_OUTPUT_TOKENS", cls.claude_synthesis_output_tokens),
            claude_effort=_effort(env),
            git_sha=env.get("AGENT_LAB_GIT_SHA") or env.get("GITHUB_SHA") or cls.git_sha,
            run_log_dir=Path(env.get("AGENT_LAB_RUN_LOG_DIR", str(cls.run_log_dir))),
        )


    def public_dict(self) -> dict[str, object]:
        """Settings that shape a run, without secrets. Used for the run's config_hash.
        Claude CLI settings are excluded here; that path hashes its own configuration."""
        data = {k: v for k, v in self.__dict__.items()
                if k not in ("gemini_api_key", "git_sha", "run_log_dir") and not k.startswith("claude_")}
        return {k: str(v) if isinstance(v, Path) else v for k, v in data.items()}


def _effort(env: dict[str, str]) -> str | None:
    raw = (env.get("AGENT_LAB_CLAUDE_EFFORT") or "").strip().lower()
    if not raw:
        return None
    if raw not in CLAUDE_EFFORTS:
        raise ValueError(f"AGENT_LAB_CLAUDE_EFFORT must be one of {CLAUDE_EFFORTS}")
    return raw


def checkout_root() -> Path | None:
    """The source checkout root, or None when running from an installed package.
    Resolved from this file's location (never the working directory or a parent search) and
    accepted only if it looks like this repository's checkout."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "agent_lab" / "config.py").is_file():
        return root
    return None


def live_env_path() -> Path | None:
    root = checkout_root()
    return None if root is None else root / ".env.live"


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_dotenv(text: str) -> dict[str, str]:
    """Tiny non-executing dotenv grammar: blank lines, `#` comment lines, and `KEY=VALUE` where
    VALUE is literal text, optionally wrapped in one matching pair of quotes. No `export`,
    interpolation, escapes, command substitution or sourcing. Errors name a line, never a value."""
    out: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise ValueError(f".env.live line {number}: expected KEY=VALUE")
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f".env.live line {number}: unbalanced quote")
            value = value[1:-1]
        if key in out:
            raise ValueError(f".env.live line {number}: duplicate assignment of {key}")
        out[key] = value
    return out


def load_env_file(path: Path | None) -> dict[str, str]:
    """Parse `path`; a missing file (or no path) is harmless and yields {}."""
    if path is None:
        return {}
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"{path.name} could not be read") from None
    return parse_dotenv(text)


def _positive_int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{key} must be >= 1, got {value}")
    return value


def _non_negative_float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    value = float(raw)
    if not 0 <= value <= 120:
        raise ValueError(f"{key} must be between 0 and 120, got {value}")
    return value
