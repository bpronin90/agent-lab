"""Calm Down as two separate actions.

1. ``run_calm_down``: research. A bounded model run through the standard ``runtime.run_agent``
   (same retries, shared usage budget, error classes and RunRecord as every agent), then mechanical
   verification. It never writes an email. It saves an immutable ``bundle.json`` (the dossier plus
   a snapshot of every retrieved source text) and, if the mechanical checks pass, a
   ``review-request.json`` for a person.
2. ``finalize_calm_down``: offline. It loads that exact bundle, checks the source snapshot's
   digests, applies the human approvals, and renders the email. It never calls a model or the network.

With ``AGENT_LAB_PROVIDER=claude-cli`` research instead runs two Claude CLI launches sharing one
``ClaudeCliContext`` (one deadline, one accounting): a query plan, application-controlled retrieval
through the same session tools, then dossier synthesis over the exact retrieved snapshots. Nothing is
retried, repaired or sent to another provider; any failure ends the run before the next phase.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Mapping

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_ai import ModelRetry

from agent_lab import claude_cli
from agent_lab.agents.calm_down import AGENT_NAME, PLAN_INSTRUCTIONS, SYNTHESIS_INSTRUCTIONS
from agent_lab.agents.calm_down.render import render_dossier_md, render_email_html, render_email_text
from agent_lab.agents.calm_down.retrieval import (
    MAX_QUERY_LENGTH, MAX_READS, MAX_SEARCHES, ResearchSession, RetrievalMode, SourceRecord,
    read_source, research_session, search_sources,
)
from agent_lab.agents.calm_down.schemas import CalmDownDossier
from agent_lab.agents.calm_down.verify import VerificationReport, review_request, verify_dossier
from agent_lab.config import CLAUDE_EFFORTS, Settings, live_env_path, load_env_file
from agent_lab.runtime import run_agent
from agent_lab.schemas import RunRecord, ToolCallRecord

BUNDLE_FORMAT = "calm-down-bundle/1"

# A research run needs more room than the demo. These apply only when the caller did not set them.
RESEARCH_DEFAULTS = {
    "AGENT_LAB_MAX_REQUESTS": ("max_requests", 16),
    "AGENT_LAB_MAX_TOOL_CALLS": ("max_tool_calls", 30),
    "AGENT_LAB_MAX_OUTPUT_TOKENS": ("max_output_tokens", 8192),
    "AGENT_LAB_DEADLINE_SECONDS": ("deadline_seconds", 300),
    # Gemini free tier: ~5 requests/minute. Pacing keeps a 10+ step research loop under it.
    "AGENT_LAB_MIN_REQUEST_INTERVAL_SECONDS": ("min_request_interval_seconds", 13.0),
}

# Research owns every generated file; finalize owns only the email outputs and LIMITATION.md.
RESEARCH_FILES = ("email.html", "email.txt", "research-dossier.md", "LIMITATION.md", "sources.json",
                  "review-request.json", "bundle.json", "mechanical-report.json")
FINALIZE_FILES = ("email.html", "email.txt", "LIMITATION.md", "mechanical-report.json")
MECHANICAL_REPORT_FORMAT = "calm-down-mechanical-report/1"


# Claude CLI research bounds. The model only proposes queries; the application decides what to read.
CLAUDE_MAX_QUERIES = 6
CLAUDE_READS_PER_QUERY = 2
CLAUDE_MAX_EVIDENCE_CHARS = 60_000
CLAUDE_LAUNCHES = 2  # planning + synthesis; there is never a third
assert CLAUDE_MAX_QUERIES <= MAX_SEARCHES and CLAUDE_MAX_QUERIES * CLAUDE_READS_PER_QUERY <= MAX_READS
claude_binary = "claude"


class PlannedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["supports_fear", "challenges_fear"]
    query: str = Field(min_length=3, max_length=MAX_QUERY_LENGTH)


class QueryPlan(BaseModel):
    """The planning phase's only output. Validated locally; it can name queries, nothing else."""

    model_config = ConfigDict(extra="forbid")
    queries: list[PlannedQuery] = Field(min_length=2, max_length=CLAUDE_MAX_QUERIES)

    @model_validator(mode="after")
    def _balanced(self) -> "QueryPlan":
        if {q.side for q in self.queries} != {"supports_fear", "challenges_fear"}:
            raise ValueError("the plan must cover both sides of the fear")
        if len({q.query.strip().lower() for q in self.queries}) != len(self.queries):
            raise ValueError("queries must be distinct")
        return self


