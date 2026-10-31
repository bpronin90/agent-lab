"""Deterministic rendering of a VERIFIED dossier into an email, plain text, and a research dossier.

Only verified findings reach the email. All text is HTML-escaped; links are https only.
The HTML uses tables and inline styles for email clients, has no JavaScript, no tracking,
and no remote images. One <style> block carries a mobile media query and is optional.
"""

from __future__ import annotations

import textwrap
from html import escape

from agent_lab.agents.calm_down.retrieval import SourceRecord
from agent_lab.agents.calm_down.schemas import CalmDownDossier, Finding
from agent_lab.agents.calm_down.verify import VerificationReport

KIND_LABEL = {"demonstrated_fact": "MEASURED", "forecast": "FORECAST", "speculation": "SPECULATION"}
KIND_COLOR = {"demonstrated_fact": "#1f7a4d", "forecast": "#b8860b", "speculation": "#8a3ffc"}
STANCE_LABEL = {"supports_fear": "point for the robots", "challenges_fear": "point for the humans", "mixed": "it's complicated"}
METER_COLORS = ["#2bb673", "#55c26a", "#8cc63f", "#c3d135", "#f2d43d", "#f7b733", "#f7941d", "#f15a29", "#e8352b", "#b5121b"]

INK, PAPER, CARD, PINK, TEAL, MUSTARD = "#1d1a2f", "#fff7e8", "#ffffff", "#ff4f81", "#13a89e", "#ffc845"
FONT = "Georgia, 'Times New Roman', serif"
SANS = "'Helvetica Neue', Helvetica, Arial, sans-serif"


class Citations:
    """Number sources in order of first citation."""

    def __init__(self, sources: dict[str, SourceRecord]):
        self.sources = sources
        self.order: list[str] = []

    def number(self, sid: str) -> int:
        if sid not in self.order:
            self.order.append(sid)
        return self.order.index(sid) + 1

    def html(self, finding: Finding) -> str:
        links = []
        for sid in finding.source_ids:
            n, url = self.number(sid), escape(self.sources[sid].url, quote=True)
            links.append(f'<a href="{url}" style="color:{PINK};font-weight:bold;text-decoration:none;">[{n}]</a>')
        return "<sup>" + "".join(links) + "</sup>"

    def text(self, finding: Finding) -> str:
        return "".join(f"[{self.number(sid)}]" for sid in finding.source_ids)


def _e(text: str) -> str:
    return escape(text, quote=True)


def _section(title: str, emoji: str, color: str, body: str, kicker: str = "") -> str:
    kick = f'<div style="font:bold 12px/1.4 {SANS};letter-spacing:2px;text-transform:uppercase;color:{INK};opacity:.7;">{_e(kicker)}</div>' if kicker else ""
    return f"""
<tr><td class="pad" style="padding:28px 32px 8px 32px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="background:{color};border:3px solid {INK};border-radius:14px;padding:12px 18px;box-shadow:6px 6px 0 {INK};">
      {kick}<div style="font:bold 26px/1.15 {SANS};color:{INK};">{emoji}&nbsp;{_e(title)}</div>
    </td></tr></table>
</td></tr>
<tr><td class="pad" style="padding:8px 32px 8px 32px;">{body}</td></tr>"""


def _meter(score: int) -> str:
    cells = "".join(
        f'<td width="10%" style="height:26px;background:{METER_COLORS[i] if i < score else "#ece4d3"};'
        f'border:2px solid {INK};font:bold 11px {SANS};text-align:center;color:{INK};">{i + 1}</td>'
        for i in range(10)
    )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="3" border="0"><tr>{cells}</tr></table>'


def _finding_card(f: Finding, cites: Citations, accent: str) -> str:
    analogy = (f'<div style="margin-top:10px;padding:10px 12px;background:#fff1c9;border-left:5px solid {MUSTARD};'
               f'font:italic 15px/1.5 {FONT};color:{INK};">🍝 <b>Translation:</b> {_e(f.analogy)}</div>') if f.analogy else ""
    surprise = f'<span style="font:bold 11px {SANS};color:{PINK};letter-spacing:1px;">&nbsp;★ SURPRISING</span>' if f.surprising else ""
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 16px 0;"><tr>
<td style="background:{CARD};border:2px solid {INK};border-top:8px solid {accent};border-radius:10px;padding:16px 18px;">
  <div><span style="display:inline-block;background:{KIND_COLOR[f.evidence_kind]};color:#fff;font:bold 11px {SANS};letter-spacing:1px;padding:3px 8px;border-radius:20px;">{KIND_LABEL[f.evidence_kind]}</span>
  <span style="font:11px {SANS};color:#6b6580;text-transform:uppercase;letter-spacing:1px;">&nbsp;{STANCE_LABEL[f.stance]} · confidence: {f.confidence}</span>{surprise}</div>
  <div style="margin-top:8px;font:bold 18px/1.4 {FONT};color:{INK};">{_e(f.claim)}{cites.html(f)}</div>
  <div style="margin-top:8px;font:15px/1.5 {FONT};color:#3d3852;">“{_e(f.evidence_quote)}”</div>
  <div style="margin-top:8px;font:13px/1.5 {SANS};color:#6b6580;"><b>Fine print:</b> {_e(f.caveat)}</div>
  {analogy}
