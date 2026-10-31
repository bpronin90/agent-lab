"""Offline tests for the Calm Down agent. They validate the pipeline, not live research."""

import hashlib
import json
import re

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_lab import runtime
from agent_lab.agents import REGISTRY
from agent_lab.agents.calm_down import retrieval
from agent_lab.agents.calm_down.mock_script import load_mock_dossier
from agent_lab.agents.calm_down.pipeline import (
    finalize_calm_down, fixture_review, load_bundle, research_settings, run_calm_down,
)
from agent_lab.agents.calm_down.render import render_email_html
from agent_lab.agents.calm_down.retrieval import (
    RetrievalError, SourceRecord, check_url, html_to_text, mode_from_env, research_session, safe_get,
)
from agent_lab.agents.calm_down.schemas import CalmDownDossier
from agent_lab.agents.calm_down.verify import finding_hash, normalize, review_request, verify_dossier
from agent_lab.cli import main
from agent_lab.config import Settings

FEAR = "AI will replace lawyers and accountants"
OFFLINE = research_settings(Settings.from_env({}), env={})


def read_all_fixture_sources():
    corpus = retrieval.load_fixture_corpus()
    return {s["source_id"]: SourceRecord(source_id=s["source_id"], title=s["title"], url=s["url"],
                                         publisher=s["publisher"], year=s["year"], tier=s["tier"],
                                         backend="fixture", text=s["text"], read=True)
            for s in corpus["sources"]}


def research_and_finalize(out, fear=FEAR):
    research = run_calm_down(fear, OFFLINE, "fixture", out)
    assert research.report.status == "needs_review" and not (out / "email.html").exists()
    return finalize_calm_down(out, fixture_review())


def dossier(**changes):
    data = load_mock_dossier()
    data.update(changes)
    return CalmDownDossier.model_validate(data)


# ---------------------------------------------------------------- end to end (fixture)

def test_fixture_pipeline_produces_labeled_artifacts(tmp_path):
    research = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert research.record.agent_name == "calm_down"
    assert set(research.record.tools_used) == {"search_sources", "read_source"}
    result = finalize_calm_down(tmp_path, fixture_review())
    assert result.ok, result.report.problems
    html = (tmp_path / "email.html").read_text()
    text = (tmp_path / "email.txt").read_text()
    md = (tmp_path / "research-dossier.md").read_text()
    assert "FIXTURE PREVIEW" in html and "FIXTURE PREVIEW" in text and "FIXTURE PREVIEW" in md
    for needle in ("CALM DOWN.", "Doom Meter", "What the Doomers Get Right", "Reasons to Chill", "THE VERDICT"):
        assert needle in html
    provenance = json.loads((tmp_path / "sources.json").read_text())
    assert provenance["mode"] == "fixture" and all(s["text_sha256"] for s in provenance["sources"] if s["read"])


def test_email_html_is_email_safe(tmp_path):
    research_and_finalize(tmp_path)
    html = (tmp_path / "email.html").read_text()
    assert "<script" not in html.lower() and "<img" not in html.lower() and "javascript:" not in html.lower()
    assert "<link" not in html.lower() and "@import" not in html
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs and all(h.startswith("https://") for h in hrefs)
    corpus_urls = {s["url"] for s in retrieval.load_fixture_corpus()["sources"]}
    assert set(hrefs) <= {u.replace("&", "&amp;") for u in corpus_urls}


def test_every_rendered_citation_was_read_and_verified(tmp_path):
    result = research_and_finalize(tmp_path)
    verified_sources = {sid for f in result.report.verified for sid in f.source_ids}
    provenance = json.loads((tmp_path / "sources.json").read_text())
    read = {s["source_id"] for s in provenance["sources"] if s["read"]}
    assert verified_sources <= read


def test_html_escapes_model_text():
    d = dossier(opening="Cousin <script>alert(x)</script> & friends, calm down please now.")
    review = fixture_review()
    review["approvals"].append(
        review_request(d, verify_dossier(d, read_all_fixture_sources()), read_all_fixture_sources())["editorial"]["hash"])
    report = verify_dossier(d, read_all_fixture_sources(), review)
    assert report.ok, report.problems
    html = render_email_html(d, report, read_all_fixture_sources(), fixture=True)
    assert "<script>alert" not in html and "&lt;script&gt;" in html


