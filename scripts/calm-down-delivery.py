#!/usr/bin/env python3
"""Bounded host runner for the manually triggered local n8n Calm Down delivery workflow.

Standard library only. It is the only thing the n8n workflow runs on the host. It never sends email:
it validates, claims and records. n8n's Gmail node does the sending between `claim` and `ack`.

Subcommands (all state lives under the private root AGENT_LAB_CALM_DOWN_ROOT):

  init                       create the private directories and an EMPTY attempt ledger (owner, once)
  reserve                    reserve one daily attempt and print its execution id (manual CLI runs)
  run --fear-b64 <base64>    preflight, reserve an attempt, run the unreviewed research, validate
  claim --execution-id <id>  atomically claim a READY edition for exactly one send attempt
  ack --execution-id <id> --outcome sent|unknown
  status --execution-id <id>

Guarantees and limits (see docs/experiments/calm-down.md):
  * Attempts are reserved in an append-only ledger (3 per America/New_York day, shared by every caller
    that uses this script) BEFORE any research. A crash keeps the reservation. A missing, unreadable or
    uncertain ledger, a revoked allowance or a fourth attempt refuses and starts nothing.
  * A delivery is claimed once (ready -> send_attempted) before the email is handed to n8n. Nothing here
    ever moves a claimed edition back to ready or claims it again, even after a crash or restart. This is
    at-most-once ATTEMPT semantics: a lost email is possible and is never repaired automatically.
  * Locks are advisory flock locks on one local filesystem. This is not protection against someone who
    controls the account or the files, and it is not an OS sandbox.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

DAILY_ATTEMPTS = 3
MODEL = "claude-sonnet-5-5"
PROVIDER = "claude-cli"
MAX_FEAR_CHARS = 300
MAX_FEAR_B64_CHARS = 4 * MAX_FEAR_CHARS + 8  # 4 UTF-8 bytes per char, base64 expansion
RESEARCH_TIMEOUT_SECONDS = 900
MAX_ARTIFACT_BYTES = 2_000_000
SUBJECT = "Calm Down: unreviewed AI-generated edition"  # fixed; never model-derived
BANNER = "Unreviewed, AI-generated"
LEDGER_TZ = "America/New_York"
# Fixed launcher. Tests may replace it with a fake; production never takes it from input.
CLI_ARGV = [sys.executable, "-m", "agent_lab"]

ID_RE = re.compile(r"^[0-9a-f]{32}$")
B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
BUNDLE_FORMAT = "calm-down-bundle/1"
REPORT_FORMAT = "calm-down-mechanical-report/1"


class Refusal(Exception):
    """A refused or failed step. The message is a fixed code: never fear text, paths or secrets."""


@dataclass(frozen=True)
class Config:
    root: Path
    recipient: str

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def executions(self) -> Path:
        return self.root / "executions"


# ---------------------------------------------------------------- configuration and private paths

def _private_dir(path: Path, code: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        raise Refusal(f"{code}_missing") from None
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise Refusal(f"{code}_not_a_directory")
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refusal(f"{code}_not_private")


def load_config(env: Mapping[str, str]) -> Config:
    """Validate all local configuration. Called before any reservation or research."""
    raw = env.get("AGENT_LAB_CALM_DOWN_ROOT", "")
    if not raw or not os.path.isabs(raw):
        raise Refusal("root_not_configured")
    recipient = env.get("AGENT_LAB_CALM_DOWN_RECIPIENT", "")
    if not EMAIL_RE.fullmatch(recipient):
        raise Refusal("recipient_not_configured")
    cfg = Config(Path(raw), recipient)
    _private_dir(cfg.root, "root")
    _private_dir(cfg.state, "state")
    _private_dir(cfg.executions, "executions")
    return cfg


def init_state(env: Mapping[str, str]) -> None:
    raw = env.get("AGENT_LAB_CALM_DOWN_ROOT", "")
    if not raw or not os.path.isabs(raw):
        raise Refusal("root_not_configured")
    root = Path(raw)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for sub in (root / "state", root / "executions"):
        sub.mkdir(mode=0o700, exist_ok=True)
    ledger = root / "state" / "ledger.jsonl"
    if not ledger.exists():
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)


# ---------------------------------------------------------------- input

def decode_fear(fear_b64: str) -> str:
    """The fear arrives base64-encoded so the workflow never places free text on a command line."""
    if not (0 < len(fear_b64) <= MAX_FEAR_B64_CHARS) or not B64_RE.fullmatch(fear_b64):
        raise Refusal("fear_invalid")
    try:
        fear = base64.b64decode(fear_b64, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError):
        raise Refusal("fear_invalid") from None
    if not (0 < len(fear) <= MAX_FEAR_CHARS) or any(unicodedata.category(c).startswith("C") for c in fear):
        raise Refusal("fear_invalid")
    return fear


# ---------------------------------------------------------------- locks and durable files

@contextmanager
def _lock(path: Path, blocking: bool = True) -> Iterator[int]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Refusal("another_run_in_progress") from None
        yield fd
    finally:
        os.close(fd)  # releases the flock


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(data, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# ---------------------------------------------------------------- the attempt ledger

def ledger_day(now: datetime) -> str:
    """The America/New_York calendar date. Tz database errors propagate as a refusal (fail closed)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return now.astimezone(ZoneInfo(LEDGER_TZ)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise Refusal("ledger_clock_unavailable") from None


def _read_ledger(path: Path) -> list[dict]:
    try:
        raw = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Refusal("ledger_unavailable") from None
    entries = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            raise Refusal("ledger_corrupt") from None
        if (not isinstance(entry, dict) or not ID_RE.fullmatch(str(entry.get("execution_id", "")))
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("day", "")))):
            raise Refusal("ledger_corrupt")
        entries.append(entry)
    if raw and not raw.endswith("\n"):
        raise Refusal("ledger_corrupt")  # a torn final line is uncertain accounting
    return entries


