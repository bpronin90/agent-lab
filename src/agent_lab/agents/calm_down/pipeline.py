"""Calm Down as two separate actions.

1. ``run_calm_down``: research. A bounded model run through the standard ``runtime.run_agent``
   (same retries, shared usage budget, error classes and RunRecord as every agent), then mechanical
   verification. It never writes an email. It saves an immutable ``bundle.json`` (the dossier plus
   a snapshot of every retrieved source text) and, if the mechanical checks pass, a
   ``review-request.json`` for a person.
2. ``finalize_calm_down``: offline. It loads that exact bundle, checks the source snapshot's
   digests, applies the human approvals, and renders the email. It never calls a model or the network.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from agent_lab.agents.calm_down import AGENT_NAME
from agent_lab.agents.calm_down.render import render_dossier_md, render_email_html, render_email_text
from agent_lab.agents.calm_down.retrieval import RetrievalMode, SourceRecord, research_session
from agent_lab.agents.calm_down.schemas import CalmDownDossier
from agent_lab.agents.calm_down.verify import VerificationReport, review_request, verify_dossier
from agent_lab.config import Settings
from agent_lab.runtime import run_agent
from agent_lab.schemas import RunRecord

BUNDLE_FORMAT = "calm-down-bundle/1"

# A research run needs more room than the demo. These apply only when the caller did not set them.
RESEARCH_DEFAULTS = {
    "AGENT_LAB_MAX_REQUESTS": ("max_requests", 16),
    "AGENT_LAB_MAX_TOOL_CALLS": ("max_tool_calls", 30),
    "AGENT_LAB_MAX_OUTPUT_TOKENS": ("max_output_tokens", 8192),
    "AGENT_LAB_DEADLINE_SECONDS": ("deadline_seconds", 300),
}

# Research owns every generated file; finalize owns only the email outputs and LIMITATION.md.
RESEARCH_FILES = ("email.html", "email.txt", "research-dossier.md", "LIMITATION.md", "sources.json",
                  "review-request.json", "bundle.json")
FINALIZE_FILES = ("email.html", "email.txt", "LIMITATION.md")


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
    files["dossier"] = out_dir / "research-dossier.md"
    files["dossier"].write_text(render_dossier_md(dossier, report, sources, mode, meta), "utf-8")
    if report.ok:
        fixture = mode == "fixture"
        files["html"] = out_dir / "email.html"
        files["html"].write_text(render_email_html(dossier, report, sources, fixture), "utf-8")
        files["text"] = out_dir / "email.txt"
        files["text"].write_text(render_email_text(dossier, report, sources, fixture), "utf-8")
    else:
        files["limitation"] = _limitation(out_dir, report)


def run_calm_down(fear: str, settings: Settings, mode: RetrievalMode, out_dir: Path) -> CalmDownResult:
    """Research only. Produces a bundle for review, never an email."""
    if mode == "live" and settings.provider == "mock":
        raise ValueError("live retrieval with the mock provider would mix fixture content with live sources")
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_generated(out_dir)
    with research_session(mode) as session:
        record = run_agent(build_prompt(fear), settings, AGENT_NAME)
    sources = session.sources
    files: dict[str, Path] = {}
    meta = {"run_id": record.run_id, "status": record.status, "provider": record.provider,
            "served_model": record.served_model, "git_sha": record.git_sha, "config_hash": record.config_hash[:16],
            "requests": record.requests, "tool_calls": len(record.tool_calls), "searches": len(session.searches)}

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
        report = verify_dossier(dossier, session.read_sources(), None)
    if session.errors:
        report.problems.extend(f"retrieval error (non-fatal): {e}" for e in session.errors)

    if dossier is not None:
        bundle = {"format": BUNDLE_FORMAT, "fear": fear, "mode": mode, "run_meta": meta,
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
        {"mode": mode, "searches": session.searches, "sources": [s.provenance() for s in sources.values()]},
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