# ---------------------------------------------------------------- verification rejections

def test_fabricated_quote_is_rejected():
    data = load_mock_dossier()
    data["findings"][3]["evidence_quote"] = "AI chatbots have increased accountant earnings dramatically"
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F4" and "verbatim" in r.reason for r in report.rejected)
    assert not report.ok  # F4 is referenced by reasons_to_chill


def test_unread_source_is_rejected():
    sources = read_all_fixture_sources()
    sources["S6"].read = False
    read = {k: v for k, v in sources.items() if v.read}
    report = verify_dossier(dossier(), read)
    assert any(r.item == "F6" and "not read" in r.reason for r in report.rejected)


def test_unknown_source_id_is_rejected():
    data = load_mock_dossier()
    data["findings"][0]["source_ids"] = ["S99"]
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F1" for r in report.rejected)


def test_claim_cannot_add_statistics():
    data = load_mock_dossier()
    data["findings"][4]["claim"] = "Lawyers were fined because 97 percent of AI citations are fake."
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F5" and "97" in r.reason for r in report.rejected)


@pytest.mark.parametrize("field,value,needle", [
    ("verdict", "Lawyers are safe and their jobs will never be automated by anything at all.", "guarantee"),
    ("verdict", "Relax: 99% of lawyers keep their jobs, which is plenty of good news for everyone.", "99"),
    ("opening", "Read https://example.com before panicking about robots taking your law degree.", "URL"),
])
def test_editorial_honesty_rules(field, value, needle):
    report = verify_dossier(dossier(**{field: value}), read_all_fixture_sources())
    assert not report.ok and any(needle in p for p in report.problems)


def test_one_sided_research_is_refused():
    data = load_mock_dossier()
    data["findings"] = [f for f in data["findings"] if f["stance"] == "challenges_fear"]
    data["doomers_get_right"] = []
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert not report.ok and any("one-sided" in p for p in report.problems)


def test_too_few_findings_is_insufficient():
    data = load_mock_dossier()
    data["findings"] = data["findings"][:2]
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert not report.ok


def test_agent_reported_insufficient_blocks_email():
    report = verify_dossier(dossier(status="insufficient"), read_all_fixture_sources())
    assert not report.ok
    with pytest.raises(ValueError):
        render_email_html(dossier(status="insufficient"), report, read_all_fixture_sources(), fixture=True)


def test_normalize_handles_typography():
    assert normalize("“Smart” quotes — and\n spaces.") == normalize('"smart" quotes - and spaces')


# ---------------------------------------------------------------- failure handling end to end

def test_failed_run_writes_limitation_not_email(tmp_path, monkeypatch):
    def broken(messages, info):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(runtime, "build_model", lambda s, m=None: FunctionModel(broken, model_name="x"))
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert result.record.status == "failed" and result.record.error_class == "transient"
    assert not (tmp_path / "email.html").exists() and (tmp_path / "LIMITATION.md").exists()


def test_model_citing_unread_sources_gets_no_email(tmp_path, monkeypatch):
    """A model that skips reading and cites from 'memory' is blocked."""
    def lazy(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, load_mock_dossier())])

    monkeypatch.setattr(runtime, "build_model", lambda s, m=None: FunctionModel(lazy, model_name="lazy"))
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert result.record.status == "succeeded" and not result.ok
    assert not (tmp_path / "email.html").exists()
    assert "not read" in (tmp_path / "research-dossier.md").read_text()


def test_cli_calm_down_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LAB_RUN_LOG_DIR", str(tmp_path / "runs"))
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "ok")]) == 5  # research stops at human review
    assert not (tmp_path / "ok" / "email.html").exists()
    assert (tmp_path / "ok" / "review-request.json").exists() and (tmp_path / "ok" / "bundle.json").exists()
    assert main(["calm-down-finalize", str(tmp_path / "ok"), "--review", "fixture"]) == 0
    assert (tmp_path / "ok" / "email.html").exists()
    assert main(["calm-down-finalize", str(tmp_path / "missing"), "--review", "fixture"]) == 2
    assert (tmp_path / "runs" / "runs.jsonl").exists()
    monkeypatch.setenv("AGENT_LAB_CALM_DOWN_RETRIEVAL", "live")
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "x")]) == 2  # live + mock refused
    monkeypatch.setenv("AGENT_LAB_CALM_DOWN_RETRIEVAL", "bogus")
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "x")]) == 2