def _allowance_active(cfg: Config) -> bool:
    try:
        return (cfg.state / "ALLOWANCE").read_text("utf-8").strip() == "active"
    except (OSError, UnicodeDecodeError):
        return False


def reserve_attempt(cfg: Config, now: datetime | None = None) -> str:
    """Atomically count and record one attempt BEFORE research. Returns the new execution id."""
    now = now or datetime.now(timezone.utc)
    day = ledger_day(now)
    with _lock(cfg.state / "state.lock"):
        if not _allowance_active(cfg):
            raise Refusal("allowance_not_active")
        ledger = cfg.state / "ledger.jsonl"
        used = sum(1 for e in _read_ledger(ledger) if e["day"] == day)
        if used >= DAILY_ATTEMPTS:
            raise Refusal("daily_attempts_exhausted")
        eid = uuid.uuid4().hex
        line = json.dumps({"execution_id": eid, "day": day, "reserved_at": now.isoformat()}) + "\n"
        fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        _write_json_atomic(cfg.state / f"exec-{eid}.json",
                           {"execution_id": eid, "state": "researching", "day": day})
        return eid


# ---------------------------------------------------------------- execution state machine

_NEXT = {  # the only legal transitions; there is no edge back to "ready" and none out of "sent"/"unknown"
    "researching": {"ready", "failed"},
    "ready": {"send_attempted", "invalid"},
    "send_attempted": {"sent", "unknown"},
}


def _state_path(cfg: Config, eid: str) -> Path:
    if not ID_RE.fullmatch(eid):
        raise Refusal("execution_id_invalid")
    return cfg.state / f"exec-{eid}.json"


def _load_state(cfg: Config, eid: str) -> dict:
    try:
        data = json.loads(_state_path(cfg, eid).read_text("utf-8"))
    except (OSError, ValueError):
        raise Refusal("state_unavailable") from None
    if not isinstance(data, dict) or data.get("execution_id") != eid or data.get("state") not in (
            "researching", "failed", "ready", "invalid", "send_attempted", "sent", "unknown"):
        raise Refusal("state_corrupt")
    return data


