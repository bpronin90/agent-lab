# Experiment 3: Calm Down

> "Last Week Tonight meets scientific research meets a mildly sarcastic friend telling you to calm down."

`calm_down` is a research agent. It takes a fear about AI, looks for evidence on **both** sides,
checks every citation against the text it actually retrieved, and only then writes a one-off,
playful HTML email. It writes three files: the email, a plain-text version, and a research dossier.
Nothing is ever sent. Delivery is a human copying the file into their own mail client.

First edition: **CALM DOWN.** *The Robots Are Not Taking Your Law Degree.*

## How it works

```
research   agent-lab calm-down "<fear>" --out DIR
  fear ──► runtime.run_agent("calm_down")  (same bounds, retries, error classes, RunRecord)
             │  tools: search_sources · read_source · fetch_url   (ResearchSession records provenance)
             ▼
         CalmDownDossier ──► mechanical verify ──► insufficient ─► LIMITATION.md (exit 4)
                                              └─► needs_review ─► bundle.json + review-request.json (exit 5)
                                                                   (never an email)
human      a person checks review-request.json and writes review.json (approved hashes)

finalize   agent-lab calm-down-finalize DIR --review review.json      (offline: no model, no network)
  bundle.json ──► digest check ──► verify + approvals ──► complete ─► email.html + email.txt (exit 0)
                       │                              └─► still pending/insufficient ─► LIMITATION.md
                       └─ refused bundle or review ─► earlier email removed, LIMITATION.md (exit 2)
```

| File | Role |
| --- | --- |
| `src/agent_lab/agents/calm_down/schemas.py` | Findings carry an evidence kind (`demonstrated_fact` / `forecast` / `speculation`), a stance, a verbatim quote, and a caveat. |
| `retrieval.py` | Fixture or live backends, budgets, allowlist, and fetch safety. |
| `verify.py` | Citation integrity and editorial honesty rules. |
| `render.py` | Deterministic, escaped, email-safe HTML; plain text; Markdown dossier. |
| `pipeline.py` | Ties the run, verification and rendering together. |
| `fixtures/` | Hand-written offline corpus and the mock researcher's draft. **Synthetic.** |

### What verification enforces

Two layers. Both must pass before an email is rendered.

**1. Mechanical checks (`verify.py`).**
- A cited source must have been **read** during this run. Being listed by a search is not enough.
- The evidence quote must appear verbatim in the text shown to the model, in **every** cited source. The match ignores case, whitespace and typographic quotes. Padding a finding with unrelated sources is rejected.
- A claim may not contain a number that is not in its quote. A claim and its quote must agree on negation, so a reversed claim is rejected.
- These rules cover every model-written field that reaches the email: headline, opening, doom meter, advice, verdict, uncertainties, limitations, caveats and analogies.
  - No URLs or domains.
  - No guarantees or proof claims ("safe forever", "proves").
  - No numbers in global editorial copy.
  - A caveat or analogy may reuse only numbers from its own finding's quote.
- "What the Doomers Get Right" may hold only supporting or mixed findings and needs at least one supporting one. "Reasons to Chill" may hold only challenging or mixed findings and needs at least one challenging one. No finding may appear in both.
- The edition needs at least 4 verified findings and at least 3 distinct **works**, counted by URL rather than by id, with verified evidence on both sides.
- If any rule fails, the status is `insufficient`. No email is written, only `LIMITATION.md` and the dossier (exit 4).

**2. Human support review, as a separate offline step.** Matching substrings cannot prove that a claim fairly represents its evidence: its population, outcome, direction, or quantitative context. So the pipeline fails closed.

- `agent-lab calm-down` does **research only**. It never writes an email. It saves:
  - `bundle.json`: the dossier plus a snapshot of every retrieved source text and its SHA-256.
  - `review-request.json`: everything the reviewer is approving. For each finding, the claim, quote, stance, kind, **confidence**, the surprising flag, caveat, analogy, and each cited source's URL and text digest. For the edition as a whole, the editorial copy plus its presentation: doom-meter score, section selections, and which advice cites which finding.
  - It exits 5 (`needs_review`).