# Command-level defaults for `agent-lab calm-down` only. They sit below the checkout's `.env.live`
# and the process environment, and never enable the launch gate (AGENT_LAB_ALLOW_CLAUDE_CLI).
RESEARCH_COMMAND_DEFAULTS = {"AGENT_LAB_PROVIDER": "claude-cli", "AGENT_LAB_CLAUDE_MODEL": "claude-sonnet-5-5"}


def resolve_research_env(process_env: Mapping[str, str] | None = None,
                         env_file: Path | None | str = "default") -> dict[str, str]:
    """The one configuration a research run uses: process environment (even empty values) over the
    checkout-root `.env.live` over command defaults. Returns a new dict; nothing is exported to
    os.environ, so no later command in the process sees it. CI and KUBERNETES_SERVICE_HOST count
    only when set in the process environment, never from the file."""
    process = dict(os.environ if process_env is None else process_env)
    path = live_env_path() if env_file == "default" else env_file
    merged = {**RESEARCH_COMMAND_DEFAULTS, **load_env_file(path), **process}
    for marker in claude_cli.REFUSE_IF_PRESENT:
        if marker not in process:
            merged.pop(marker, None)
    return merged


def research_settings(settings: Settings, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    overrides = {field: value for key, (field, value) in RESEARCH_DEFAULTS.items() if not env.get(key)}
    return dataclasses.replace(settings, **overrides)


@dataclass
class CalmDownResult:
    record: RunRecord | None  # None for an offline finalize
    mode: RetrievalMode
    report: VerificationReport | None
    dossier: CalmDownDossier | None
    files: dict[str, Path]

    @property
    def ok(self) -> bool:
        return self.report is not None and self.report.ok and (self.record is None or self.record.status == "succeeded")


def build_prompt(fear: str) -> str:
    return (f"The fear to investigate: {fear.strip()}\n"
            "Research it on both sides, verify, then write the Calm Down edition.")


def clear_generated(out_dir: Path, names: tuple[str, ...] = RESEARCH_FILES) -> None:
    """Remove only files this pipeline generates, so no stale email can survive a later run."""
    for name in names:
        path = out_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()


def fixture_review() -> dict:
    """The authoring agent's own, non-independent approval of the fixture preview's claims."""
    text = resources.files("agent_lab.agents.calm_down.fixtures").joinpath("fixture_review.json").read_text("utf-8")
    return json.loads(text)


def load_review(path: Path | None) -> dict | None:
    if path is None:
        return None
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("approvals", []), list):
        raise ValueError("review file must be a JSON object with a reviewer and an approvals list")
    return data


def _limitation(out_dir: Path, report: VerificationReport) -> Path:
    path = out_dir / "LIMITATION.md"
    if report.status == "needs_review":
        body = ("# Calm Down is waiting for human support review\n\nThe quotes were found in the retrieved sources, "
                "but a machine cannot prove that each claim fairly represents its evidence. No email was produced. "
                "Review `review-request.json`, then run `agent-lab calm-down-finalize <dir> --review review.json`. "
                "Finalizing renders this exact saved draft and does not rerun research.\n\nPending: "
                + ", ".join(report.pending_review) + "\n")
    else:
        body = ("# Calm Down could not finish reliable research\n\nNo reassurance email was produced, because "
                "producing one would mean making claims without verified evidence.\n\n"
                + "\n".join(f"- {p}" for p in report.problems) + "\n")
    path.write_text(body, "utf-8")
    return path


def _write_reports(out_dir: Path, dossier: CalmDownDossier, report: VerificationReport,
                   sources: dict[str, SourceRecord], mode: str, meta: dict, files: dict[str, Path]) -> None:
    unreviewed = report.unreviewed
    files["dossier"] = out_dir / "research-dossier.md"
    files["dossier"].write_text(render_dossier_md(dossier, report, sources, mode, meta, unreviewed), "utf-8")
    if report.ok:
        fixture = mode == "fixture"
        files["html"] = out_dir / "email.html"
        files["html"].write_text(render_email_html(dossier, report, sources, fixture, unreviewed), "utf-8")
        files["text"] = out_dir / "email.txt"
        files["text"].write_text(render_email_text(dossier, report, sources, fixture, unreviewed), "utf-8")
        if unreviewed:
            files["mechanical_report"] = out_dir / "mechanical-report.json"
            files["mechanical_report"].write_text(json.dumps(mechanical_report(
                report, mode, meta, files["html"], files["text"]), indent=2) + "\n", "utf-8")
    else:
        files["limitation"] = _limitation(out_dir, report)


