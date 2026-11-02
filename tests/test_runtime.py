import json

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_lab import runtime
from agent_lab.agents import REGISTRY
from agent_lab.agents import demo
from agent_lab.cli import main
from agent_lab.config import Settings
from agent_lab.mock_provider import mock_response
from agent_lab.runtime import run_agent, save_record

OFFLINE = Settings.from_env({})


@pytest.fixture
def fake_clock(monkeypatch):
    """Replace sleeping with a recorded, instantaneous clock."""
    state = {"now": 0.0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(runtime, "_sleep", sleep)
    monkeypatch.setattr(runtime, "_monotonic", lambda: state["now"])
    return state


def use_model(monkeypatch, fn):
    """Route every model build (any model name) to a scripted FunctionModel."""
    built = []

    def build(settings, model_name=None):
        built.append(model_name)
        return FunctionModel(fn, model_name=model_name or "scripted")

    monkeypatch.setattr(runtime, "build_model", build)
    return built


def scripted(*steps):
    """Each call pops the next step: an exception to raise, or a function producing a response."""
    queue = list(steps)

    def fn(messages, info):
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, BaseException):
            raise step
        return step(messages, info)

    return fn


def http_error(status, retry_after=None):
    return ModelHTTPError(status_code=status, model_name="scripted", retry_after=retry_after)


def test_mock_run_calls_real_tools_and_returns_structured_output():
    record = run_agent("What is (12+30)*2 and what is k3s?", OFFLINE)
    assert record.status == "succeeded"
    assert [c.tool_name for c in record.tool_calls] == ["calculate", "lookup_note"]
    assert record.tools_used == ["calculate", "lookup_note"]
    assert "84" in record.output["answer"]
    assert record.requests == 2
    assert record.served_model == "mock-tool-caller"
    assert len(record.config_hash) == 64
    assert [a.error_class for a in record.attempts] == [None]


def test_run_without_tools():
    record = run_agent("hello", OFFLINE)
    assert record.status == "succeeded"
    assert record.tool_calls == [] and record.tools_used == []
    assert record.output["confidence"] == "low"


def test_output_tool_excluded_from_tool_calls_by_definition_not_name():
    record = run_agent("2+2", OFFLINE)
    assert {c.tool_name for c in record.tool_calls} <= REGISTRY["demo"].tool_names


def test_loop_is_bounded(monkeypatch):
    """A model that never stops calling tools must be cut off by UsageLimits."""
    use_model(monkeypatch, lambda m, i: ModelResponse(parts=[ToolCallPart("calculate", {"expression": "1+1"})]))
    record = run_agent("loop forever", Settings.from_env({"AGENT_LAB_MAX_REQUESTS": "3"}))
    assert record.status == "failed"
    assert record.error_class == "limit"
    assert "UsageLimitExceeded" in record.error
    assert record.tool_calls and record.tools_used == ["calculate"]


# --- A1: every failure becomes a classified RunRecord ---------------------------------


def test_tool_crash_is_recorded_as_tool_error(monkeypatch):
    def broken(expression: str) -> str:
        raise OSError("disk gone")

    monkeypatch.setattr(demo, "TOOLS", (broken,))
    monkeypatch.setitem(REGISTRY, "demo", REGISTRY["demo"].__class__(
        build=demo.build_demo_agent, tool_names=frozenset({"broken"}), instructions=demo.INSTRUCTIONS))
    use_model(monkeypatch, lambda m, i: ModelResponse(parts=[ToolCallPart("broken", {"expression": "1"})]))
    record = run_agent("x", OFFLINE)
    assert record.status == "failed" and record.error_class == "tool"
    assert "OSError" in record.error
    assert len(record.attempts) == 1  # tool errors are never retried


def test_transport_error_is_transient_and_recorded(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(httpx.ConnectError("no route")))
    record = run_agent("x", OFFLINE)
    assert record.status == "failed" and record.error_class == "transient"
    assert len(record.attempts) == OFFLINE.max_attempts


def test_missing_gemini_key_is_config_error():
    record = run_agent("x", Settings.from_env({"AGENT_LAB_PROVIDER": "gemini"}))
    assert record.status == "failed" and record.error_class == "config"
    assert len(record.attempts) == 1


def test_auth_error_is_config_and_not_retried(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(403)))
    record = run_agent("x", OFFLINE)
    assert record.error_class == "config" and len(record.attempts) == 1
    assert fake_clock["sleeps"] == []