def _transition(cfg: Config, eid: str, to: str, **fields) -> dict:
    """Move one execution along _NEXT under the state lock, durably, or refuse."""
    with _lock(cfg.state / "state.lock"):
        data = _load_state(cfg, eid)
        if to not in _NEXT.get(data["state"], set()):
            raise Refusal(f"state_{data['state']}_cannot_become_{to}")
        data.update(fields, state=to)
        _write_json_atomic(_state_path(cfg, eid), data)
        return data


# ---------------------------------------------------------------- artifact validation

def _read_private(dirfd: int, name: str) -> bytes:
    """Read one fixed-name regular file inside the execution directory; never follows a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        raise Refusal("artifact_missing") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ARTIFACT_BYTES:
            raise Refusal("artifact_invalid")
        return os.read(fd, st.st_size + 1)[: st.st_size]
    finally:
        os.close(fd)


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refusal("artifact_invalid") from None
    if not isinstance(data, dict):
        raise Refusal("artifact_invalid")
    return data


def validate_artifacts(cfg: Config, eid: str, summary: dict | None = None) -> dict:
    """Prove the execution directory still holds exactly one mechanically accepted, unreviewed, live edition
    produced by THIS execution. Returns the run id, digests and the validated bodies."""
    if not ID_RE.fullmatch(eid):
        raise Refusal("execution_id_invalid")
    path = cfg.executions / eid
    try:
        dirfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # a symlinked dir fails here
    except OSError:
        raise Refusal("artifact_missing") from None
    try:
        if os.path.realpath(path) != str(cfg.executions.resolve() / eid):
            raise Refusal("artifact_path_escape")
        if _json(_read_private(dirfd, "execution.json")).get("execution_id") != eid:
            raise Refusal("artifact_wrong_execution")
        report_bytes = _read_private(dirfd, "mechanical-report.json")
        bundle_bytes = _read_private(dirfd, "bundle.json")
        report, bundle = _json(report_bytes), _json(bundle_bytes)
        html_bytes = _read_private(dirfd, "email.html")
        text_bytes = _read_private(dirfd, "email.txt")
    finally:
        os.close(dirfd)

    run_id = report.get("run_id")
    checks = (
        report.get("format") == REPORT_FORMAT, isinstance(run_id, str) and bool(run_id),
        report.get("run_status") == "succeeded", report.get("provider") == PROVIDER,
        report.get("retrieval") == "live", report.get("research_status") == "complete",
        report.get("unreviewed") is True, report.get("human_review") == "absent", report.get("reviewer") is None,
        report.get("problems") == [],
        bundle.get("format") == BUNDLE_FORMAT, bundle.get("mode") == "live",
        bundle.get("human_review") == "absent",
        isinstance(bundle.get("run_meta"), dict) and bundle["run_meta"].get("run_id") == run_id,
        isinstance(bundle.get("sources"), list) and bool(bundle["sources"])
        and all(isinstance(s, dict) and s.get("backend") != "fixture" for s in bundle["sources"]),
    )
    if not all(checks):
        raise Refusal("artifact_not_deliverable")
    for src in bundle["sources"]:  # bundle integrity: every read source text still matches its recorded digest
        if src.get("read") and (not isinstance(src.get("text"), str)
                                or src.get("text_sha256") != hashlib.sha256(src["text"].encode()).hexdigest()):
            raise Refusal("artifact_changed")
    html_sha, text_sha = hashlib.sha256(html_bytes).hexdigest(), hashlib.sha256(text_bytes).hexdigest()
    if report.get("email_html_sha256") != html_sha or report.get("email_txt_sha256") != text_sha:
        raise Refusal("artifact_changed")
    try:
        html, text = html_bytes.decode("utf-8"), text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("artifact_invalid") from None
    if (BANNER not in html or BANNER not in text or "FIXTURE PREVIEW" in html or "FIXTURE PREVIEW" in text
            or "<html" not in html.lower() or "<script" in html.lower()):
        raise Refusal("artifact_not_deliverable")
    if summary is not None and not (
            summary.get("run_id") == run_id and summary.get("unreviewed") is True
            and summary.get("human_review") == "absent" and summary.get("research_status") == "complete"
            and summary.get("run_status") == "succeeded" and summary.get("retrieval") == "live"
            and summary.get("problems") == [] and summary.get("pending_review") == []):
        raise Refusal("summary_mismatch")
    return {"run_id": run_id, "html_sha256": html_sha, "text_sha256": text_sha, "html": html,
            "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest()}


_BOUND = ("run_id", "html_sha256", "text_sha256", "bundle_sha256", "report_sha256")

# ---------------------------------------------------------------- research

def _cli_env(env: Mapping[str, str]) -> dict[str, str]:
    out = {k: v for k, v in env.items()
           if k not in ("AGENT_LAB_CALM_DOWN_ROOT", "AGENT_LAB_CALM_DOWN_RECIPIENT")}
    out.update(AGENT_LAB_PROVIDER=PROVIDER, AGENT_LAB_CLAUDE_MODEL=MODEL, AGENT_LAB_CALM_DOWN_RETRIEVAL="live")
    return out


PR_SET_CHILD_SUBREAPER = 36
REAP_DEADLINE_SECONDS = 10.0


def _become_subreaper() -> None:
    """Make orphaned descendants (for example a Claude process the research CLI started in its own session)
    reparent to this runner instead of init, so they stay findable and killable until confirmed dead."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl")
    except (OSError, AttributeError):
        raise Refusal("subreaper_unavailable") from None