def mechanical_report(report: VerificationReport, mode: str, meta: dict, html: Path, text: Path) -> dict:
    """Machine-readable outcome of the mechanical checks for an unreviewed edition, bound to the exact
    rendered bytes. It asserts that human review is ABSENT; it is not an approval of any kind."""
    return {"format": MECHANICAL_REPORT_FORMAT, "run_id": meta["run_id"], "run_status": meta["status"],
            "provider": meta["provider"], "retrieval": mode, "research_status": report.status,
            "human_review": "absent", "unreviewed": True, "reviewer": None,
            "verified_findings": len(report.verified), "distinct_works": report.distinct_works,
            "problems": list(report.problems),
            "email_html_sha256": hashlib.sha256(html.read_bytes()).hexdigest(),
            "email_txt_sha256": hashlib.sha256(text.read_bytes()).hexdigest()}


def _claude_policy(settings: Settings, mode: RetrievalMode) -> str:
    """Everything that shapes a Claude CLI run besides the context bounds, for its config_hash."""
    return json.dumps({
        "plan_instructions": PLAN_INSTRUCTIONS, "synthesis_instructions": SYNTHESIS_INSTRUCTIONS,
        "effort": settings.claude_effort,
        "plan_output_tokens": settings.claude_plan_output_tokens,
        "synthesis_output_tokens": settings.claude_synthesis_output_tokens,
        "launches": CLAUDE_LAUNCHES, "max_queries": CLAUDE_MAX_QUERIES, "reads_per_query": CLAUDE_READS_PER_QUERY,
        "max_evidence_chars": CLAUDE_MAX_EVIDENCE_CHARS, "deadline_seconds": settings.deadline_seconds,
        "retrieval": mode,
    }, sort_keys=True)


def _plan_prompt(fear: str) -> str:
    return (PLAN_INSTRUCTIONS + "\nReturn ONLY one JSON object, no prose, matching this JSON Schema:\n"
            + json.dumps(QueryPlan.model_json_schema()) + "\n\nThe fear to investigate: " + fear.strip() + "\n")


def _synthesis_prompt(fear: str, snapshots: list[str]) -> str:
    return (SYNTHESIS_INSTRUCTIONS + "\nReturn ONLY one JSON object, no prose, matching this JSON Schema:\n"
            + json.dumps(CalmDownDossier.model_json_schema()) + "\n\nThe fear to investigate: " + fear.strip()
            + "\n\nEVIDENCE (the only sources you may cite):\n\n" + "\n\n".join(snapshots) + "\n")


def _require_time(ctx: claude_cli.ClaudeCliContext) -> None:
    if ctx.remaining_seconds() <= 0:
        raise claude_cli.ClaudeCliError("limit", "deadline_expired")


def _within_deadline(ctx: claude_cli.ClaudeCliContext, op, *args):
    """Run one retrieval operation, waiting no longer than the shared deadline allows.

    Retrieval's own timeouts bound each network wait, not a slow stream, so the operation runs in a
    daemon thread bound to the current retrieval session. If the deadline passes first, the run fails
    and the abandoned operation is left to finish or die with the process; its result is never used.
    """
    _require_time(ctx)
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = op(*args)
        except BaseException as exc:  # handed back to the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=contextvars.copy_context().run, args=(target,), daemon=True)
    worker.start()
    worker.join(max(0.0, ctx.remaining_seconds()))
    if worker.is_alive():
        raise claude_cli.ClaudeCliError("limit", "deadline_expired")
    _require_time(ctx)
    if "error" in outcome:
        if isinstance(outcome["error"], ModelRetry):
            raise claude_cli.ClaudeCliError("tool", "retrieval_failed")
        raise outcome["error"]
    return outcome["value"]