def test_schema_failure_is_validation_error(monkeypatch):
    use_model(monkeypatch, lambda m, i: ModelResponse(parts=[ToolCallPart(i.output_tools[0].name, {"bad": 1})]))
    record = run_agent("x", OFFLINE)
    assert record.status == "failed" and record.error_class == "validation"


def test_unexpected_internal_error_is_recorded(monkeypatch):
    use_model(monkeypatch, scripted(KeyError("boom")))
    record = run_agent("x", OFFLINE)
    assert record.status == "failed" and record.error_class == "internal"


# --- A2: bounded retry -----------------------------------------------------------------


def test_429_retried_then_succeeds_honoring_retry_after(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(429, retry_after=7), http_error(429), mock_response))
    record = run_agent("hello", OFFLINE)
    assert record.status == "succeeded"
    assert [a.error_class for a in record.attempts] == ["transient", "transient", None]
    assert fake_clock["sleeps"][0] == 7  # Retry-After honored
    assert len(fake_clock["sleeps"]) == 2


def test_no_retry_after_a_tool_has_run(monkeypatch, fake_clock):
    def tool_then_503(messages, info):
        if any(getattr(p, "tool_name", None) == "calculate" for m in messages for p in getattr(m, "parts", [])
               if p.__class__.__name__ == "ToolReturnPart"):
            raise http_error(503)
        return ModelResponse(parts=[ToolCallPart("calculate", {"expression": "1+1"})])

    use_model(monkeypatch, tool_then_503)
    record = run_agent("x", OFFLINE)
    assert record.error_class == "transient"
    assert len(record.attempts) == 1 and fake_clock["sleeps"] == []


def test_retry_after_longer_than_deadline_stops_without_early_retry(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(429, retry_after=30)))
    record = run_agent("x", Settings.from_env({"AGENT_LAB_DEADLINE_SECONDS": "10", "AGENT_LAB_MAX_ATTEMPTS": "5"}))
    assert record.status == "failed" and record.error_class == "transient"
    assert "exceeds remaining deadline" in record.error
    assert fake_clock["sleeps"] == []  # never retried before the provider's requested time
    assert len(record.attempts) == 1


def test_retry_after_above_backoff_cap_is_honored_in_full(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(429, retry_after=60), mock_response))
    record = run_agent("hello", Settings.from_env({"AGENT_LAB_DEADLINE_SECONDS": "120"}))
    assert record.status == "succeeded"
    assert fake_clock["sleeps"] == [60]


def test_deadline_stops_retries(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(503)))
    # Our own backoff (2s, 4s, ...) fits, but the clock runs out before the next attempt.
    fake_clock["now"] = 0.0
    record = run_agent("x", Settings.from_env({"AGENT_LAB_DEADLINE_SECONDS": "7", "AGENT_LAB_MAX_ATTEMPTS": "5"}))
    assert record.status == "failed"
    assert sum(fake_clock["sleeps"]) <= 7


def test_fallback_model_used_after_transient_exhaustion(monkeypatch, fake_clock):
    def build(settings, model_name=None):
        if model_name == "primary":
            return FunctionModel(scripted(http_error(503)), model_name="primary")
        return FunctionModel(mock_response, model_name="fallback")

    monkeypatch.setattr(runtime, "build_model", build)
    monkeypatch.setattr(runtime, "model_names", lambda s: ["primary", "fallback"])
    record = run_agent("hello", OFFLINE)
    assert record.status == "succeeded" and record.served_model == "fallback"
    assert [a.model for a in record.attempts] == ["primary"] * OFFLINE.max_attempts + ["fallback"]


def test_fallback_not_used_for_validation_failures(monkeypatch):
    built = use_model(monkeypatch, lambda m, i: ModelResponse(parts=[ToolCallPart(i.output_tools[0].name, {"bad": 1})]))
    monkeypatch.setattr(runtime, "model_names", lambda s: ["primary", "fallback"])
    record = run_agent("x", OFFLINE)
    assert record.error_class == "validation" and built == ["primary"]


def test_fallback_used_when_model_retired(monkeypatch, fake_clock):
    def build(settings, model_name=None):
        if model_name == "old":
            return FunctionModel(scripted(http_error(404)), model_name="old")
        return FunctionModel(mock_response, model_name="new")

    monkeypatch.setattr(runtime, "build_model", build)
    monkeypatch.setattr(runtime, "model_names", lambda s: ["old", "new"])
    record = run_agent("hello", OFFLINE)
    assert record.status == "succeeded" and record.served_model == "new"


# --- records and CLI -------------------------------------------------------------------


