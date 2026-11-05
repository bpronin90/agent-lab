"""Internal, gated execution core for the Claude CLI (`claude -p`). Not a provider.

Only the Calm Down research pipeline uses it, when `AGENT_LAB_PROVIDER=claude-cli` is set
explicitly (#21); the general model factory has no Claude implementation, mock stays the
default, and Gemini is unchanged. Every launch needs `AGENT_LAB_ALLOW_CLAUDE_CLI=1` and is refused when
`CI` or `KUBERNETES_SERVICE_HOST` is present. That catches accidental use in marked
environments. It cannot detect cron, prove a human is present, or verify an owner
authorization record. Unattended use stays prohibited by policy.

Runtime guarantees, all held by one `ClaudeCliContext`:
- One request allowance and one monotonic deadline shared by every call.
- No retry, fallback, repair, or automatic login. A failure is final.
- stdout/stderr drained concurrently with byte caps; timeout or overflow kills and
  reaps the process group, and the temporary working directory is always removed.
- Reported output tokens are checked against a per-response allowance *after* the
  response. This is not a hard service-side token cap.
- Records hold fixed diagnostic codes only, never prompts, stdout, stderr, or
  exception text. The subprocess controls here are not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from agent_lab.schemas import AttemptRecord, ErrorClass, RunRecord

PROVIDER = "claude-cli"
ALLOW_ENV = "AGENT_LAB_ALLOW_CLAUDE_CLI"
REFUSE_IF_PRESENT = ("CI", "KUBERNETES_SERVICE_HOST")

BASE_ARGS = (
    "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
    "--disable-slash-commands", "--setting-sources", "",
)

# Variables passed to the subprocess: enough to execute and find the existing login.
# Everything else, including ANTHROPIC_*/GEMINI_* keys and model overrides, is dropped.
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "CLAUDE_CONFIG_DIR",
)

CLEANUP_SECONDS = 5.0

# Lower-cased fragments of CLI output used only to classify; the text itself is never kept.
_UNSUPPORTED_FLAG = ("unknown option", "unknown argument", "unrecognized option")
_NO_LOGIN = ("not logged in", "please run /login", "invalid api key", "authentication_error", "oauth token")
_TRANSIENT = ("rate limit", "rate_limit", "usage limit", "overloaded", "529", "503", "502", "temporarily")

_monotonic = time.monotonic
T = TypeVar("T", bound=BaseModel)


class ClaudeCliError(Exception):
    """A classified failure. `code` is a fixed identifier that is safe to record."""

    def __init__(self, error_class: ErrorClass, code: str):
        super().__init__(code)
        self.error_class = error_class
        self.code = code


@dataclass
class CallResult:
    """One launched call. Token counts are None when the CLI did not report them."""

    model: str
    served_model: str | None = None
    input_tokens: int | None = None  # sum of the reported input components; lower bound unless input_complete
    output_tokens: int | None = None
    input_complete: bool = False  # True only when all three input components were reported
    error_class: ErrorClass | None = None
    code: str | None = None

    @property
    def usage_complete(self) -> bool:
        return self.input_complete and self.output_tokens is not None


def check_launch_gate(env: Mapping[str, str]) -> None:
    for name in REFUSE_IF_PRESENT:
        if name in env:
            raise ClaudeCliError("config", f"gate_refused_{name.lower()}")
    if env.get(ALLOW_ENV) != "1":
        raise ClaudeCliError("config", "gate_not_enabled")


def build_argv(binary: str, model: str) -> list[str]:
    return [binary, *BASE_ARGS, "--model", model]


def build_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: env[k] for k in ENV_ALLOWLIST if k in env}


@dataclass
class ClaudeCliContext:
    """Shared bounds and accounting for every call in one run."""

    model: str
    deadline: float  # absolute time.monotonic() value
    max_requests: int
    max_output_tokens: int  # per response, checked after the fact
    max_stdout_bytes: int = 1_000_000
    max_stderr_bytes: int = 64_000
    binary: str = "claude"
    env: Mapping[str, str] | None = None  # defaults to os.environ at launch
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: list[CallResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.model:
            raise ClaudeCliError("config", "model_required")

    @property
    def usage_complete(self) -> bool:
        """False when any launched call lacked reported usage; totals are then lower bounds."""
        return all(c.usage_complete for c in self.calls)

    @property
    def served_model(self) -> str | None:
        return next((c.served_model for c in reversed(self.calls) if c.served_model), None)

    def remaining_seconds(self) -> float:
        return self.deadline - _monotonic()

    def call(self, prompt: str, schema: type[T], max_output_tokens: int | None = None) -> T:
        """Launch exactly one `claude -p` call and validate its JSON result against `schema`.
        `max_output_tokens` narrows the per-response allowance for this call; it never widens it."""
        env = os.environ if self.env is None else self.env
        check_launch_gate(env)
        if self.requests >= self.max_requests:
            raise ClaudeCliError("limit", "request_allowance_exhausted")
        remaining = self.deadline - _monotonic()
        if remaining <= 0:
            raise ClaudeCliError("limit", "deadline_expired")

        allowance = self.max_output_tokens if max_output_tokens is None else min(max_output_tokens, self.max_output_tokens)
        result = CallResult(model=self.model)
        try:
            stdout, stderr, returncode = self._launch(prompt, env, remaining, result)
            return self._interpret(stdout, stderr, returncode, schema, result, allowance)
        except ClaudeCliError as exc:
            result.error_class, result.code = exc.error_class, exc.code
            raise
        except Exception:
            result.error_class, result.code = "internal", "unexpected_exception"
            raise

    def _launch(self, prompt, env, remaining, result):
        workdir = tempfile.mkdtemp(prefix="agent-lab-claude-")
        try:
            try:
                proc = subprocess.Popen(
                    build_argv(self.binary, self.model),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=workdir, env=build_env(env), shell=False, start_new_session=True,
                )
            except FileNotFoundError:
                raise ClaudeCliError("config", "binary_not_found") from None
            # Spawned: this call now counts, whatever happens next.
            self.requests += 1
            self.calls.append(result)
            return _communicate(proc, prompt.encode(), remaining, self.max_stdout_bytes, self.max_stderr_bytes)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _interpret(self, stdout, stderr, returncode, schema, result, allowance):
        envelope = _parse_envelope(stdout)
        if envelope is not None:
            self._account(envelope, result)
        text = stderr.decode(errors="replace").lower()
        if envelope is None or returncode != 0 or envelope.get("is_error") or envelope.get("subtype") != "success":
            detail = text + " " + str((envelope or {}).get("result", "")).lower() + " " + str((envelope or {}).get("subtype", "")).lower()
            if any(s in detail for s in _UNSUPPORTED_FLAG):
                raise ClaudeCliError("config", "unsupported_flag")
            if any(s in detail for s in _NO_LOGIN):
                raise ClaudeCliError("config", "not_logged_in")
            if any(s in detail for s in _TRANSIENT):
                raise ClaudeCliError("transient", "service_unavailable")
            if envelope is None and returncode == 0:
                raise ClaudeCliError("validation", "malformed_envelope")
            if returncode != 0:
                raise ClaudeCliError("internal", "unrecognized_exit")
            raise ClaudeCliError("internal", "error_envelope")
        if result.output_tokens is not None and result.output_tokens > allowance:
            raise ClaudeCliError("limit", "output_tokens_exceeded")
        generated = envelope.get("result")
        if not isinstance(generated, str):
            raise ClaudeCliError("validation", "malformed_envelope")
        try:
            data = json.loads(_strip_fence(generated))
        except ValueError:
            raise ClaudeCliError("validation", "malformed_generated_json") from None
        try:
            return schema.model_validate(data)
        except ValidationError:
            raise ClaudeCliError("validation", "schema_mismatch") from None

    def _account(self, envelope: dict, result: CallResult) -> None:
        """Accumulate top-level `usage` only (never `modelUsage` or nested cache breakdowns,
        which overlap it). Input tokens are the sum of uncached `input_tokens`,
        `cache_creation_input_tokens` and `cache_read_input_tokens`, each counted once per call.
        A component that is missing or not a nonnegative integer is unavailable, never assumed
        zero: known components still count, so the totals are then lower bounds and the call is
        marked incomplete (explicit zero is available usage)."""
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            parts = [_int_or_none(usage.get(k)) for k in _INPUT_COMPONENTS]
            known = [p for p in parts if p is not None]
            result.input_tokens = sum(known) if known else None
            result.input_complete = len(known) == len(parts)
            result.output_tokens = _int_or_none(usage.get("output_tokens"))
        models = envelope.get("modelUsage")
        if isinstance(models, dict) and models:
            result.served_model = str(next(iter(models)))[:200]
        if result.input_tokens is not None:
            self.input_tokens += result.input_tokens
        if result.output_tokens is not None:
            self.output_tokens += result.output_tokens


def _communicate(proc, stdin: bytes, timeout: float, max_out: int, max_err: int):
    """Feed stdin and drain both pipes concurrently under byte caps and one timeout."""
    overflow = threading.Event()
    buffers = {"out": bytearray(), "err": bytearray()}

    def drain(stream, key, cap):
        try:
            while chunk := stream.read1(65536):
                buf = buffers[key]
                if len(buf) + len(chunk) > cap:
                    buf.extend(chunk[: cap - len(buf)])
                    overflow.set()
                    return
                buf.extend(chunk)
        except (OSError, ValueError):
            pass

    def feed():
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [
        threading.Thread(target=feed, daemon=True),
        threading.Thread(target=drain, args=(proc.stdout, "out", max_out), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "err", max_err), daemon=True),
    ]
    for t in threads:
        t.start()
    end = _monotonic() + timeout
    try:
        while proc.poll() is None:
            if overflow.is_set():
                raise ClaudeCliError("limit", "output_bytes_exceeded")
            if _monotonic() >= end:
                raise ClaudeCliError("limit", "deadline_exceeded")
            overflow.wait(0.02)
        # The parent exited, but a descendant may still hold the pipes open.
        for t in threads[1:]:
            t.join(max(0.0, end - _monotonic()))
        if overflow.is_set():
            raise ClaudeCliError("limit", "output_bytes_exceeded")
        if any(t.is_alive() for t in threads[1:]):
            raise ClaudeCliError("limit", "deadline_exceeded")
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        # Reap any descendants left in the group, then give readers a bounded chance to see EOF.
        _kill_group(proc)
        for t in threads:
            t.join(CLEANUP_SECONDS / len(threads))
        # Closing a stream a live thread still holds would block on its lock; leave those to the
        # daemon threads rather than break the deadline.
        for t, stream in zip(threads, (proc.stdin, proc.stdout, proc.stderr)):
            if not t.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
    return bytes(buffers["out"]), bytes(buffers["err"]), proc.returncode


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _parse_envelope(stdout: bytes) -> dict | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # A CLI result envelope always reports both fields; anything else is malformed.
    if not isinstance(data.get("is_error"), bool) or not isinstance(data.get("subtype"), str):
        return None
    return data


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


_INPUT_COMPONENTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def config_hash(ctx: ClaudeCliContext, instructions: str) -> str:
    """Secret-free hash of model, bounds, launch policy and supplied instructions."""
    payload = {
        "provider": PROVIDER,
        "model": ctx.model,
        "max_requests": ctx.max_requests,
        "max_output_tokens": ctx.max_output_tokens,
        "max_stdout_bytes": ctx.max_stdout_bytes,
        "max_stderr_bytes": ctx.max_stderr_bytes,
        "argv": list(BASE_ARGS),
        "env_allowlist": list(ENV_ALLOWLIST),
        "gate": [ALLOW_ENV, *REFUSE_IF_PRESENT],
        "instructions": instructions,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_with_record(
    ctx: ClaudeCliContext,
    work: Callable[[ClaudeCliContext], BaseModel],
    *,
    agent_name: str,
    prompt_descriptor: str,
    instructions: str,
    git_sha: str = "unknown",
) -> RunRecord:
    """Run `work` against `ctx` and return a RunRecord on success or any caught failure.

    `prompt_descriptor` is stored as `RunRecord.prompt` and must be a safe summary, not
    the raw prompt. Usage the CLI did not report is noted in `error`/attempts, never
    silently recorded as zero consumption. Host termination or storage failure can
    still lose the record.
    """
    started = datetime.now(timezone.utc)
    output: dict | None = None
    error_class: ErrorClass | None = None
    code: str | None = None
    try:
        output = work(ctx).model_dump(mode="json")
    except ClaudeCliError as exc:
        error_class, code = exc.error_class, exc.code
    except Exception:
        error_class, code = "internal", "unexpected_exception"
    notes = [code] if code else []
    if not ctx.usage_complete:
        notes.append("usage_unavailable_for_some_calls")
    return RunRecord(
        run_id=str(uuid.uuid4()),
        agent_name=agent_name,
        provider=PROVIDER,
        model=ctx.model,
        prompt=prompt_descriptor[:500],
        status="failed" if error_class else "succeeded",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        output=output,
        error="; ".join(notes) or None,
        error_class=error_class,
        served_model=ctx.served_model,
        git_sha=git_sha,
        config_hash=config_hash(ctx, instructions),
        attempts=[
            AttemptRecord(
                model=c.model,
                error_class=c.error_class,
                error=c.code if c.usage_complete
                else "; ".join(filter(None, [c.code, "usage_unavailable"])),
            )
            for c in ctx.calls
        ],
        requests=ctx.requests,
        input_tokens=ctx.input_tokens,
        output_tokens=ctx.output_tokens,
    )