def _retrieve(plan: QueryPlan, ctx: claude_cli.ClaudeCliContext, session: ResearchSession,
              calls: list[ToolCallRecord]) -> list[str]:
    """Run the plan's searches and bounded reads under the shared deadline.
    Returns the exact snapshots shown to synthesis."""
    snapshots: list[str] = []
    chars = 0
    covered: set[str] = set()
    for item in plan.queries:
        calls.append(ToolCallRecord(tool_name="search_sources", args={"query": item.query}))
        listing = _within_deadline(ctx, search_sources, item.query)
        if listing == "No results.":
            continue
        taken = 0
        for entry in json.loads(listing):
            rec = session.sources[entry["source_id"]]
            if taken >= CLAUDE_READS_PER_QUERY:
                break
            if rec.read:
                covered.add(item.side)  # already in the evidence from an earlier query
                continue
            if not rec.text or chars + len(rec.text) > CLAUDE_MAX_EVIDENCE_CHARS:
                continue
            calls.append(ToolCallRecord(tool_name="read_source", args={"source_id": rec.source_id}))
            snapshot = _within_deadline(ctx, read_source, rec.source_id)
            snapshots.append(snapshot)
            chars += len(rec.text)
            taken += 1
            covered.add(item.side)
    _require_time(ctx)
    if covered != {"supports_fear", "challenges_fear"}:
        # Synthesis never runs without evidence on both sides: no reassurance from nothing.
        raise claude_cli.ClaudeCliError("validation", "insufficient_evidence")
    return snapshots


def run_claude_research(fear: str, settings: Settings, mode: RetrievalMode, session: ResearchSession,
                        env: Mapping[str, str] | None = None) -> RunRecord:
    """Plan, retrieve, synthesize: exactly two CLI launches on success, one shared context."""
    refusal = None
    if not settings.claude_model:
        refusal = ("unset", "model_required: set AGENT_LAB_CLAUDE_MODEL explicitly")
    elif settings.claude_effort is not None and settings.claude_effort not in CLAUDE_EFFORTS:
        refusal = (settings.claude_model, "invalid_effort")
    if refusal:
        # Refused before any launch, but still recorded like every other run.
        now = datetime.now(timezone.utc)
        return RunRecord(
            run_id=str(uuid.uuid4()), agent_name=AGENT_NAME, provider=claude_cli.PROVIDER, model=refusal[0],
            prompt=build_prompt(fear)[:500], status="failed", started_at=now, finished_at=now,
            error=refusal[1], error_class="config", git_sha=settings.git_sha,
            config_hash=hashlib.sha256(_claude_policy(settings, mode).encode()).hexdigest())
    ctx = claude_cli.ClaudeCliContext(
        model=settings.claude_model, deadline=claude_cli._monotonic() + settings.deadline_seconds,
        max_requests=CLAUDE_LAUNCHES,
        max_output_tokens=max(settings.claude_plan_output_tokens, settings.claude_synthesis_output_tokens),
        binary=claude_binary, env=env, effort=settings.claude_effort,
    )
    calls: list[ToolCallRecord] = []

    def work(ctx: claude_cli.ClaudeCliContext) -> CalmDownDossier:
        plan = ctx.call(_plan_prompt(fear), QueryPlan, settings.claude_plan_output_tokens)
        snapshots = _retrieve(plan, ctx, session, calls)
        return ctx.call(_synthesis_prompt(fear, snapshots), CalmDownDossier, settings.claude_synthesis_output_tokens)

    record = claude_cli.run_with_record(
        ctx, work, agent_name=AGENT_NAME, prompt_descriptor=build_prompt(fear),
        instructions=_claude_policy(settings, mode), git_sha=settings.git_sha)
    return record.model_copy(update={"tool_calls": calls, "tools_used": sorted({c.tool_name for c in calls})})