# ---------------------------------------------------------------- tools and retrieval safety

def test_tool_budgets_and_unknown_ids():
    from pydantic_ai import ModelRetry
    with research_session("fixture") as session:
        with pytest.raises(ModelRetry):
            retrieval.read_source("S1")  # not listed yet
        for i in range(retrieval.MAX_SEARCHES):
            retrieval.search_sources(f"lawyers {i}")
        with pytest.raises(ModelRetry):
            retrieval.search_sources("one too many")
        with pytest.raises(ModelRetry):
            retrieval.fetch_url("https://evil.example/")
        assert "UNTRUSTED SOURCE TEXT" in retrieval.read_source("S5")
        assert session.read_sources().keys() == {"S5"}


def test_mode_from_env():
    assert mode_from_env({}) == "fixture"
    assert mode_from_env({"AGENT_LAB_CALM_DOWN_RETRIEVAL": "LIVE"}) == "live"
    with pytest.raises(ValueError):
        mode_from_env({"AGENT_LAB_CALM_DOWN_RETRIEVAL": "web"})


@pytest.mark.parametrize("url", [
    "http://www.nber.org/papers/w1",          # not https
    "https://evil.example/",                  # not allowlisted
    "https://nber.org.evil.example/",         # suffix trick
    "https://user:pw@www.nber.org/",          # credentials
    "https://www.nber.org:8443/",             # odd port
    "file:///etc/passwd",
])
def test_check_url_refuses_unsafe(url):
    with pytest.raises(RetrievalError):
        check_url(url, resolve=False)