- A person approves hashes in a review file.
- `agent-lab calm-down-finalize <dir> --review review.json` loads that **exact** bundle, recomputes and checks the source digests, applies the approvals, and renders. It never calls a model or the network, so the draft that was reviewed is the draft that gets rendered.
- Each finding's approval hash covers everything shown about the finding and its evidence snapshot: claim, quote, stance, kind, confidence, caveat, analogy, surprising flag, and each cited source's URL and text digest. Changing any of these, including the source text, invalidates the approval. Editing a source's text without updating its digest makes finalize refuse the bundle.
- To research again, run `calm-down` again. That produces a new bundle, which needs a new review.

`--review fixture` uses the packaged fixture self-review. It was written by the authoring agent, is not independent, and covers the offline preview only. Finalize refuses it for a live bundle.

Research first deletes every file this pipeline generates in the output directory (`email.html`, `email.txt`, `research-dossier.md`, `LIMITATION.md`, `sources.json`, `review-request.json`, `bundle.json`). Finalize first deletes `email.html`, `email.txt` and `LIMITATION.md`. So a failed step can never leave an earlier email behind. Other files are untouched.

## Running it

```sh
# Offline fixture preview (default): mock provider + hand-written corpus. No network.
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-out    # exit 5: needs review
agent-lab calm-down-finalize calm-down-out --review fixture                            # offline render

# Live edition: explicit opt-in to BOTH a real model and real retrieval.
export AGENT_LAB_PROVIDER=gemini GEMINI_API_KEY=...        # free tier; never commit
export AGENT_LAB_CALM_DOWN_RETRIEVAL=live
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-live   # exit 5: review
# a person reviews calm-down-live/review-request.json, writes review.json, then (offline, no model call):
agent-lab calm-down-finalize calm-down-live --review review.json
```

Live retrieval with the mock provider is refused, so fixture text can never be mixed into a live edition.
Unless you set them, research runs use larger bounds: 16 requests, 30 tool calls, 8192 output tokens, and a 300 s deadline.
Model requests are paced 13 s apart (`AGENT_LAB_MIN_REQUEST_INTERVAL_SECONDS`) to stay under Gemini's free-tier limit of 5 requests per minute. The free tier also caps requests per day per model (20 on `gemini-2.5-flash` at the time of writing), so expect about one live research run per day. The dossier schema is sent as prompt text (`PromptedOutput`) because Gemini rejects it as a function-call schema; it is still fully validated locally.

Exit codes: `0` edition rendered (finalize), `4` research insufficient, `5` waiting for human support review (dossier only in both cases), and otherwise the usual runtime codes (`2` config, `3` transient, `1` other).

## Live retrieval safety

