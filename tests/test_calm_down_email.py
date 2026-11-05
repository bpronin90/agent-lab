"""Offline tests for the Calm Down host runner and the manual n8n workflow export (#36).

Nothing here starts n8n, Gmail, Claude or a network connection. A fake CLI stands in for the research
command, a Python stub stands in for the Gmail node, and the workflow is exercised by a small local
interpreter that follows its connections. That checks the workflow's data and state transitions only;
it does NOT establish native n8n, Execute Command or Gmail node compatibility.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("calm_down_delivery", ROOT / "scripts" / "calm-down-delivery.py")
runner = importlib.util.module_from_spec(spec)
sys.modules["calm_down_delivery"] = runner
spec.loader.exec_module(runner)

WORKFLOW = ROOT / "n8n" / "agent-lab-calm-down-email.json"
NOW = datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc)
FAKE_CLI = textwrap.dedent('''\
    import hashlib, json, os, sys, time
    argv = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\\n")
    mode = os.environ.get("FAKE_MODE", "good")
    out = argv[argv.index("--out") + 1]
    if mode == "crash":
        sys.exit(1)
    if mode in ("hang", "orphan_hang", "orphan_exit"):
        if mode != "hang":  # stand-in for Claude: a grandchild in its own session, without the lock fd
            import subprocess
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            with open(os.environ["FAKE_GRANDCHILD"], "w") as f:
                f.write(str(g.pid))
        if mode != "orphan_exit":
            time.sleep(60)
    banner = "Unreviewed, AI-generated"
    html = "<html><body>" + (banner if mode != "nobanner" else "") + " edition</body></html>"
    text = (banner if mode != "nobanner" else "") + " edition"
    if mode == "fixture":
        html, text = html + "FIXTURE PREVIEW", text + "FIXTURE PREVIEW"
    run_id = "run-1"
    live = mode != "fixture"
    report = {"format": "calm-down-mechanical-report/1", "run_id": run_id, "run_status": "succeeded",
              "provider": "claude-cli", "retrieval": "live" if live else "fixture",
              "research_status": "insufficient" if mode == "insufficient" else "complete",
              "human_review": "absent", "unreviewed": True, "reviewer": None, "problems": [],
              "email_html_sha256": hashlib.sha256(html.encode()).hexdigest(),
              "email_txt_sha256": hashlib.sha256(text.encode()).hexdigest()}
    bundle = {"format": "calm-down-bundle/1", "mode": "live" if live else "fixture", "human_review": "absent",
              "run_meta": {"run_id": run_id}, "sources": [{"backend": "openalex" if live else "fixture"}]}
    for name, body in (("email.html", html), ("email.txt", text)):
        open(os.path.join(out, name), "w").write(body)
    json.dump(report, open(os.path.join(out, "mechanical-report.json"), "w"))
    json.dump(bundle, open(os.path.join(out, "bundle.json"), "w"))
    summary = {"unreviewed": True, "human_review": "absent", "run_id": run_id, "run_status": "succeeded",
               "retrieval": "live" if live else "fixture", "research_status": report["research_status"],
               "problems": [], "pending_review": []}
    if mode == "badsummary":
        summary["run_id"] = "someone-else"
    print(json.dumps(summary))
    ''')


@pytest.fixture
def host(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    log = tmp_path / "launches.jsonl"
    monkeypatch.setattr(runner, "CLI_ARGV", [sys.executable, str(script)])
    root = tmp_path / "private"
    env = {"AGENT_LAB_CALM_DOWN_ROOT": str(root), "AGENT_LAB_CALM_DOWN_RECIPIENT": "owner@example.invalid",
           "FAKE_LOG": str(log), "PATH": os.environ.get("PATH", "")}
    assert runner.main(["init"], env) == 0
    (root / "state" / "ALLOWANCE").write_text("active\n")

    class Host:
        pass

    h = Host()
    h.root, h.env, h.log = root, env, log
    h.cfg = lambda: runner.load_config(env)
    h.launches = lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return h


def b64(fear: str) -> str:
    return base64.b64encode(fear.encode()).decode()


def run_ok(h, fear="AI will replace lawyers", now=NOW, **env):
    return runner.run_research(h.cfg(), b64(fear), {**h.env, **env}, now)


def state(h, eid):
    return json.loads((h.root / "state" / f"exec-{eid}.json").read_text())["state"]


def refused(excinfo, code):
    assert str(excinfo.value) == code or str(excinfo.value).startswith(code), str(excinfo.value)


# ---------------------------------------------------------------- preflight, input and launch

@pytest.mark.parametrize("break_it,code", [
    (lambda h: h.env.pop("AGENT_LAB_CALM_DOWN_ROOT"), "root_not_configured"),
    (lambda h: h.env.update(AGENT_LAB_CALM_DOWN_ROOT="relative/path"), "root_not_configured"),
    (lambda h: h.env.update(AGENT_LAB_CALM_DOWN_RECIPIENT="not-an-address"), "recipient_not_configured"),
    (lambda h: h.env.update(AGENT_LAB_CALM_DOWN_RECIPIENT="a@b.invalid, c@d.invalid"), "recipient_not_configured"),
    (lambda h: os.chmod(h.root, 0o755), "root_not_private"),
    (lambda h: os.chmod(h.root / "state", 0o750), "state_not_private"),
    (lambda h: (h.root / "state" / "ALLOWANCE").write_text("revoked\n"), "allowance_not_active"),
    (lambda h: (h.root / "state" / "ALLOWANCE").unlink(), "allowance_not_active"),
    (lambda h: (h.root / "state" / "ledger.jsonl").unlink(), "ledger_unavailable"),
    (lambda h: (h.root / "state" / "ledger.jsonl").write_text("{not json\n"), "ledger_corrupt"),
    (lambda h: (h.root / "state" / "ledger.jsonl").write_text('{"execution_id": "x", "day": "bad"}\n'), "ledger_corrupt"),
])
def test_preflight_and_attempt_rejection_start_no_research(host, break_it, code):
    break_it(host)
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    refused(exc, code)
    assert host.launches() == []


def test_torn_ledger_line_is_uncertain_accounting(host):
    (host.root / "state" / "ledger.jsonl").write_text(
        json.dumps({"execution_id": "a" * 32, "day": "2026-06-10"}))  # no trailing newline
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    refused(exc, "ledger_corrupt")
    assert host.launches() == []


def test_symlinked_root_is_refused(host, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(host.root)
    host.env["AGENT_LAB_CALM_DOWN_ROOT"] = str(link)
    with pytest.raises(runner.Refusal) as exc:
        host.cfg()
    refused(exc, "root_not_a_directory")


@pytest.mark.parametrize("fear_b64", ["", "%%%%", "not base64!", b64("x" * 301), b64("line\nbreak"),
                                      b64("nul\x00byte"), base64.b64encode(b"\xff\xfe").decode()])
def test_invalid_fear_is_refused_before_reservation(host, fear_b64):
    with pytest.raises(runner.Refusal) as exc:
        runner.run_research(host.cfg(), fear_b64, host.env, NOW)
    refused(exc, "fear_invalid")
    assert host.launches() == [] and (host.root / "state" / "ledger.jsonl").read_text() == ""


@pytest.mark.parametrize("fear", ["--agent=evil", "-rf /", "; touch /tmp/pwned", "$(id) `id` && echo hi", "'quoted' \"too\"",
                                  "--out /elsewhere"])
def test_hostile_and_hyphen_input_stays_one_literal_argument(host, fear):
    result = run_ok(host, fear)
    [argv] = host.launches()
    out = host.root / "executions" / result["execution_id"]
    assert argv == ["calm-down", "--unreviewed", "--out", str(out), "--", fear]
    assert runner.SUBJECT == "Calm Down: unreviewed AI-generated edition"


def test_launch_is_shell_free_with_forced_provider_and_live_retrieval(host, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw, argv=argv)
        return real(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    host.env.update(AGENT_LAB_PROVIDER="gemini", AGENT_LAB_CALM_DOWN_RETRIEVAL="fixture", AGENT_LAB_CLAUDE_MODEL="other")
    run_ok(host)
    assert seen["shell"] is False and isinstance(seen["argv"], list)
    env = seen["env"]
    assert (env["AGENT_LAB_PROVIDER"], env["AGENT_LAB_CLAUDE_MODEL"], env["AGENT_LAB_CALM_DOWN_RETRIEVAL"]) == (
        "claude-cli", "claude-sonnet-5-5", "live")
    assert "AGENT_LAB_CALM_DOWN_RECIPIENT" not in env and "AGENT_LAB_CALM_DOWN_ROOT" not in env


def test_execution_directory_is_fresh_private_and_exclusive(host):
    result = run_ok(host)
    d = host.root / "executions" / result["execution_id"]
    assert d.is_dir() and (d.stat().st_mode & 0o077) == 0
    assert json.loads((d / "execution.json").read_text()) == {"execution_id": result["execution_id"]}


# ---------------------------------------------------------------- daily attempts

def test_fourth_attempt_is_refused_without_research(host):
    for _ in range(3):
        run_ok(host)
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    refused(exc, "daily_attempts_exhausted")
    assert len(host.launches()) == 3


def test_failed_and_crashed_attempts_consume_the_allowance(host):
    for _ in range(3):
        with pytest.raises(runner.Refusal) as exc:
            run_ok(host, FAKE_MODE="crash")
        refused(exc, "research_failed")
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    refused(exc, "daily_attempts_exhausted")
    assert len(host.launches()) == 3


def test_reservation_survives_a_crash_between_reserve_and_research(host, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt  # simulates the process dying after the reservation

    monkeypatch.setattr(runner, "_launch_cli", boom)
    with pytest.raises(KeyboardInterrupt):
        run_ok(host)
    entries = (host.root / "state" / "ledger.jsonl").read_text().splitlines()
    assert len(entries) == 1
    eid = json.loads(entries[0])["execution_id"]
    assert state(host, eid) == "failed"
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)


def test_reserved_but_never_finished_execution_cannot_be_claimed(host):
    eid = runner.reserve_attempt(host.cfg(), NOW)  # the process "died" here
    assert state(host, eid) == "researching"
    with pytest.raises(runner.Refusal) as exc:
        runner.claim_delivery(host.cfg(), eid)
    refused(exc, "claim_refused_state_researching")


def test_timeout_kills_research_and_keeps_the_reservation(host, monkeypatch):
    monkeypatch.setattr(runner, "RESEARCH_TIMEOUT_SECONDS", 1)
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host, FAKE_MODE="hang")
    refused(exc, "research_timeout")
    assert len((host.root / "state" / "ledger.jsonl").read_text().splitlines()) == 1


def test_interrupted_runner_kills_its_research_child(host, monkeypatch):
    started = {}
    real = subprocess.Popen

    def spy(argv, **kw):
        started["proc"] = real(argv, **kw)
        return started["proc"]

    def interrupted(self, timeout=None):
        raise KeyboardInterrupt  # the runner is terminated while the child is still researching

    host.env["FAKE_MODE"] = "hang"
    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    monkeypatch.setattr(real, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_ok(host, FAKE_MODE="hang")
    assert started["proc"].poll() is not None  # reaped and dead, not still running
    monkeypatch.undo()


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_for(path: Path) -> int:
    import time
    for _ in range(200):
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("grandchild never started")


def test_interrupted_runner_kills_separately_sessioned_grandchild_before_unlocking(host, monkeypatch, tmp_path):
    marker = tmp_path / "grandchild.pid"
    host.env["FAKE_GRANDCHILD"] = str(marker)
    real = subprocess.Popen
    real_communicate = real.communicate
    seen = {}

    def interrupted(self, timeout=None):
        seen["gpid"] = _wait_for(marker)
        assert _alive(seen["gpid"])
        raise KeyboardInterrupt

    real_kill = runner._kill_descendants

    def checking_kill():
        real_kill()
        # still inside the run lock: a second run must be refused while the cleanup finishes
        with pytest.raises(runner.Refusal) as busy:
            with runner._lock(host.root / "state" / "run.lock", blocking=False):
                pass
        seen["busy"] = str(busy.value)

    monkeypatch.setattr(real, "communicate", interrupted)
    monkeypatch.setattr(runner, "_kill_descendants", checking_kill)
    with pytest.raises(KeyboardInterrupt):
        run_ok(host, FAKE_MODE="orphan_hang")
    monkeypatch.setattr(real, "communicate", real_communicate)
    assert not _alive(seen["gpid"])
    assert seen["busy"] == "another_run_in_progress"


def test_grandchild_left_behind_by_a_finished_cli_is_killed(host, tmp_path):
    marker = tmp_path / "grandchild.pid"
    host.env["FAKE_GRANDCHILD"] = str(marker)
    assert run_ok(host, FAKE_MODE="orphan_exit")["state"] == "ready"  # CLI exits, grandchild still sleeping
    assert not _alive(int(marker.read_text()))


def test_unconfirmed_descendant_death_refuses(monkeypatch):
    monkeypatch.setattr(runner, "REAP_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr(runner, "_descendants", lambda pid: [2 ** 22 + 7])  # never disappears
    with pytest.raises(runner.Refusal) as exc:
        runner._kill_descendants()
    assert str(exc.value) == "research_descendants_survived"


def test_missing_subreaper_support_refuses_before_reservation(host, monkeypatch):
    def unavailable():
        raise runner.Refusal("subreaper_unavailable")
    monkeypatch.setattr(runner, "_become_subreaper", unavailable)
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    assert str(exc.value) == "subreaper_unavailable"
    assert not (host.root / "state" / "ledger.jsonl").read_text().strip()


def test_research_child_inherits_the_run_lock(host, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw)
        seen["lock_target"] = os.readlink(f"/proc/self/fd/{kw['pass_fds'][0]}")
        return real(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    run_ok(host)
    assert seen["lock_target"] == str(host.root / "state" / "run.lock")


def test_concurrent_reservations_never_over_allocate(host):
    results, errors = [], []

    def go():
        try:
            results.append(runner.reserve_attempt(host.cfg(), NOW))
        except runner.Refusal as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 3 and len(set(results)) == 3 and errors == ["daily_attempts_exhausted"] * 9


def test_concurrent_research_runs_are_serialized_not_queued(host):
    lock_path = host.root / "state" / "run.lock"
    with runner._lock(lock_path):
        with pytest.raises(runner.Refusal) as exc:
            run_ok(host)
    refused(exc, "another_run_in_progress")
    assert host.launches() == [] and (host.root / "state" / "ledger.jsonl").read_text() == ""


def test_manual_reserve_shares_the_same_ledger(host):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert runner.main(["reserve"], host.env, NOW) == 0
    run_ok(host)
    run_ok(host)
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host)
    refused(exc, "daily_attempts_exhausted")


def test_new_york_midnight_and_dst_boundaries(host):
    cfg = host.cfg()
    # EST (UTC-5): 04:59Z is 23:59 on Jan 14, 05:00Z is already Jan 15.
    before, after = datetime(2026, 1, 15, 4, 59, tzinfo=timezone.utc), datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)
    assert runner.ledger_day(before) == "2026-01-14" and runner.ledger_day(after) == "2026-01-15"
    # Spring forward on 2026-03-08 makes that New York day 23 hours long (05:00Z to 04:00Z).
    start = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
    assert runner.ledger_day(start - timedelta(seconds=1)) == "2026-03-07"
    assert runner.ledger_day(start + timedelta(hours=22, minutes=59)) == "2026-03-08"
    assert runner.ledger_day(start + timedelta(hours=23)) == "2026-03-09"
    # Fall back on 2026-11-01 makes it 25 hours long (04:00Z to 05:00Z the next day).
    start = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    assert runner.ledger_day(start + timedelta(hours=24, minutes=59)) == "2026-11-01"
    assert runner.ledger_day(start + timedelta(hours=25)) == "2026-11-02"
    for _ in range(3):
        runner.reserve_attempt(cfg, before)
    with pytest.raises(runner.Refusal):
        runner.reserve_attempt(cfg, before + timedelta(seconds=30))  # still Jan 14 in New York
    assert runner.reserve_attempt(cfg, after)  # the New York day rolled over


# ---------------------------------------------------------------- artifacts bound to the run

@pytest.mark.parametrize("mode,code", [
    ("fixture", "artifact_not_deliverable"), ("nobanner", "artifact_not_deliverable"),
    ("insufficient", "artifact_not_deliverable"), ("badsummary", "summary_mismatch"),
    ("crash", "research_failed")])
def test_unfit_editions_never_become_ready(host, mode, code):
    with pytest.raises(runner.Refusal) as exc:
        run_ok(host, FAKE_MODE=mode)
    refused(exc, code)
    (entry,) = (host.root / "state" / "ledger.jsonl").read_text().splitlines()
    eid = json.loads(entry)["execution_id"]
    assert state(host, eid) == "failed"
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)


def tamper(h, eid, name, new=None):
    path = h.root / "executions" / eid / name
    path.write_text(new if new is not None else "changed")


@pytest.mark.parametrize("what", ["html", "text", "report", "bundle", "missing", "symlink", "wrong_execution",
                                  "dir_symlink"])
def test_stale_changed_or_escaping_artifacts_prevent_claim(host, tmp_path, what):
    eid = run_ok(host)["execution_id"]
    d = host.root / "executions" / eid
    if what == "html":
        tamper(host, eid, "email.html", "<html>Unreviewed, AI-generated injected</html>")
    elif what == "text":
        tamper(host, eid, "email.txt", "Unreviewed, AI-generated different")
    elif what == "report":
        rep = json.loads((d / "mechanical-report.json").read_text())
        rep["research_status"] = "insufficient"
        tamper(host, eid, "mechanical-report.json", json.dumps(rep))
    elif what == "bundle":
        tamper(host, eid, "bundle.json", "{}")
    elif what == "missing":
        (d / "email.html").unlink()
    elif what == "symlink":
        outside = tmp_path / "outside.html"
        outside.write_text((d / "email.html").read_text())
        (d / "email.html").unlink()
        (d / "email.html").symlink_to(outside)
    elif what == "wrong_execution":
        (d / "execution.json").write_text(json.dumps({"execution_id": "f" * 32}))
    else:
        moved = tmp_path / "moved"
        d.rename(moved)
        d.symlink_to(moved)
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)
    assert state(host, eid) == "invalid"  # and nothing was handed out: claim raised before returning


def test_revocation_after_ready_prevents_claim_and_hands_out_nothing(host):
    eid = run_ok(host)["execution_id"]
    (host.root / "state" / "ALLOWANCE").unlink()
    with pytest.raises(runner.Refusal) as exc:
        runner.claim_delivery(host.cfg(), eid)
    refused(exc, "allowance_not_active")
    assert state(host, eid) == "ready"  # no claim was persisted; restoring the allowance re-enables it
    (host.root / "state" / "ALLOWANCE").write_text("active")
    assert runner.claim_delivery(host.cfg(), eid)["execution_id"] == eid


@pytest.mark.parametrize("change", ["source_text", "dossier", "digest_mismatch"])
def test_changed_bundle_contents_prevent_claim(host, change):
    eid = run_ok(host)["execution_id"]
    path = host.root / "executions" / eid / "bundle.json"
    bundle = json.loads(path.read_text())
    if change == "dossier":
        bundle["dossier"] = {"status": "insufficient"}  # metadata the older checks look at is untouched
    elif change == "source_text":
        bundle["sources"] = [{"backend": "openalex", "read": True, "text": "swapped", "text_sha256": "0" * 64}]
    else:
        bundle["sources"][0].update(read=True, text="x", text_sha256=hashlib.sha256(b"y").hexdigest())
    path.write_text(json.dumps(bundle))
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)
    assert state(host, eid) == "invalid"


@pytest.mark.parametrize("bad", ["../x", "..", "a" * 31, "A" * 32, "x" * 32, "", "../../etc/passwd"])
def test_execution_id_path_traversal_is_refused(host, bad):
    for call in (runner.claim_delivery, lambda c, e: runner.acknowledge(c, e, "sent")):
        with pytest.raises(runner.Refusal) as exc:
            call(host.cfg(), bad)
        refused(exc, "execution_id_invalid")


# ---------------------------------------------------------------- the delivery claim

def test_claim_returns_validated_html_and_fixed_subject_and_private_recipient(host):
    eid = run_ok(host)["execution_id"]
    claim = runner.claim_delivery(host.cfg(), eid, NOW)
    assert claim == {"execution_id": eid, "to": "owner@example.invalid", "subject": runner.SUBJECT,
                     "html": (host.root / "executions" / eid / "email.html").read_text()}
    assert state(host, eid) == "send_attempted"


def test_second_claim_is_refused_even_after_restart(host):
    eid = run_ok(host)["execution_id"]
    runner.claim_delivery(host.cfg(), eid)
    for _ in range(2):  # a fresh Config load is a restart: state is only what is on disk
        with pytest.raises(runner.Refusal) as exc:
            runner.claim_delivery(runner.load_config(host.env), eid)
        refused(exc, "claim_refused_state_send_attempted")


def test_concurrent_claims_succeed_exactly_once(host):
    eid = run_ok(host)["execution_id"]
    wins, losses = [], []

    def go():
        try:
            wins.append(runner.claim_delivery(host.cfg(), eid))
        except runner.Refusal:
            losses.append(1)

    threads = [threading.Thread(target=go) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(losses) == 9


def test_no_edge_leads_back_to_ready_or_resend(host):
    eid = run_ok(host)["execution_id"]
    with pytest.raises(runner.Refusal):
        runner.acknowledge(host.cfg(), eid, "sent")  # cannot ack what was never claimed
    runner.claim_delivery(host.cfg(), eid)
    assert runner.acknowledge(host.cfg(), eid, "unknown")["state"] == "unknown"
    for outcome in ("sent", "unknown"):
        with pytest.raises(runner.Refusal):
            runner.acknowledge(host.cfg(), eid, outcome)
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)
    with pytest.raises(runner.Refusal) as exc:
        runner.acknowledge(host.cfg(), eid, "ready")
    refused(exc, "outcome_invalid")


def test_lost_acknowledgement_stays_send_attempted_and_blocks_resend(host):
    eid = run_ok(host)["execution_id"]
    runner.claim_delivery(host.cfg(), eid)  # n8n crashed or timed out; no ack ever arrives
    assert state(host, eid) == "send_attempted"
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)


def test_corrupt_or_missing_state_refuses(host):
    eid = run_ok(host)["execution_id"]
    (host.root / "state" / f"exec-{eid}.json").write_text("{broken")
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)
    (host.root / "state" / f"exec-{eid}.json").unlink()
    with pytest.raises(runner.Refusal):
        runner.acknowledge(host.cfg(), eid, "sent")


def test_command_line_exit_codes_and_no_private_values_in_output(host, capsys):
    assert runner.main(["run", "--fear-b64", b64("a fear")], host.env, NOW) == 0
    out = json.loads(capsys.readouterr().out)
    assert runner.main(["claim", "--execution-id", out["execution_id"]], host.env, NOW) == 0
    capsys.readouterr()
    assert runner.main(["claim", "--execution-id", out["execution_id"]], host.env, NOW) == 2
    err = capsys.readouterr().err
    assert err.strip() == "refused: claim_refused_state_send_attempted"
    assert "owner@example.invalid" not in err and str(host.root) not in err
    assert runner.main(["status", "--execution-id", out["execution_id"]], host.env) == 0
    assert runner.main(["bogus"], host.env) == 2


# ---------------------------------------------------------------- the workflow export

def load_workflow():
    return json.loads(WORKFLOW.read_text())


def test_workflow_export_is_inactive_manual_and_secret_free():
    wf = load_workflow()
    raw = WORKFLOW.read_text()
    assert wf["active"] is False
    types = [n["type"] for n in wf["nodes"]]
    assert types.count("n8n-nodes-base.manualTrigger") == 1
    assert "n8n-nodes-base.executeCommand" not in types and types.count("n8n-nodes-base.ssh") == 4
    for forbidden in ("schedule", "cron", "webhook", "httpRequest", "emailSend", "Trigger") :
        assert not any(forbidden.lower() in t.lower() and t != "n8n-nodes-base.manualTrigger" for t in types), forbidden
    assert all("credentials" not in n for n in wf["nodes"])
    for secret in ("@", "/home/", "client_secret", "refresh_token", "oauth"):
        assert secret not in raw.replace("n8n-nodes", "").replace("calm-down-", "") or secret == "@" and "@" not in raw
    assert "settings" in wf and wf["settings"]["saveDataSuccessExecution"] == "none"
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    reachable, frontier = set(), ["Manual Trigger"]
    while frontier:
        cur = frontier.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        for branch in wf["connections"].get(cur, {}).get("main", []):
            frontier += [c["node"] for c in branch]
    assert reachable == set(names)  # nothing dangling or unreachable


def test_workflow_disables_retries_everywhere_a_send_or_research_could_repeat():
    for node in load_workflow()["nodes"]:
        if node["type"] in ("n8n-nodes-base.ssh", "n8n-nodes-base.gmail"):
            assert node.get("retryOnFail") is False and node.get("maxTries") == 1, node["name"]
    gmail = next(n for n in load_workflow()["nodes"] if n["type"] == "n8n-nodes-base.gmail")
    assert gmail["onError"] == "continueErrorOutput" and gmail["parameters"]["emailType"] == "html"


def test_workflow_commands_only_use_the_fixed_runner_and_inert_values():
    prefix = '="$AGENT_LAB_CALM_DOWN_PYTHON" "$AGENT_LAB_CALM_DOWN_RUNNER" '
    for node in load_workflow()["nodes"]:
        if node["type"] != "n8n-nodes-base.ssh":
            continue
        assert node["parameters"]["resource"] == "command" and node["parameters"]["operation"] == "execute"
        command = node["parameters"]["command"]
        assert command.startswith(prefix), node["name"]
        for expr in re.findall(r"\{\{(.*?)\}\}", command):
            assert re.fullmatch(r"\s*(\$json\.fear_b64|JSON\.parse\(\$\('(Run Research|Claim Delivery)'\)\.first\(\)"
                                r"\.json\.stdout\)\.execution_id)\s*", expr), expr
        assert "fear" not in command.replace("fear_b64", "").replace("--fear-b64", "")


def test_hostile_fear_cannot_reach_the_shell_through_the_run_command(tmp_path):
    stub = tmp_path / "stub.py"
    marker = tmp_path / "pwned"
    stub.write_text("import json, sys\nopen(sys.argv[0] + '.args', 'w').write(json.dumps(sys.argv[1:]))\n")
    node = next(n for n in load_workflow()["nodes"] if n["name"] == "Run Research")
    for fear in ("; touch %s" % marker, "$(touch %s)" % marker, "`touch %s`" % marker, "--x\n'\"|&", "-rf /"):
        encoded = b64(fear)  # what the "Bound And Encode Fear" node emits
        assert re.fullmatch(r"[A-Za-z0-9+/=]+", encoded)
        command = node["parameters"]["command"][1:].replace("{{ $json.fear_b64 }}", encoded)
        env = {"PATH": os.environ["PATH"], "AGENT_LAB_CALM_DOWN_PYTHON": sys.executable,
               "AGENT_LAB_CALM_DOWN_RUNNER": str(stub)}
        subprocess.run(["sh", "-c", command], env=env, check=True)
        assert json.loads(Path(str(stub) + ".args").read_text()) == ["run", "--fear-b64", encoded]
        assert not marker.exists()
        if "\n" in fear:  # the Code node rejects control characters; the runner would too
            with pytest.raises(runner.Refusal):
                runner.decode_fear(encoded)
        else:
            assert runner.decode_fear(encoded) == fear.strip()


# A small interpreter that follows the export's connections. It only runs what the export contains.
class NodeFailed(Exception):
    pass


GATES = {"Research Succeeded?": "Run Research", "Claim Succeeded?": "Claim Delivery"}


def execute_workflow(h, fear, gmail, now=NOW):
    wf = load_workflow()
    nodes = {n["name"]: n for n in wf["nodes"]}
    outputs: dict[str, dict] = {}
    queue, executed = [("Manual Trigger", {})], []

    def expr(text, current):
        def sub(m):
            body = m.group(1).strip()
            if body == "$json.fear_b64":
                return current["fear_b64"]
            ref = re.fullmatch(r"JSON\.parse\((.+)\.stdout\)\.(\w+)", body)
            src = current if ref.group(1) == "$json" else outputs[re.search(r"\$\('(.+?)'\)", ref.group(1)).group(1)]
            return json.loads(src["stdout"])[ref.group(2)]
        return re.sub(r"\{\{(.*?)\}\}", sub, text)

    while queue:
        name, item = queue.pop(0)
        node, kind = nodes[name], nodes[name]["type"].split(".")[-1]
        executed.append(name)
        branch = 0
        if kind == "manualTrigger":
            out = {}
        elif kind == "set":
            out = {"fear": fear}
        elif kind == "code":
            assert 0 < len(fear.strip()) <= 300 and "Buffer.from(fear, 'utf8').toString('base64')" in node["parameters"]["jsCode"]
            out = {"fear_b64": b64(fear.strip())}
        elif kind == "if":
            assert "$json.code" in node["parameters"]["conditions"]["conditions"][0]["leftValue"]
            if item["code"] != 0:  # the false branch is unconnected: the workflow ends here
                raise NodeFailed(GATES[name])
            out = item
        elif kind == "ssh":
            cmd = expr(node["parameters"]["command"][1:], item)
            parts = shlex.split(cmd)[2:]  # drop the two fixed "$AGENT_LAB_CALM_DOWN_*" words
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = runner.main(parts, h.env, now)
            out = {"code": code, "stdout": buf.getvalue(), "stderr": err.getvalue()}  # SSH node: no failure on non-zero
        elif kind == "gmail":
            claim = json.loads(item["stdout"])
            try:
                out = gmail(expr(node["parameters"]["sendTo"][1:], item), expr(node["parameters"]["subject"][1:], item),
                            expr(node["parameters"]["message"][1:], item), node["parameters"]["emailType"])
                out = out or {}
            except Exception:
                branch, out = 1, {"error": "gmail failed"}
            assert claim["to"] == "owner@example.invalid"
        else:
            raise AssertionError(kind)
        outputs[name] = out
        branches = wf["connections"].get(name, {}).get("main", [])
        if branch < len(branches):
            queue += [(c["node"], out) for c in branches[branch]]
    return executed


class GmailStub:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, to, subject, message, email_type):
        self.sent.append((to, subject, message, email_type))
        if self.fail:
            raise TimeoutError("lost acknowledgement")
        return {"id": "m1"}


def only_state(h):
    ids = [json.loads(x)["execution_id"] for x in (h.root / "state" / "ledger.jsonl").read_text().splitlines()]
    return [(i, state(h, i)) for i in ids]


def test_workflow_happy_path_sends_validated_html_once(host):
    gmail = GmailStub()
    executed = execute_workflow(host, "AI will replace lawyers", gmail)
    assert executed[-1] == "Ack Sent" and "Ack Unknown" not in executed
    (to, subject, message, kind), = gmail.sent
    assert (to, subject, kind) == ("owner@example.invalid", runner.SUBJECT, "html")
    assert "Unreviewed, AI-generated" in message and message.startswith("<html>")
    [(eid, st)] = only_state(host)
    assert st == "sent"
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)  # re-execution cannot send again


def test_workflow_ambiguous_gmail_outcome_is_recorded_unknown_and_never_resent(host):
    gmail = GmailStub(fail=True)
    executed = execute_workflow(host, "AI will replace lawyers", gmail)
    assert executed[-1] == "Ack Unknown" and len(gmail.sent) == 1
    [(eid, st)] = only_state(host)
    assert st == "unknown"
    with pytest.raises(runner.Refusal):
        runner.claim_delivery(host.cfg(), eid)
    assert len(gmail.sent) == 1


@pytest.mark.parametrize("mode", ["fixture", "nobanner", "insufficient", "badsummary", "crash"])
def test_workflow_rejected_editions_invoke_no_send(host, mode):
    gmail = GmailStub()
    host.env["FAKE_MODE"] = mode
    with pytest.raises(NodeFailed) as exc:
        execute_workflow(host, "a fear", gmail)
    assert str(exc.value) == "Run Research" and gmail.sent == []


def test_workflow_fourth_attempt_invokes_no_research_and_no_send(host):
    gmail = GmailStub()
    for _ in range(3):
        execute_workflow(host, "a fear", gmail)
    launches = len(host.launches())
    with pytest.raises(NodeFailed) as exc:
        execute_workflow(host, "a fear", gmail)
    assert str(exc.value) == "Run Research" and len(host.launches()) == launches == 3 and len(gmail.sent) == 3


def test_workflow_revoked_allowance_or_bad_config_invokes_nothing(host):
    gmail = GmailStub()
    (host.root / "state" / "ALLOWANCE").write_text("revoked")
    with pytest.raises(NodeFailed):
        execute_workflow(host, "a fear", gmail)
    (host.root / "state" / "ALLOWANCE").write_text("active")
    host.env["AGENT_LAB_CALM_DOWN_RECIPIENT"] = ""
    with pytest.raises(NodeFailed):
        execute_workflow(host, "a fear", gmail)
    assert host.launches() == [] and gmail.sent == []


def test_workflow_tampered_artifacts_between_research_and_send_invoke_no_send(host):
    gmail = GmailStub()
    real_main = runner.main

    def tamper_before_claim(argv=None, env=None, now=None):
        if argv and argv[0] == "claim":
            eid = argv[argv.index("--execution-id") + 1]
            (host.root / "executions" / eid / "email.html").write_text("<html>Unreviewed, AI-generated evil</html>")
        return real_main(argv, env, now)

    runner.main = tamper_before_claim
    try:
        with pytest.raises(NodeFailed) as exc:
            execute_workflow(host, "a fear", gmail)
    finally:
        runner.main = real_main
    assert str(exc.value) == "Claim Delivery" and gmail.sent == []