def test_check_url_allows_subdomains_and_rejects_private_ip(monkeypatch):
    assert check_url("https://export.arxiv.org/abs/1", resolve=False) == "primary"
    monkeypatch.setattr(retrieval.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    with pytest.raises(RetrievalError, match="non-public"):
        check_url("https://www.nber.org/", resolve=True)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_off_allowlist_is_refused():
    client = _mock_client(lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    with pytest.raises(RetrievalError, match="allowlist"):
        safe_get("https://www.nber.org/papers/w1", client=client, resolve=False)


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_FETCH_BYTES", 100)
    client = _mock_client(lambda r: httpx.Response(200, content=b"x" * 500))
    with pytest.raises(RetrievalError, match="size cap"):
        safe_get("https://www.nber.org/papers/w1", client=client, resolve=False)


def test_html_to_text_drops_scripts():
    title, text = html_to_text("<title>T</title><script>steal()</script><p>Hello <b>world</b></p>")
    assert title == "T" and "steal" not in text and "Hello world" in text


def test_openalex_abstract_reconstruction():
    assert retrieval._abstract({"world": [1], "hello": [0]}) == "hello world"


# ---------------------------------------------------------------- runtime compatibility

def test_registry_keeps_demo_and_adds_calm_down():
    assert {"demo", "calm_down"} <= set(REGISTRY)
    assert REGISTRY["calm_down"].thinking_budget < OFFLINE.max_output_tokens


def test_research_settings_respect_explicit_env():
    s = research_settings(Settings.from_env({}), env={"AGENT_LAB_MAX_REQUESTS": "5"})
    assert s.max_requests == Settings().max_requests and s.max_tool_calls == 30


# ---------------------------------------------------------------- review regressions (PR #10)

def verify_mutated(mutate, review=True):
    data = load_mock_dossier()
    mutate(data)
    return verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources(),
                          fixture_review() if review else None)


def finding(data, fid):
    return next(f for f in data["findings"] if f["id"] == fid)


def test_reversed_claim_is_rejected():
    report = verify_mutated(lambda d: finding(d, "F4").update(
        claim="AI chatbots significantly increased earnings and recorded hours in every occupation."))
    assert any(r.item == "F4" and "negation" in r.reason for r in report.rejected)
    assert not report.ok


def test_reversed_claim_without_negation_cue_still_needs_human_approval():
    # Mechanical checks cannot catch every distortion, so a changed claim invalidates its approval.
    report = verify_mutated(lambda d: finding(d, "F8").update(
        claim="In a large workplace study, a generative AI assistant raised productivity, mostly for senior experts."))
    assert report.status == "needs_review" and "F8" in report.pending_review


def test_unrelated_reused_statistic_in_verdict_is_rejected():
    report = verify_mutated(lambda d: d.update(
        verdict="Research proves that 88% of accountants are safe from job loss caused by AI."))
    assert not report.ok
    assert any("verdict" in p and "88" in p for p in report.problems)
    assert any("verdict" in p and "guarantee" in p for p in report.problems)


@pytest.mark.parametrize("where", ["caveat", "uncertainty", "limitation"])
def test_every_rendered_field_is_checked(where):
    payload = "99% of lawyers are safe forever. Read https://evil.example for details."

    def mutate(d):
        if where == "caveat":
            finding(d, "F1")["caveat"] = payload
        elif where == "uncertainty":
            d["uncertainties"][0] = payload
        else:
            d["limitations"][0] = payload

    report = verify_mutated(mutate)
    assert not report.ok
    joined = " ".join(report.problems)
    assert "URL" in joined and "guarantee" in joined and "99" in joined


def test_caveat_may_reuse_its_own_quote_numbers_only():
    ok = verify_mutated(lambda d: finding(d, "F2").update(caveat="The 13 percent figure is relative, not absolute."))
    assert not any("F2.caveat" in p for p in ok.problems)
    bad = verify_mutated(lambda d: finding(d, "F2").update(caveat="The 88 percent figure is relative."))
    assert any("F2.caveat" in p for p in bad.problems)


def test_padded_citations_are_rejected_and_do_not_count():
    quote = "The analysis measures exposure of tasks, not whether jobs will be eliminated"

    def mutate(d):
        for f in d["findings"]:
            f.update(source_ids=["S1", "S2", "S3"], evidence_quote=quote,
                     claim="The analysis measures task exposure and not whether jobs will be eliminated.")

    report = verify_mutated(mutate)
    assert report.verified == [] and all("unsupported citation" in r.reason for r in report.rejected)
    assert not report.ok


def test_distinct_works_count_urls_not_ids():
    sources = read_all_fixture_sources()
    for sid in ("S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12"):
        sources[sid].url = "https://www.nber.org/papers/same"
    report = verify_dossier(dossier(), sources, fixture_review())
    assert report.distinct_works == 2 and not report.ok


def test_opposing_sections_must_hold_their_stance():
    report = verify_mutated(lambda d: d.update(doomers_get_right=["F4"], reasons_to_chill=["F4"]))
    assert not report.ok
    joined = " ".join(report.problems)
    assert "wrong stance" in joined and "both sides" in joined and "supports_fear" in joined


def test_needs_review_without_approvals():
    report = verify_mutated(lambda d: None, review=False)
    assert report.status == "needs_review" and "editorial" in report.pending_review
    with pytest.raises(ValueError):
        render_email_html(dossier(), report, read_all_fixture_sources(), fixture=True)


def test_fixture_review_refused_for_live_bundle(tmp_path):
    run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    bundle["mode"] = "live"
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match="fixture-scoped"):
        finalize_calm_down(tmp_path, fixture_review())


# ---------------------------------------------------------------- stale artifacts

def _fail_run(monkeypatch):
    def broken(messages, info):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(runtime, "build_model", lambda s, m=None: FunctionModel(broken, model_name="x"))
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)


def test_success_then_failure_leaves_no_stale_email(tmp_path, monkeypatch):
    assert research_and_finalize(tmp_path).ok
    assert (tmp_path / "email.html").exists()
    _fail_run(monkeypatch)
    result = run_calm_down("AI will replace dentists", OFFLINE, "fixture", tmp_path)
    assert not result.ok
    for name in ("email.html", "email.txt", "research-dossier.md", "bundle.json", "review-request.json"):
        assert not (tmp_path / name).exists()
    assert (tmp_path / "LIMITATION.md").exists()