- Search uses the free [OpenAlex](https://openalex.org) works API. Its abstracts become the source text.
- `OPENALEX_API_KEY` (optional, environment only) authenticates search with a free OpenAlex key
  (about a $1/day budget, no billing enabled). Without it, search is anonymous and shares a small
  per-IP budget that OpenAlex can pause under load (HTTP 503 "Anonymous search is paused").
  The key is added as the `api_key` parameter to OpenAlex search requests only: never to
  `fetch_url` or another host, a keyed request that redirects away from `api.openalex.org` is refused,
  it is redacted from errors, and it is not passed to the Claude CLI or any model or saved output.
- `fetch_url` accepts only HTTPS, standard port, no embedded credentials, and an allowlist of research, government, court and professional hosts (`ALLOWED_HOSTS`). Every redirect hop is re-checked. Hosts that resolve to non-public IPs are refused.
- Caps: 8 searches, 14 reads, 5 results per search, 2 MB per response, 15 s timeout, 6000 characters of text per source. PDFs are not parsed.
- No cookies or auth headers are sent. Source text is wrapped in untrusted-data markers, and the instructions tell the model to ignore instructions inside it.
- Provenance (`sources.json`) records URL, backend, retrieval time and the SHA-256 of the exact text the model saw.

## Fixture preview vs live research

`examples/calm-down/` holds the **fixture preview**. Its source excerpts were written by hand from memory
to paraphrase real public works. They are **not verified quotations**, and their numbers must be checked
against the linked sources. Passing verification there only shows that the pipeline works. The email, text and dossier
all carry a FIXTURE PREVIEW banner. The offline tests do not validate live research.

Some fixture figures are older vintages: newer releases of the Stanford payroll study and the BLS outlook report different numbers, and the legal-hallucination figures are pinned to arXiv v1. A live edition has not been produced yet. It needs owner authorization (live Gemini runs spend free-tier quota),
a `GEMINI_API_KEY`, and network egress to OpenAlex and the allowlisted hosts.

## Claude CLI research path (manual only)

`AGENT_LAB_PROVIDER=claude-cli` makes `agent-lab calm-down` research through the existing Claude
subscription (#21) instead of the tool-calling runtime. Only Calm Down research accepts it: the general
model factory has no Claude implementation, so `agent-lab run` with this provider fails as a `config`
error before any subprocess or network activity. Mock stays the default and Gemini is unchanged.

```sh
export AGENT_LAB_PROVIDER=claude-cli AGENT_LAB_CLAUDE_MODEL=<explicit model>   # no default model
export AGENT_LAB_ALLOW_CLAUDE_CLI=1                                            # per-launch gate
export AGENT_LAB_CALM_DOWN_RETRIEVAL=live      # or leave unset for the offline fixture corpus
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-claude
```

A successful run makes exactly **two** CLI launches that share one context (one deadline from
`AGENT_LAB_DEADLINE_SECONDS`, one request allowance of 2, one usage tally):

1. **Plan.** The model returns a `QueryPlan`: 2 to 6 search queries, each tagged `supports_fear` or
   `challenges_fear`, with both sides present. It is validated locally (`extra="forbid"`, query length
   capped by retrieval's own limit). It cannot name URLs, tools or commands.
2. **Retrieve (application).** The application runs each query through the existing `search_sources`
   and reads at most 2 results per query with `read_source` (at most 12 reads, 60,000 source
   characters), inside the same retrieval session, allowlist and digests as the Gemini path. Each search or
   read waits only as long as the shared deadline allows; a slower one (for example a trickling stream)
   is abandoned in a background thread, its result is never used, and the run fails as `limit`. Any retrieval failure, an expired deadline, or reading
   nothing for one side stops the run before synthesis (`insufficient_evidence`): no reassurance from
   missing evidence.
3. **Synthesize.** The model receives the fear and the exact source snapshots it may cite (each in
   untrusted-text markers with its `source_id`) and returns a `CalmDownDossier`, validated locally and
   then verified exactly as on the Gemini path. The saved bundle, human review, fixture-review rejection
   for live bundles, and offline finalize are unchanged. Research never writes or sends an email.

Invalid plans or output, service failures, exhausted bounds and deadline expiry end the run. There is
no retry, repair call, third launch or fallback to another provider. One `RunRecord` is written on
success or caught failure, with phase-one usage kept when phase two fails, unavailable usage marked as
such, the retrieval calls as `tool_calls`, and a `config_hash` covering the model, bounds, launch policy,
both phase instructions, phase allocations and retrieval bounds.

**Phase allocations.** `AGENT_LAB_CLAUDE_PLAN_OUTPUT_TOKENS` (default 1,000) and
`AGENT_LAB_CLAUDE_SYNTHESIS_OUTPUT_TOKENS` (default 4,000), about 5,000 output tokens per run. Each is
checked against the CLI's *reported* usage after that response: an allocation expectation, not a
service-side cap. Subscription capacity for a run is not proven. Every live use needs the per-launch
authorization below, unless the activated personal-use allowance applies.

Offline tests (`tests/test_calm_down.py`) drive a fake `claude` executable through both phases with the
fixture corpus and mocked retrieval failures. They validate the pipeline, not live evidence: no live
Claude call or live retrieval has been run for this path.

### Claude CLI execution core

`src/agent_lab/claude_cli.py` is the internal core for running `claude -p` (#17). The Calm Down research
path above is its only user.

- **Launch gate.** Every launch requires `AGENT_LAB_ALLOW_CLAUDE_CLI=1` and is refused if `CI` or
  `KUBERNETES_SERVICE_HOST` is set, even to an empty value. A refused launch starts nothing and is a
  `config` failure. This catches accidental use in marked environments only. It cannot detect cron,
  prove a human is present, verify GitHub comments, or resist someone who controls the environment.
  Scheduled or unattended use stays prohibited by policy.
- **Per-call authorization.** Unless the owner has activated the "Bounded Personal-Use
  Allowance (Calm Down)" in `CONTRIBUTING.md` (inactive by default; it covers only model
  `claude-sonnet-5-5`, at most 3 attempted runs per New York calendar day with at most 2
  launches each, recorded in a local attempt ledger, and delivery of `email.html`/`email.txt` to
  the owner's private address), each live invocation needs its own owner authorization (planning and
  synthesis separately, at the exact commit and model, naming the phase allocation) plus the OWNER AUTHORIZATION RECORD in `CONTRIBUTING.md`. A launched call consumes that
  authorization even if it fails. Use is existing-subscription only: no API billing, paid overage, or
  automatic paid fallback. The expected live use is one research run of about 5,000 output tokens.
- **Subprocess boundary.** Argument array, no shell, prompt on stdin, a fresh empty temporary working
  directory, and
  `claude -p --output-format json --tools "" --no-session-persistence --disable-slash-commands --setting-sources "" --model <model>`.
  The model is always explicit. Only an allowlist of environment variables (`ENV_ALLOWLIST`: path,
  home, user, locale, XDG directories, `CLAUDE_CONFIG_DIR`) reaches the CLI so it can find the existing
  login. API keys, model overrides and other secrets are dropped. Credentials are never copied or changed.
  These controls are not an OS sandbox.
- **Guarantees.** One context holds one request allowance and one monotonic deadline for every call.
  A call counts once it has spawned, failed or not. Usage the CLI does not report is marked unavailable,
  never counted as zero. There is no retry, fallback, repair or automatic login, and rate-limit
  guidance never triggers another launch. stdout and stderr are drained concurrently under byte caps.
  A timeout or overflow kills and reaps the whole process group, and the temporary directory is
  always removed.
- **Token limit is after the fact.** Reported output tokens are checked against the per-response
  allowance *after* the response arrives. That cannot stop the service from producing more; there is
  no hard service-side token cap.
- **Classification.** Gate, missing binary or login, and unsupported flags → `config`; explicit
  service or rate failures → `transient`; deadline, output bytes, tokens or request allowance → `limit`;
  malformed envelope, generated JSON or schema mismatch → `validation`; error envelopes with exit 0,
  unrecognized exits and unexpected exceptions → `internal`.
- **Records.** `run_with_record` returns a `RunRecord` on success and on every caught failure, with
  `git_sha`, a secret-free `config_hash` (model, bounds, launch policy, instructions), per-call attempts
  and partial accounting. Records hold fixed diagnostic codes and a caller-supplied prompt descriptor,
  never raw prompts, stdout, stderr, credentials or exception text. If the host is killed or storage
  fails, the record can still be lost.

Tests (`tests/test_claude_cli.py`) run a generated fake `claude` executable offline. They do not prove
the installed CLI's flags, native login, subscription billing behavior, or a live research run.

## Kubernetes / n8n

No new infrastructure is needed. The existing image and Job pattern work: override the container args with
`["calm-down", "<fear>", "--out", "/data/runs/calm-down"]`, and collect the files from the runs volume.
Nothing here schedules runs or sends email. The personal-use allowance permits manually
triggering local n8n only; it does not permit schedules, webhooks, or Kubernetes Claude execution.