</td></tr></table>"""


def _fact_list(findings: list[Finding], cites: Citations) -> str:
    rows = "".join(
        f'<tr><td valign="top" style="font:900 30px/1 {SANS};color:{PINK};padding:8px 12px 8px 0;">{n}</td>'
        f'<td style="font:17px/1.45 {FONT};color:{INK};padding:8px 0;border-bottom:2px dotted #d9cfb8;">'
        f'<span style="display:inline-block;background:{KIND_COLOR[f.evidence_kind]};color:#fff;font:bold 10px {SANS};'
        f'letter-spacing:1px;padding:2px 6px;border-radius:20px;">{KIND_LABEL[f.evidence_kind]}</span> '
        f'{_e(f.claim)}{cites.html(f)}</td></tr>'
        for n, f in enumerate(findings, 1))
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'


def render_email_html(dossier: CalmDownDossier, report: VerificationReport, sources: dict[str, SourceRecord],
                      fixture: bool) -> str:
    if not report.ok:
        raise ValueError("refusing to render a reassurance email from insufficient research")
    by_id = {f.id: f for f in report.verified}
    cites = Citations(sources)
    banner = (f'<tr><td style="background:{INK};color:{MUSTARD};font:bold 12px/1.5 {SANS};padding:10px 20px;text-align:center;">'
              "⚠ FIXTURE PREVIEW: layout test built from hand-written offline excerpts. "
              "Not live research, not verified quotations. Do not send.</td></tr>") if fixture else ""
    surprising = [f for f in report.verified if f.surprising] or report.verified[:3]

    meter = f"""
<div style="font:15px/1.6 {FONT};color:{INK};">{_meter(dossier.doom_meter.score)}
<div style="margin-top:12px;font:bold 34px/1 {SANS};color:{INK};">{dossier.doom_meter.score}/10 · <span style="color:{PINK};">{_e(dossier.doom_meter.label)}</span></div>
<div style="margin-top:8px;">{_e(dossier.doom_meter.rationale)}</div>
<div style="margin-top:6px;font:italic 12px {SANS};color:#6b6580;">Highly subjective. Calibrated by vibes and the findings below. Not a forecast. Not a peer-reviewed instrument. Do not cite in court.</div></div>"""

    sections = [
        _section("The Doom Meter™", "📟", MUSTARD, meter, "a deeply unscientific instrument"),
        _section("Things the Research Actually Says", "🔬", "#9be7e0", _fact_list(surprising, cites),
                 "surprising findings, real footnotes (details below)"),
        _section("What the Doomers Get Right", "😬", "#ffb3c7",
                 "".join(_finding_card(by_id[i], cites, PINK) for i in dossier.doomers_get_right if i in by_id),
                 "steelmanning the apocalypse"),
        _section("Reasons to Chill", "🧊", "#b7f0c8",
                 "".join(_finding_card(by_id[i], cites, TEAL) for i in dossier.reasons_to_chill if i in by_id),
                 "evidence, not vibes"),
    ]
    if dossier.adapt:
        items = "".join(
            f'<tr><td valign="top" style="font:bold 20px {SANS};color:{PINK};padding:6px 10px 6px 0;">→</td>'
            f'<td style="font:16px/1.5 {FONT};color:{INK};padding:6px 0;">{_e(a.text)}'
            f'<sup style="font:11px {SANS};color:#6b6580;">&nbsp;based on {", ".join(_e(i) for i in a.finding_ids)}</sup></td></tr>'
            for a in dossier.adapt)
        sections.append(_section("What To Actually Do", "🛠", "#d7c8ff",
                                 f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{items}</table>',
                                 "instead of doomscrolling"))
    verdict = f"""<div style="background:{INK};color:#fff;border-radius:14px;padding:22px;font:18px/1.55 {FONT};">