def test_failure_then_success_leaves_no_stale_limitation(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        _fail_run(m)
        run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert (tmp_path / "LIMITATION.md").exists()
    assert research_and_finalize(tmp_path).ok
    assert not (tmp_path / "LIMITATION.md").exists() and (tmp_path / "email.html").exists()


def test_unrelated_files_in_output_dir_are_kept(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    research_and_finalize(tmp_path)
    assert (tmp_path / "notes.txt").read_text() == "mine"


# ---------------------------------------------------------------- second review (head 3afe0dd)

def test_finalize_renders_reviewed_bundle_without_calling_the_model(tmp_path, monkeypatch):
    run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    request = json.loads((tmp_path / "review-request.json").read_text())
    review = {"reviewer": "test person", "approvals": [f["hash"] for f in request["findings"]]
              + [request["editorial"]["hash"]]}

    def forbidden(*a, **k):
        raise AssertionError("finalize must not call the research model or runtime")

    monkeypatch.setattr(runtime, "run_agent", forbidden)
    monkeypatch.setattr(runtime, "build_model", forbidden)
    monkeypatch.setattr("agent_lab.agents.calm_down.pipeline.run_agent", forbidden)
    monkeypatch.setattr(retrieval, "safe_get", forbidden)
    result = finalize_calm_down(tmp_path, review)
    assert result.ok and result.record is None
    assert "test person" in (tmp_path / "email.html").read_text()
    # the review request and bundle the person approved are untouched
    assert json.loads((tmp_path / "review-request.json").read_text()) == request


def test_review_request_exposes_confidence_sources_and_presentation(tmp_path):
    run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    request = json.loads((tmp_path / "review-request.json").read_text())
    first = request["findings"][0]
    assert {"confidence", "surprising", "sources"} <= set(first)
    assert {"url", "text_sha256"} <= set(first["sources"][0])
    assert {"doom_meter_score", "doomers_get_right", "reasons_to_chill", "advice_evidence"} <= set(
        request["editorial"]["presentation"])


def _approved(tmp_path):
    run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    request = json.loads((tmp_path / "review-request.json").read_text())
    return {"reviewer": "test person",
            "approvals": [f["hash"] for f in request["findings"]] + [request["editorial"]["hash"]]}


def test_changed_confidence_invalidates_approval(tmp_path):
    review = _approved(tmp_path)
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    next(f for f in bundle["dossier"]["findings"] if f["id"] == "F3")["confidence"] = "high"
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    result = finalize_calm_down(tmp_path, review)
    assert result.report.status == "needs_review" and "F3" in result.report.pending_review
    assert not (tmp_path / "email.html").exists()


def test_changed_source_snapshot_invalidates_approval(tmp_path):
    review = _approved(tmp_path)
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    s3 = next(s for s in bundle["sources"] if s["source_id"] == "S3")
    s3["text"] += " Correction: these earnings estimates have been withdrawn because the records were invalid."
    s3["text_sha256"] = hashlib.sha256(s3["text"].encode()).hexdigest()  # a consistent, re-digested snapshot
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    result = finalize_calm_down(tmp_path, review)
    assert result.report.status == "needs_review" and "F4" in result.report.pending_review
    assert not (tmp_path / "email.html").exists()


def test_tampered_snapshot_without_digest_update_is_refused(tmp_path):
    review = _approved(tmp_path)
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    next(s for s in bundle["sources"] if s["source_id"] == "S3")["text"] += " edited"
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match="digest"):
        finalize_calm_down(tmp_path, review)


def test_finding_hash_depends_on_source_url_and_text():
    sources = read_all_fixture_sources()
    f = dossier().findings[0]
    before = finding_hash(f, sources)
    sources["S1"].url = "https://arxiv.org/abs/2303.10130v9"
    assert finding_hash(f, sources) != before


def test_bundle_round_trip_matches_dossier(tmp_path):
    research = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    _, loaded, sources = load_bundle(tmp_path)
    assert loaded == research.dossier and all(s.read for s in sources.values() if s.text_sha256)
