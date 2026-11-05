"""Offline tests for the gated Claude CLI core. A generated fake `claude` stands in for the CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_lab import claude_cli
from agent_lab.claude_cli import ClaudeCliContext, ClaudeCliError, run_with_record

SECRET = "sk-canary-DO-NOT-LEAK-1234"
GATE_ENV = {"AGENT_LAB_ALLOW_CLAUDE_CLI": "1", "PATH": os.environ.get("PATH", ""), "HOME": "/tmp/fake-home"}


class Answer(BaseModel):
    answer: str


FULL_USAGE = {"input_tokens": 11, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 7}


def envelope(result='{"answer": "ok"}', *, is_error=False, subtype="success", usage=True, model="claude-sonnet-5-5", usage_data=None):
    data = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result}
    if usage:
        data["usage"] = usage_data if usage_data is not None else FULL_USAGE
    if model:
        data["modelUsage"] = {model: {"outputTokens": 7}}
    return json.dumps(data)


@pytest.fixture
def fake(tmp_path):
    """Return a factory writing a fake `claude` whose body runs after logging what it received."""

    def make(body: str = "", stdout: str | None = None, stderr: str = "", code: int = 0) -> Path:
        log = tmp_path / f"log-{len(list(tmp_path.glob('log-*')))}.jsonl"
        script = tmp_path / f"claude-{log.stem}"
        out = envelope() if stdout is None else stdout
        script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys, time
            with open({str(log)!r}, "a") as f:
                f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                                    "env": dict(os.environ), "cwd": os.getcwd(),
                                    "cwd_entries": os.listdir(".")}}) + "\\n")
            {body}
            sys.stdout.write({out!r}); sys.stderr.write({stderr!r}); sys.exit({code})
            """))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    return make


def ctx(binary, *, env=None, max_requests=3, timeout=10.0, max_output_tokens=100, **kw) -> ClaudeCliContext:
    return ClaudeCliContext(
        model="claude-sonnet-5-5", deadline=time.monotonic() + timeout, max_requests=max_requests,
        max_output_tokens=max_output_tokens, binary=str(binary), env=GATE_ENV if env is None else env, **kw,
    )


def calls(script) -> list[dict]:
    log = Path(script).parent / (Path(script).name.removeprefix("claude-") + ".jsonl")
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- launch boundary ---------------------------------------------------------------

def test_exact_argv_stdin_env_and_cwd(fake):
    script = fake()
    env = {**GATE_ENV, "GEMINI_API_KEY": SECRET, "ANTHROPIC_API_KEY": SECRET, "ANTHROPIC_MODEL": "x",
           "AWS_SECRET_ACCESS_KEY": SECRET, "LANG": "C.UTF-8"}
    c = ctx(script, env=env)
    assert c.call("the prompt", Answer) == Answer(answer="ok")
    [seen] = calls(script)
    assert seen["argv"] == ["-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
                            "--disable-slash-commands", "--setting-sources", "", "--model", "claude-sonnet-5-5"]
    assert seen["stdin"] == "the prompt"
    assert SECRET not in json.dumps(seen["env"])
    assert set(seen["env"]) - {"PWD", "SHLVL", "_", "LC_CTYPE"} <= set(claude_cli.ENV_ALLOWLIST)
    assert "AGENT_LAB_ALLOW_CLAUDE_CLI" not in seen["env"]
    assert seen["cwd_entries"] == [] and "agent-lab-claude-" in seen["cwd"]
    assert not Path(seen["cwd"]).exists()  # temporary cwd removed