<div style="font:bold 13px {SANS};letter-spacing:3px;color:{MUSTARD};">THE VERDICT</div>
<div style="margin-top:8px;">{_e(dossier.verdict)}</div></div>"""
    sections.append(f'<tr><td class="pad" style="padding:28px 32px 8px 32px;">{verdict}</td></tr>')

    refs = "".join(
        f'<li style="margin:0 0 8px 0;"><a href="{_e(sources[sid].url)}" style="color:{INK};">{_e(sources[sid].title)}</a>'
        f' <span style="color:#6b6580;">· {_e(sources[sid].publisher)}{", " + str(sources[sid].year) if sources[sid].year else ""}'
        f'{" · fixture excerpt" if sources[sid].backend == "fixture" else ""}</span></li>'
        for sid in cites.order)
    uncert = "".join(f"<li>{_e(u)}</li>" for u in dossier.uncertainties)
    footer = f"""
<tr><td class="pad" style="padding:24px 32px 32px 32px;font:13px/1.5 {SANS};color:{INK};">
<div style="font:bold 14px {SANS};letter-spacing:2px;">RECEIPTS</div>
<ol style="padding-left:20px;margin:10px 0 16px 0;">{refs}</ol>
{f'<div style="font:bold 14px {SANS};letter-spacing:2px;">STILL UNCERTAIN</div><ul style="padding-left:20px;margin:8px 0 16px 0;">{uncert}</ul>' if uncert else ''}
<div style="color:#6b6580;">Researched and drafted by Agent Lab's Calm Down agent. Every finding above was checked against the text of the source it cites. This email is for perspective, not career, legal or financial advice. Nobody can promise any profession is safe forever, including this email.</div>
</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting"><title>{_e(dossier.headline)}</title>
<style>@media only screen and (max-width:620px){{.wrap{{width:100%!important}}.pad{{padding-left:16px!important;padding-right:16px!important}}.hero{{font-size:52px!important}}}}</style>
</head>
<body style="margin:0;padding:0;background:{PAPER};">
<div style="display:none;max-height:0;overflow:hidden;">{_e(dossier.subtitle)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER};"><tr><td align="center" style="padding:16px 0;">
<table role="presentation" class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:{PAPER};border:3px solid {INK};border-radius:18px;overflow:hidden;">
{banner}
<tr><td class="pad" style="background:{PINK};padding:36px 32px 28px 32px;border-bottom:3px solid {INK};">
  <div style="font:bold 12px {SANS};letter-spacing:3px;color:{INK};">AN URGENT BULLETIN FROM YOUR FAMILY'S DEPARTMENT OF PERSPECTIVE</div>
  <div class="hero" style="margin-top:10px;font:900 68px/0.95 {SANS};color:#fff;text-shadow:4px 4px 0 {INK};letter-spacing:-2px;">{_e(dossier.headline)}</div>
  <div style="margin-top:14px;font:bold italic 22px/1.3 {FONT};color:{INK};">{_e(dossier.subtitle)}</div>