def test_prompt_validation():
    with pytest.raises(ValueError):
        run_agent("   ", OFFLINE)
    with pytest.raises(KeyError):
        run_agent("hi", OFFLINE, agent_name="nope")


def test_config_hash_changes_with_settings_but_not_secrets():
    a = run_agent("hi", OFFLINE).config_hash
    assert a == run_agent("hi", Settings.from_env({"GEMINI_API_KEY": "secret"})).config_hash
    assert a != run_agent("hi", Settings.from_env({"AGENT_LAB_MAX_REQUESTS": "5"})).config_hash


def test_git_sha_recorded():
    assert run_agent("hi", Settings.from_env({"GITHUB_SHA": "abc123"})).git_sha == "abc123"


def test_save_record_appends_jsonl(tmp_path):
    record = run_agent("1+1", OFFLINE)
    path = save_record(record, tmp_path)
    save_record(record, tmp_path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["run_id"] == record.run_id


def test_cli_run_and_healthcheck(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LAB_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_LAB_PROVIDER", raising=False)
    assert main(["healthcheck"]) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "mock"
    assert main(["run", "2*21"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert (tmp_path / "runs.jsonl").exists()


def test_cli_exit_codes(tmp_path, monkeypatch, fake_clock):
    monkeypatch.setenv("AGENT_LAB_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_LAB_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["run", "hi"]) == 2  # config; still recorded
    assert json.loads((tmp_path / "runs.jsonl").read_text())["error_class"] == "config"
    monkeypatch.setenv("AGENT_LAB_PROVIDER", "mock")
    use_model(monkeypatch, scripted(httpx.ReadTimeout("slow")))
    assert main(["run", "hi"]) == 3  # transient
    use_model(monkeypatch, scripted(KeyError("x")))
    assert main(["run", "hi"]) == 1


def test_registry_contains_expected_agents():
    assert sorted(REGISTRY) == ["calm_down", "demo"]


# --- review fixes: fallback after tools, and run-wide usage limits ---------------------


def test_no_fallback_after_a_tool_has_run_even_on_404(monkeypatch):
    built = []

    def tool_then_404(messages, info):
        if any(p.__class__.__name__ == "ToolReturnPart" for m in messages for p in getattr(m, "parts", [])):
            raise http_error(404)
        return ModelResponse(parts=[ToolCallPart("calculate", {"expression": "1+1"})])

    def build(settings, model_name=None):
        built.append(model_name)
        return FunctionModel(tool_then_404 if model_name == "primary" else mock_response, model_name=model_name)

    monkeypatch.setattr(runtime, "build_model", build)
    monkeypatch.setattr(runtime, "model_names", lambda s: ["primary", "fallback"])
    record = run_agent("x", OFFLINE)
    assert record.status == "failed" and record.error_class == "config"
    assert built == ["primary"]
    assert record.tools_used == ["calculate"]


def test_request_limit_is_shared_across_retries(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(429)))
    settings = Settings.from_env({"AGENT_LAB_MAX_REQUESTS": "2", "AGENT_LAB_MAX_ATTEMPTS": "5"})
    record = run_agent("x", settings)
    assert record.status == "failed" and record.error_class == "limit"
    assert record.requests == 2  # failed calls count against the cap
    assert len([a for a in record.attempts if a.error_class == "transient"]) == 2


def test_request_limit_is_shared_across_fallback(monkeypatch, fake_clock):
    built = []

    def build(settings, model_name=None):
        built.append(model_name)
        return FunctionModel(scripted(http_error(503)) if model_name == "primary" else mock_response,
                             model_name=model_name)

    monkeypatch.setattr(runtime, "build_model", build)
    monkeypatch.setattr(runtime, "model_names", lambda s: ["primary", "fallback"])
    # 3 failed primary requests leave 1 of 4: the fallback's 2-request answer cannot fit.
    record = run_agent("What is 2+2?", Settings.from_env({"AGENT_LAB_MAX_REQUESTS": "4"}))
    assert record.status == "failed" and record.error_class == "limit"
    assert record.requests <= 4


def test_usage_totals_include_failed_attempts(monkeypatch, fake_clock):
    use_model(monkeypatch, scripted(http_error(429), mock_response))
    record = run_agent("What is 2+2?", OFFLINE)
    assert record.status == "succeeded"
    assert record.requests == 3  # 1 failed + 2 successful
    assert record.input_tokens > 0


def test_config_error_does_not_consume_requests():
    record = run_agent("x", Settings.from_env({"AGENT_LAB_PROVIDER": "gemini"}))
    assert record.error_class == "config" and record.requests == 0