def _descendants(root_pid: int) -> list[int]:
    """Every live process below root_pid, by parent links in /proc (sessions and process groups ignored)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                stat_line = f.read().decode("ascii", "replace")
        except OSError:
            continue  # exited meanwhile
        fields = stat_line[stat_line.rfind(")") + 2:].split()
        children.setdefault(int(fields[1]), []).append(int(entry))
    found, todo = [], [root_pid]
    while todo:
        for pid in children.get(todo.pop(), []):
            found.append(pid)
            todo.append(pid)
    return found


def _kill_descendants() -> None:
    """SIGKILL and reap every descendant of this runner until none remain. Refuses (keeping the caller's
    lock held until it unwinds) if they cannot be confirmed dead within a bounded deadline."""
    me = os.getpid()
    deadline = time.monotonic() + REAP_DEADLINE_SECONDS
    while True:
        pids = _descendants(me)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for pid in pids:  # reparented orphans and zombies are now our children
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if time.monotonic() > deadline:
            raise Refusal("research_descendants_survived")
        time.sleep(0.05)


def _launch_cli(fear: str, out_dir: Path, cwd: Path, env: Mapping[str, str],
                lock_fd: int | None = None) -> tuple[int, str]:
    # Argument array, shell=False, fixed executable/subcommand, application-generated output path.
    # "--" makes a leading hyphen in the fear literal data.
    argv = [*CLI_ARGV, "calm-down", "--unreviewed", "--out", str(out_dir), "--", fear]
    proc = subprocess.Popen(argv, shell=False, cwd=cwd, env=_cli_env(env), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                            # The child inherits the run lock's open file description, so the lock outlives a
                            # runner that is killed outright, until the research process itself exits.
                            pass_fds=(lock_fd,) if lock_fd is not None else ())

    def kill() -> None:
        # Kill the CLI and its whole tree, including descendants in other sessions (Claude), and reap them,
        # before the caller releases the run lock.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        _kill_descendants()

    try:
        stdout, _ = proc.communicate(timeout=RESEARCH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        kill()
        raise Refusal("research_timeout") from None
    except BaseException:  # interrupted or terminated runner: never leave research running unserialized
        kill()
        raise
    _kill_descendants()  # anything the CLI left behind dies before the lock is released
    return proc.returncode, stdout.decode("utf-8", "replace")


def run_research(cfg: Config, fear_b64: str, env: Mapping[str, str], now: datetime | None = None) -> dict:
    fear = decode_fear(fear_b64)
    with _lock(cfg.state / "run.lock", blocking=False) as run_lock:  # one run at a time, before any reservation
        _become_subreaper()  # before reserving: refusal here starts nothing and consumes nothing
        eid = reserve_attempt(cfg, now)  # a crash from here on keeps the reservation
        try:
            exec_dir = cfg.executions / eid
            os.mkdir(exec_dir, 0o700)  # exclusive: an existing directory is an error
            _write_json_atomic(exec_dir / "execution.json", {"execution_id": eid})
            code, stdout = _launch_cli(fear, exec_dir, exec_dir, env, run_lock)
            if code != 0:
                raise Refusal("research_failed")
            try:
                summary = json.loads(stdout)
            except ValueError:
                raise Refusal("research_output_invalid") from None
            if not isinstance(summary, dict):
                raise Refusal("research_output_invalid")
            facts = validate_artifacts(cfg, eid, summary)
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, Refusal) else "runner_error"
            try:
                _transition(cfg, eid, "failed", reason=reason)
            except Refusal:
                pass
            raise
        _transition(cfg, eid, "ready", **{k: facts[k] for k in _BOUND})
        return {"execution_id": eid, "state": "ready"}


# ---------------------------------------------------------------- claim and acknowledge

def claim_delivery(cfg: Config, eid: str, now: datetime | None = None) -> dict:
    """Atomically move ready -> send_attempted, re-validating the artifacts, and only then hand the
    validated HTML to the caller. No code path returns an edition without having persisted the claim."""
    with _lock(cfg.state / "state.lock"):
        data = _load_state(cfg, eid)
        if data["state"] != "ready":
            raise Refusal(f"claim_refused_state_{data['state']}")
        if not _allowance_active(cfg):  # revocation applies to an already-ready edition too
            raise Refusal("allowance_not_active")
        try:
            facts = validate_artifacts(cfg, eid)
            if tuple(facts[k] for k in _BOUND) != tuple(data.get(k) for k in _BOUND):
                raise Refusal("artifact_changed")
        except Refusal as exc:
            data.update(state="invalid", reason=str(exc))
            _write_json_atomic(_state_path(cfg, eid), data)
            raise
        data.update(state="send_attempted", claimed_at=(now or datetime.now(timezone.utc)).isoformat())
        _write_json_atomic(_state_path(cfg, eid), data)
    return {"execution_id": eid, "to": cfg.recipient, "subject": SUBJECT, "html": facts["html"]}


def acknowledge(cfg: Config, eid: str, outcome: str) -> dict:
    if outcome not in ("sent", "unknown"):
        raise Refusal("outcome_invalid")
    _transition(cfg, eid, outcome)
    return {"execution_id": eid, "state": outcome}


# ---------------------------------------------------------------- command line

def _print(data: dict) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, now: datetime | None = None) -> int:
    import argparse
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="calm-down-delivery")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("reserve")
    run = sub.add_parser("run")
    run.add_argument("--fear-b64", required=True)
    for name in ("claim", "status"):
        sub.add_parser(name).add_argument("--execution-id", required=True)
    ack = sub.add_parser("ack")
    ack.add_argument("--execution-id", required=True)
    ack.add_argument("--outcome", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        if args.command == "init":
            init_state(env)
            _print({"initialized": True})
            return 0
        cfg = load_config(env)  # all configuration is validated before any reservation or research
        if args.command == "reserve":
            _print({"execution_id": reserve_attempt(cfg, now)})
        elif args.command == "run":
            _print(run_research(cfg, args.fear_b64, env, now))
        elif args.command == "claim":
            _print(claim_delivery(cfg, args.execution_id, now))
        elif args.command == "ack":
            _print(acknowledge(cfg, args.execution_id, args.outcome))
        else:
            _print({"execution_id": args.execution_id, "state": _load_state(cfg, args.execution_id)["state"]})
        return 0
    except Refusal as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    except OSError:
        sys.stderr.write("refused: local_io_error\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