</td></tr>
<tr><td class="pad" style="padding:26px 32px 4px 32px;font:18px/1.6 {FONT};color:{INK};">{_e(dossier.opening)}</td></tr>
{"".join(sections)}
{footer}
</table></td></tr></table></body></html>
"""


def render_email_text(dossier: CalmDownDossier, report: VerificationReport, sources: dict[str, SourceRecord],
                      fixture: bool) -> str:
    if not report.ok:
        raise ValueError("refusing to render a reassurance email from insufficient research")
    by_id = {f.id: f for f in report.verified}
    cites = Citations(sources)
    wrap = lambda s, indent="": textwrap.fill(s, 72, initial_indent=indent, subsequent_indent=indent)  # noqa: E731
    out: list[str] = []
    if fixture:
        out += ["*** FIXTURE PREVIEW: built from hand-written offline excerpts. Not live research. Do not send. ***", ""]
    out += [dossier.headline, dossier.subtitle, "=" * 72, "", wrap(dossier.opening), ""]
    bar = "#" * dossier.doom_meter.score + "." * (10 - dossier.doom_meter.score)
    out += ["THE DOOM METER (highly subjective, not a forecast)", f"[{bar}] {dossier.doom_meter.score}/10: {dossier.doom_meter.label}",
            wrap(dossier.doom_meter.rationale), ""]

    def block(title: str, findings: list[Finding]) -> None:
        out.extend([title, "-" * len(title)])
        for f in findings:
            out.append(wrap(f"* [{KIND_LABEL[f.evidence_kind]}] {f.claim} {cites.text(f)}"))
            out.append(wrap(f'"{f.evidence_quote}"', "    "))
            out.append(wrap(f"Fine print: {f.caveat}", "    "))
            if f.analogy:
                out.append(wrap(f"Translation: {f.analogy}", "    "))
        out.append("")

    out.extend(["THINGS THE RESEARCH ACTUALLY SAYS", "---------------------------------"])
    for n, f in enumerate([f for f in report.verified if f.surprising] or report.verified[:3], 1):
        out.append(wrap(f"{n}. [{KIND_LABEL[f.evidence_kind]}] {f.claim} {cites.text(f)}"))
    out.append("")
    block("WHAT THE DOOMERS GET RIGHT", [by_id[i] for i in dossier.doomers_get_right if i in by_id])
    block("REASONS TO CHILL", [by_id[i] for i in dossier.reasons_to_chill if i in by_id])
    if dossier.adapt:
        out += ["WHAT TO ACTUALLY DO", "-------------------"] + [wrap(f"-> {a.text}") for a in dossier.adapt] + [""]
    out += ["THE VERDICT", "-----------", wrap(dossier.verdict), ""]
    if dossier.uncertainties:
        out += ["STILL UNCERTAIN"] + [wrap(f"- {u}") for u in dossier.uncertainties] + [""]
    out.append("RECEIPTS")
    for n, sid in enumerate(cites.order, 1):
        s = sources[sid]
        out.append(f"[{n}] {s.title}. {s.publisher}{', ' + str(s.year) if s.year else ''}. {s.url}")
    out += ["", wrap("Researched and drafted by Agent Lab's Calm Down agent. Perspective, not career, legal "
                     "or financial advice. Nobody can promise any profession is safe forever.")]
    return "\n".join(out) + "\n"


def render_dossier_md(dossier: CalmDownDossier, report: VerificationReport, sources: dict[str, SourceRecord],
                      mode: str, run_meta: dict) -> str:
    md = [f"# Research dossier: {dossier.fear}", ""]
    if mode == "fixture":
        md += ["> **FIXTURE PREVIEW.** Every source text below is a hand-written offline excerpt that paraphrases a",
               "> real public source from memory. Verification here proves the pipeline only checks quotes against",
               "> retrieved text. It does **not** prove these quotations or numbers are accurate. A live run is required.", ""]
    md += ["## Run", ""] + [f"- **{k}:** `{v}`" for k, v in run_meta.items()] + [f"- **retrieval mode:** `{mode}`", ""]
    md += ["## Verification outcome", "", f"- **status:** `{report.status}`",
           f"- **verified findings:** {len(report.verified)} of {len(dossier.findings)}",
           f"- **distinct verified sources:** {len(report.cited_sources)}", ""]
    if report.problems:
        md += ["### Blocking problems", ""] + [f"- {p}" for p in report.problems] + [""]
    if report.rejected:
        md += ["### Rejected findings", ""] + [f"- **{r.item}:** {r.reason}" for r in report.rejected] + [""]

    for kind, title in (("demonstrated_fact", "Demonstrated facts"), ("forecast", "Forecasts and exposure estimates"),
                        ("speculation", "Speculation and argument")):
        group = [f for f in report.verified if f.evidence_kind == kind]
        if not group:
            continue
        md += [f"## {title}", ""]
        for f in group:
            md += [f"### {f.id}: {f.claim}", "",
                   f"- **stance:** {f.stance} · **confidence:** {f.confidence}",
                   f"- **sources:** {', '.join(f.source_ids)}",
                   f"- **evidence:** \"{f.evidence_quote}\"", f"- **caveat:** {f.caveat}", ""]

    md += ["## Opposing evidence", "", "**Supports the fear:** " +
           (", ".join(f.id for f in report.verified if f.stance in ("supports_fear", "mixed")) or "none verified"),
           "", "**Challenges the fear:** " +
           (", ".join(f.id for f in report.verified if f.stance == "challenges_fear") or "none verified"), ""]
    md += ["## Uncertainty", ""] + ([f"- {u}" for u in dossier.uncertainties] or ["- none stated"]) + [""]
    md += ["## Limitations", ""] + ([f"- {u}" for u in dossier.limitations] or ["- none stated"]) + [""]
    md += ["## Source provenance", "", "| id | title | publisher | year | tier | backend | read | retrieved | text sha256 |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for sid, s in sorted(sources.items()):
        md.append(f"| {sid} | [{s.title.replace('|', '/')}]({s.url}) | {s.publisher.replace('|', '/')} | {s.year or ''} | "
                  f"{s.tier} | {s.backend} | {'yes' if s.read else 'no'} | {s.retrieved_at or ''} | "
                  f"`{(s.text_sha256 or '')[:16]}` |")
    return "\n".join(md) + "\n"