def run_calm_down(fear: str, settings: Settings, mode: RetrievalMode, out_dir: Path,
                  env: Mapping[str, str] | None = None, unreviewed: bool = False) -> CalmDownResult:
    """Research. By default it produces a bundle for human review and never an email. With the explicit
    ``unreviewed=True`` opt-in it also renders a bannered, mechanically checked edition; only the human
    support gate is skipped and no approval or reviewer is ever recorded."""
    if mode == "live" and settings.provider == "mock":
        raise ValueError("live retrieval with the mock provider would mix fixture content with live sources")
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_generated(out_dir)
    with research_session(mode) as session:
        if settings.provider == claude_cli.PROVIDER:
            record = run_claude_research(fear, settings, mode, session, env)
        else:
            record = run_agent(build_prompt(fear), settings, AGENT_NAME)
    sources = dict(session.sources)  # one atomic copy: an abandoned retrieval thread may still add entries
    searches = list(session.searches)
    files: dict[str, Path] = {}
    meta = {"run_id": record.run_id, "status": record.status, "provider": record.provider,
            "served_model": record.served_model, "git_sha": record.git_sha, "config_hash": record.config_hash[:16],
            "requests": record.requests, "tool_calls": len(record.tool_calls), "searches": len(searches)}

    dossier = None
    if record.status == "succeeded" and record.output is not None:
        try:
            dossier = CalmDownDossier.model_validate(record.output)
        except ValidationError as exc:  # defensive: runtime already validated
            report = VerificationReport(status="insufficient", problems=[f"output failed validation: {exc}"])
    else:
        report = VerificationReport(status="insufficient",
                                    problems=[f"research run failed ({record.error_class}): {record.error}"])
    if dossier is not None:
        # No approvals are ever applied here: approval belongs to finalize, on the saved bundle.
        report = verify_dossier(dossier, session.read_sources(), None, unreviewed)
    if session.errors:
        report.problems.extend(f"retrieval error (non-fatal): {e}" for e in session.errors)

    if dossier is not None:
        bundle = {"format": BUNDLE_FORMAT, "fear": fear, "mode": mode, "run_meta": meta,
                  **({"human_review": "absent"} if unreviewed else {}),
                  "dossier": dossier.model_dump(mode="json"),
                  "sources": [dict(s.provenance(), text=s.text) for s in sources.values()]}
        files["bundle"] = out_dir / "bundle.json"
        files["bundle"].write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", "utf-8")
        _write_reports(out_dir, dossier, report, sources, mode, meta, files)
        if report.status == "needs_review":
            files["review_request"] = out_dir / "review-request.json"
            files["review_request"].write_text(
                json.dumps(review_request(dossier, report, session.read_sources()), indent=2, ensure_ascii=False)
                + "\n", "utf-8")
    else:
        files["limitation"] = _limitation(out_dir, report)
    files["provenance"] = out_dir / "sources.json"
    files["provenance"].write_text(json.dumps(
        {"mode": mode, "searches": searches, "sources": [s.provenance() for s in sources.values()]},
        indent=2), "utf-8")
    return CalmDownResult(record=record, mode=mode, report=report, dossier=dossier, files=files)


def load_bundle(out_dir: Path) -> tuple[dict, CalmDownDossier, dict[str, SourceRecord]]:
    path = out_dir / "bundle.json"
    if not path.is_file():
        raise ValueError(f"no bundle.json in {out_dir}; run `agent-lab calm-down` first")
    bundle = json.loads(path.read_text("utf-8"))
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT or bundle.get("mode") not in ("fixture", "live"):
        raise ValueError("unsupported or corrupt bundle.json")
    dossier = CalmDownDossier.model_validate(bundle["dossier"])
    fields = {f.name for f in dataclasses.fields(SourceRecord)}
    sources: dict[str, SourceRecord] = {}
    for raw in bundle["sources"]:
        rec = SourceRecord(**{k: v for k, v in raw.items() if k in fields})
        if rec.read and rec.text_sha256 != hashlib.sha256(rec.text.encode()).hexdigest():
            raise ValueError(f"source {rec.source_id} text does not match its recorded digest; bundle was altered")
        sources[rec.source_id] = rec
    return bundle, dossier, sources


def invalidate_outputs(out_dir: Path, reason: str) -> None:
    """Fail closed on a refused finalize: remove any earlier email and say why.
    The saved bundle and review request are kept so the person can fix and retry."""
    if not out_dir.is_dir():
        return
    clear_generated(out_dir, FINALIZE_FILES)
    (out_dir / "LIMITATION.md").write_text(
        "# Calm Down finalization was refused\n\nNo email was produced. Any earlier email in this directory "
        f"was removed.\n\n- {reason}\n", "utf-8")


def finalize_calm_down(out_dir: Path, review: dict) -> CalmDownResult:
    """Offline: apply human approvals to the saved bundle and render it. Never calls a model.
    Earlier email outputs are invalidated BEFORE anything can fail."""
    if out_dir.is_dir():
        clear_generated(out_dir, FINALIZE_FILES)
    try:
        bundle, dossier, sources = load_bundle(out_dir)
        mode: RetrievalMode = bundle["mode"]
        if mode == "live" and review.get("scope") == "fixture":
            raise ValueError("a fixture-scoped review cannot approve a live edition; a person must review it")
    except (ValueError, OSError, KeyError, TypeError, ValidationError) as exc:
        invalidate_outputs(out_dir, f"{type(exc).__name__}: {exc}")
        raise ValueError(str(exc)) from exc
    read = {k: v for k, v in sources.items() if v.read}
    report = verify_dossier(dossier, read, review)
    files: dict[str, Path] = {}
    _write_reports(out_dir, dossier, report, sources, mode, dict(bundle["run_meta"], finalized="offline"), files)
    return CalmDownResult(record=None, mode=mode, report=report, dossier=dossier, files=files)