@pytest.mark.parametrize("env,code", [
    ({}, "gate_not_enabled"),
    ({"AGENT_LAB_ALLOW_CLAUDE_CLI": "0"}, "gate_not_enabled"),
    ({"AGENT_LAB_ALLOW_CLAUDE_CLI": "true"}, "gate_not_enabled"),
    ({"AGENT_LAB_ALLOW_CLAUDE_CLI": "1", "CI": ""}, "gate_refused_ci"),
    ({"AGENT_LAB_ALLOW_CLAUDE_CLI": "1", "CI": "true"}, "gate_refused_ci"),
    ({"AGENT_LAB_ALLOW_CLAUDE_CLI": "1", "KUBERNETES_SERVICE_HOST": ""}, "gate_refused_kubernetes_service_host"),
    ({"CI": "1", "KUBERNETES_SERVICE_HOST": "10.0.0.1"}, "gate_refused_ci"),
    ({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, "gate_refused_kubernetes_service_host"),
])
def test_gate_rejections_launch_nothing(fake, env, code):
    script = fake()
    c = ctx(script, env=env)
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == ("config", code)
    assert calls(script) == [] and c.requests == 0


def test_gate_checked_before_every_launch(fake):
    script = fake()
    env = dict(GATE_ENV)
    c = ctx(script, env=env)
    c.call("one", Answer)
    env["CI"] = ""
    with pytest.raises(ClaudeCliError, match="gate_refused_ci"):
        c.call("two", Answer)
    assert len(calls(script)) == 1 and c.requests == 1


def test_gate_defaults_to_process_environment(fake, monkeypatch):
    script = fake()
    monkeypatch.delenv("AGENT_LAB_ALLOW_CLAUDE_CLI", raising=False)
    monkeypatch.setenv("CI", "true")
    c = ClaudeCliContext(model="m", deadline=time.monotonic() + 5, max_requests=1, max_output_tokens=10,
                         binary=str(script))
    with pytest.raises(ClaudeCliError, match="gate_refused_ci"):
        c.call("p", Answer)
    assert calls(script) == []


def test_model_is_required():
    with pytest.raises(ClaudeCliError, match="model_required"):
        ClaudeCliContext(model="", deadline=0, max_requests=1, max_output_tokens=1)


def test_missing_binary_is_config_and_uncounted(tmp_path):
    c = ctx(tmp_path / "no-such-claude")
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == ("config", "binary_not_found")
    assert c.requests == 0


# --- shared accounting and deadline --------------------------------------------------

def test_shared_accounting_across_two_calls_then_allowance_refuses(fake):
    script = fake()
    c = ctx(script, max_requests=2)
    c.call("a", Answer)
    c.call("b", Answer)
    assert (c.requests, c.input_tokens, c.output_tokens) == (2, 22, 14)
    assert c.served_model == "claude-sonnet-5-5" and c.usage_complete
    with pytest.raises(ClaudeCliError) as err:
        c.call("c", Answer)
    assert (err.value.error_class, err.value.code) == ("limit", "request_allowance_exhausted")
    assert len(calls(script)) == 2


def test_failed_call_still_counts_and_is_never_retried(fake):
    script = fake(stdout=envelope(is_error=True, subtype="error_during_execution", result="Rate limit reached"),
                  code=1)
    c = ctx(script, max_requests=5)
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == ("transient", "service_unavailable")
    assert len(calls(script)) == 1 and c.requests == 1
    assert (c.input_tokens, c.output_tokens) == (11, 7)  # partial usage preserved


def test_deadline_shared_and_expired_calls_refused(fake):
    script = fake(body="time.sleep(0.4)")
    c = ctx(script, timeout=0.6)
    c.call("first", Answer)
    with pytest.raises(ClaudeCliError) as err:
        c.call("second", Answer)  # gets only the remaining ~0.2 s
    assert (err.value.error_class, err.value.code) == ("limit", "deadline_exceeded")
    with pytest.raises(ClaudeCliError, match="deadline_expired"):
        c.call("third", Answer)
    assert len(calls(script)) == 2 and c.requests == 2


def test_timeout_kills_process_group_and_cleans_up(fake, tmp_path):
    marker = tmp_path / "grandchild-alive"
    child = tmp_path / "grandchild.py"
    child.write_text(f"import time\ntime.sleep(1.5)\nopen({str(marker)!r}, 'w').write('x')\n")
    body = f"import subprocess; subprocess.Popen([sys.executable, {str(child)!r}]); time.sleep(30)"
    script = fake(body=body)
    c = ctx(script, timeout=0.5)
    start = time.monotonic()
    with pytest.raises(ClaudeCliError, match="deadline_exceeded"):
        c.call("p", Answer)
    assert time.monotonic() - start < 5
    [seen] = calls(script)
    assert not Path(seen["cwd"]).exists()
    time.sleep(2)
    assert not marker.exists()  # the whole process group was killed


def test_stdout_overflow_terminates(fake):
    script = fake(body="sys.stdout.write('x' * 50000); sys.stdout.flush(); time.sleep(30)")
    c = ctx(script, max_stdout_bytes=1000)
    start = time.monotonic()
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == ("limit", "output_bytes_exceeded")
    assert time.monotonic() - start < 5 and c.requests == 1


def test_stderr_overflow_terminates(fake):
    script = fake(body="sys.stderr.write('e' * 50000); sys.stderr.flush(); time.sleep(30)")
    with pytest.raises(ClaudeCliError, match="output_bytes_exceeded"):
        ctx(script, max_stderr_bytes=1000).call("p", Answer)


def test_reported_output_tokens_over_allowance_is_limit(fake):
    c = ctx(fake(), max_output_tokens=5)
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == ("limit", "output_tokens_exceeded")
    assert c.output_tokens == 7


def test_unavailable_usage_is_explicit_not_zero(fake):
    c = ctx(fake(stdout=envelope(usage=False, model=None)))
    c.call("p", Answer)
    assert c.calls[0].input_tokens is None and c.calls[0].output_tokens is None
    assert not c.usage_complete and c.served_model is None


# --- envelope and classification -----------------------------------------------------

@pytest.mark.parametrize("stdout,stderr,code,expected", [
    ("not json", "", 0, ("validation", "malformed_envelope")),
    ("[1, 2]", "", 0, ("validation", "malformed_envelope")),
    ("{}", "", 0, ("validation", "malformed_envelope")),
    (json.dumps({"result": '{"answer": "ok"}'}), "", 0, ("validation", "malformed_envelope")),
    (json.dumps({"subtype": "success", "is_error": "false", "result": '{"answer": "ok"}'}), "", 0,
     ("validation", "malformed_envelope")),
    ("{}", "", 1, ("internal", "unrecognized_exit")),
    (envelope(result="not json"), "", 0, ("validation", "malformed_generated_json")),
    (envelope(result='{"wrong": 1}'), "", 0, ("validation", "schema_mismatch")),
    (json.dumps({"subtype": "success", "is_error": False, "result": 5}), "", 0, ("validation", "malformed_envelope")),
    (envelope(is_error=True, subtype="error_during_execution", result="boom"), "", 0, ("internal", "error_envelope")),
    (envelope(subtype="error_max_turns"), "", 0, ("internal", "error_envelope")),
    (envelope(is_error=True, result="API Error: 529 Overloaded"), "", 0, ("transient", "service_unavailable")),
    ("", "error: unknown option '--disable-slash-commands'", 1, ("config", "unsupported_flag")),
    ("", "Invalid API key · Please run /login", 1, ("config", "not_logged_in")),
    (envelope(is_error=True, result="Not logged in · Please run /login"), "", 0, ("config", "not_logged_in")),
    ("", "segfault-ish nonsense", 139, ("internal", "unrecognized_exit")),
    (envelope(), "", 3, ("internal", "unrecognized_exit")),
])
def test_classification(fake, stdout, stderr, code, expected):
    c = ctx(fake(stdout=stdout, stderr=stderr, code=code))
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert (err.value.error_class, err.value.code) == expected
    assert c.requests == 1


def test_fenced_json_result_accepted(fake):
    assert ctx(fake(stdout=envelope(result='```json\n{"answer": "x"}\n```'))).call("p", Answer).answer == "x"


# --- RunRecord wrapper ---------------------------------------------------------------

def test_record_on_success(fake):
    c = ctx(fake())
    record = run_with_record(c, lambda x: x.call("raw prompt " + SECRET, Answer), agent_name="calm_down",
                             prompt_descriptor="calm-down research: public topic", instructions="be nice",
                             git_sha="abc123")
    assert record.status == "succeeded" and record.output == {"answer": "ok"}
    assert record.provider == "claude-cli" and record.served_model == "claude-sonnet-5-5"
    assert (record.requests, record.input_tokens, record.output_tokens) == (1, 11, 7)
    assert record.git_sha == "abc123" and len(record.config_hash) == 64
    assert len(record.attempts) == 1 and record.attempts[0].error_class is None
    assert SECRET not in record.model_dump_json() and "raw prompt" not in record.model_dump_json()


def test_record_on_failure_keeps_partial_accounting_and_no_raw_text(fake):
    leaky = envelope(is_error=True, subtype="error_during_execution", result="boom " + SECRET)
    script = fake(stdout=leaky, stderr="trace " + SECRET, code=1)
    c = ctx(script)

    def work(x):
        x.call("first", Answer)
        return x.call("second", Answer)

    record = run_with_record(c, work, agent_name="a", prompt_descriptor="d", instructions="i")
    assert record.status == "failed" and record.error_class == "internal" and record.error == "unrecognized_exit"
    assert (record.requests, record.input_tokens, record.output_tokens) == (1, 11, 7)
    assert [a.error for a in record.attempts] == ["unrecognized_exit"]
    assert SECRET not in record.model_dump_json() and len(calls(script)) == 1


def test_record_on_unexpected_exception_hides_message():
    c = ctx("/nonexistent")

    def work(_):
        raise RuntimeError("secret " + SECRET)

    record = run_with_record(c, work, agent_name="a", prompt_descriptor="d", instructions="i")
    assert record.status == "failed" and record.error_class == "internal"
    assert record.error == "unexpected_exception" and SECRET not in record.model_dump_json()
    assert record.requests == 0


def test_record_on_gate_refusal(fake):
    script = fake()
    record = run_with_record(ctx(script, env={}), lambda x: x.call("p", Answer), agent_name="a",
                             prompt_descriptor="d", instructions="i")
    assert record.error_class == "config" and record.error == "gate_not_enabled" and record.requests == 0
    assert calls(script) == []


def test_record_marks_unavailable_usage(fake):
    c = ctx(fake(stdout=envelope(usage=False)))
    record = run_with_record(c, lambda x: x.call("p", Answer), agent_name="a", prompt_descriptor="d",
                             instructions="i")
    assert record.status == "succeeded" and record.error == "usage_unavailable_for_some_calls"
    assert record.attempts[0].error == "usage_unavailable"


def test_config_hash_covers_model_bounds_and_instructions(fake):
    base = ctx(fake())
    h = claude_cli.config_hash(base, "i")
    assert claude_cli.config_hash(base, "other") != h
    assert claude_cli.config_hash(ctx(fake(), max_requests=9), "i") != h
    other_model = ctx(fake())
    other_model.model = "claude-opus-5-5"
    assert claude_cli.config_hash(other_model, "i") != h
    assert claude_cli.config_hash(ctx(fake(), env={**GATE_ENV, "GEMINI_API_KEY": SECRET}), "i") == h


# --- provider selection ----------------------------------------------------

def test_selectable_only_explicitly():
    from agent_lab.config import Settings

    assert Settings.from_env({}).provider == "mock"
    assert Settings.from_env({"AGENT_LAB_PROVIDER": "claude-cli"}).provider == "claude-cli"
    from agent_lab.models import ProviderConfigError, build_model

    with pytest.raises(ProviderConfigError):  # the general factory gains no Claude implementation
        build_model(Settings.from_env({"AGENT_LAB_PROVIDER": "claude-cli"}))


def test_per_call_allowance_narrows_never_widens(fake):
    c = ctx(fake(), max_output_tokens=100)
    with pytest.raises(ClaudeCliError, match="output_tokens_exceeded"):
        c.call("p", Answer, max_output_tokens=5)  # envelope reports 7
    assert ctx(fake(), max_output_tokens=100).call("p", Answer, max_output_tokens=10_000).answer == "ok"


def test_descendant_holding_pipes_after_parent_exit_respects_deadline(fake):
    # The parent exits at once; a grandchild inherits stdout/stderr and sleeps far past the deadline.
    body = ("import subprocess; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=sys.stdout, stderr=sys.stderr)")
    record = run_with_record(ctx(fake(body=body), timeout=0.5), lambda x: x.call("p", Answer), agent_name="a",
                             prompt_descriptor="d", instructions="i")
    start = time.monotonic()
    c = ctx(fake(body=body), timeout=0.5)
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert time.monotonic() - start < 5
    assert (err.value.error_class, err.value.code) == ("limit", "deadline_exceeded")
    assert record.status == "failed" and record.error_class == "limit" and record.requests == 1


def test_descendant_without_pipes_is_reaped_on_success(fake, tmp_path):
    marker = tmp_path / "survived"
    child = tmp_path / "child.py"
    child.write_text(f"import time\ntime.sleep(1.5)\nopen({str(marker)!r}, 'w').write('x')\n")
    body = (f"import subprocess; subprocess.Popen([sys.executable, {str(child)!r}], "
            f"stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)")
    assert ctx(fake(body=body)).call("p", Answer) == Answer(answer="ok")
    time.sleep(2)
    assert not marker.exists()


# --- cache-inclusive input accounting (#28) -------------------------------------------

def test_input_sums_uncached_creation_and_read_across_two_calls(fake):
    u = {"input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
    c = ctx(fake(stdout=envelope(usage_data=u)), max_requests=2)
    c.call("a", Answer)
    c.call("b", Answer)
    assert (c.input_tokens, c.output_tokens) == (2208, 10) and c.usage_complete


def test_explicit_cache_zeros_are_complete(fake):
    c = ctx(fake(stdout=envelope()))
    c.call("p", Answer)
    assert c.input_tokens == 11 and c.usage_complete


@pytest.mark.parametrize("bad", [None, -1, "5", True, 1.5, [3]])
@pytest.mark.parametrize("field", ["cache_creation_input_tokens", "cache_read_input_tokens", "input_tokens"])
def test_bad_component_keeps_known_total_and_marks_unavailable(fake, field, bad):
    u = {"input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
    u[field] = bad
    c = ctx(fake(stdout=envelope(usage_data=u)))
    c.call("p", Answer)
    assert c.input_tokens == 1104 - {"input_tokens": 4, "cache_creation_input_tokens": 100,
                                     "cache_read_input_tokens": 1000}[field]
    assert not c.usage_complete and not c.calls[0].usage_complete


def test_missing_component_is_not_inferred_zero(fake):
    u = {"input_tokens": 4, "output_tokens": 5}
    c = ctx(fake(stdout=envelope(usage_data=u)))
    c.call("p", Answer)
    assert c.input_tokens == 4 and not c.usage_complete


def test_all_input_unavailable_with_valid_output(fake):
    c = ctx(fake(stdout=envelope(usage_data={"output_tokens": 5})))
    c.call("p", Answer)
    assert c.input_tokens == 0 and c.output_tokens == 5
    assert c.calls[0].input_tokens is None and not c.calls[0].usage_complete and not c.usage_complete


def test_model_usage_and_nested_cache_breakdown_not_double_counted(fake):
    u = {**FULL_USAGE, "input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
         "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0}}
    data = json.loads(envelope(usage_data=u))
    data["modelUsage"] = {"claude-sonnet-5-5": {"inputTokens": 4, "cacheReadInputTokens": 1000,
                                                "cacheCreationInputTokens": 100, "outputTokens": 7}}
    c = ctx(fake(stdout=json.dumps(data)))
    c.call("p", Answer)
    assert c.input_tokens == 1104 and c.usage_complete


@pytest.mark.parametrize("result,code", [("not json", "malformed_generated_json"), ('{"wrong": 1}', "schema_mismatch")])
def test_generated_failures_keep_cache_inclusive_usage_without_retry(fake, result, code):
    u = {"input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
    script = fake(stdout=envelope(result=result, usage_data=u))
    c = ctx(script, max_requests=5)
    with pytest.raises(ClaudeCliError) as err:
        c.call("p", Answer)
    assert err.value.code == code and len(calls(script)) == 1
    assert (c.input_tokens, c.output_tokens, c.requests) == (1104, 5, 1)


def test_error_envelope_keeps_cache_inclusive_usage_without_retry(fake):
    u = {"input_tokens": 4, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
    script = fake(stdout=envelope(is_error=True, subtype="error_during_execution", result="boom", usage_data=u), code=1)
    c = ctx(script, max_requests=5)
    with pytest.raises(ClaudeCliError):
        c.call("p", Answer)
    assert len(calls(script)) == 1 and c.input_tokens == 1104
