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
- Each search makes two planned OpenAlex requests, one after the other: the relevance search
  (`has_abstract:true`), then the same query limited to works published in a recent window
  (`from_publication_date`). `AGENT_LAB_CALM_DOWN_RECENT_DAYS` sets the window in days: default 730,
  an integer from 1 to 3650, read from the resolved research environment and refused before any
  network activity if invalid. The window and its UTC reference date are fixed once per session and are
  part of the Claude `config_hash`. Results are merged recent-first, alternating with relevance results,
  deduplicated by OpenAlex id and capped at 5, so the Claude path's two reads per query see one from
  each pass when both have distinct results. An empty recent pass leaves relevance results only; nothing
  labels them recent. Either request failing ends that search through the normal error path (no retry,
  no anonymous fallback, no third request); a passed deadline stops the second request. At most 16
  initial OpenAlex requests per session (redirect hops are bounded separately). Search, read, model
  and deadline limits are unchanged. The second pass is planned retrieval, never recovery from failure.
- The instructions ask both model paths to prefer relevant recent evidence for current developments,
  keep older foundational research where it is still the best evidence, not treat newer as stronger,
  and say in words (no years) when evidence is mostly older or recent evidence was not found.
  Offline tests check request construction and bounds, not live OpenAlex coverage or citation freshness.
- `fetch_url` accepts only HTTPS, standard port, no embedded credentials, and an allowlist of research, government, court and professional hosts (`ALLOWED_HOSTS`). Every redirect hop is re-checked. Hosts that resolve to non-public IPs are refused.
- Caps: 8 searches, 14 reads, 5 results per search, 2 MB per response, 15 s timeout, 6000 characters of text per source. PDFs are not parsed.
- No cookies or auth headers are sent. Source text is wrapped in untrusted-data markers, and the instructions tell the model to ignore instructions inside it.
- Provenance (`sources.json`) records URL, backend, retrieval time and the SHA-256 of the exact text the model saw.

### BLS primary releases (#44)

Labor-market fears also get recent official employment data from public BLS Employment Situation
releases, through the same `safe_get` path. No paid API, new provider, dependency or host is added.

- **Trust boundary.** Application-owned discovery seed (`BLS_CATALOG`: the Employment Situation
  archive index on `www.bls.gov`) → untrusted index links and release content → the catalog link
  pattern (`news.release/archives/empsit_MMDDYYYY.htm`) plus `check_url`/`safe_get` on every hop, with
  redirects pinned to `www.bls.gov` → recorded source snapshots → synthesis and mechanical verification.
  Models never supply BLS URLs, and nothing in fetched text or links can cause another fetch.
- **Relevance rule.** Deterministic: a query (or, on the Claude path, the fear or any planned query)
  contains a labor-market word from `LABOR_TERMS` (job(s), employment, labor, workers, hiring, layoffs,
  wages, occupations, careers and similar). Other fears never contact BLS.
- **Bounds.** One index fetch and at most two release fetches per run, newest first. Each attempt,
  including discovery and failures, is counted against the 14-read session cap before any request.
  On the Claude path the releases take two of the twelve planned reads (academic reads drop to ten, still
  at most two per query) and two texts' worth of the 60,000-character evidence cap. They are read only
  after both sides have academic coverage and never count toward either side. A failure or an expired
  deadline ends the run before synthesis, with no retry or fallback.
- **Dates.** The publication date comes from the publisher's dated archive file name only; links
  without a valid date, older than the recent window, or in the future are dropped. The statistical
  reference period is taken from the release heading when present. Each snapshot header states the
  publication date, the reference period and the retrieval time separately. If no recent release is
  found, the run says so as a retrieval limitation and invents nothing. The index page is never a source.
- **Evidence.** Releases are `backend="fetch"` sources with publisher, publication year, `published`,
  `reference_period`, URL, retrieval time and text digest. Bundle reload, digest checks and offline
  finalize are unchanged. Both model paths are told (rule 8) that releases measure aggregate outcomes,
  not AI causation, displacement, forecasts or exposure, and that a release never counts for both sides.
  The tool-calling agent reaches them with `bls_employment_releases` (one effective call per run).
- Offline tests use synthetic BLS HTML. They do not establish compatibility with live BLS markup or that
  any release supports a claim.

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
export AGENT_LAB_ALLOW_CLAUDE_CLI=1                                            # per-launch gate
export AGENT_LAB_CALM_DOWN_RETRIEVAL=live      # or leave unset for the offline fixture corpus
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-claude
```

**Command defaults and `.env.live`.** Only `agent-lab calm-down` (research) defaults to provider
`claude-cli` and requested model `claude-sonnet-5-5`; explicit configuration wins. Every other command,
and `Settings.from_env({})`, keeps the offline `mock` default, and `calm-down-finalize` stays offline: it
loads no live configuration and needs no Claude settings or login. Defaulting a provider never opens the
gate: `AGENT_LAB_ALLOW_CLAUDE_CLI=1` remains an explicit operator opt-in, and `AGENT_LAB_PROVIDER=mock`
with fixture retrieval stays entirely offline. Fixture retrieval with Claude still launches Claude and
consumes Claude quota; retrieval selection defaults to `fixture`.

Research reads exactly one optional file, `.env.live` at the **checkout root** (the directory holding
`pyproject.toml`, found from the installed module's location, never from the working directory or a
parent search). It is git-ignored. A missing file is harmless. When `agent_lab` is installed as a package
rather than run from a checkout there is no checkout root, so no file is read and the process environment
and defaults apply. Precedence: process environment (including explicitly empty values) over the file
over command defaults. The result is resolved once, before settings, research-bound overrides, retrieval
selection and the launch gate, and is passed to the run explicitly; it is never exported to the process
environment, so it cannot leak into a later command. `CI` and `KUBERNETES_SERVICE_HOST` count only when
set in the process environment (even if empty); a file cannot set them. `OPENALEX_API_KEY` is read from the
process environment only.

The file grammar is deliberately tiny and never executes anything: blank lines, `#` comment lines, and
`KEY=VALUE` (key `[A-Za-z_][A-Za-z0-9_]*`; the value is literal text, optionally in one matching pair of
single or double quotes). No `export`, interpolation, escapes, command substitution or sourcing; `$(...)`,
backticks and `$VAR` stay literal text. A malformed line, unbalanced quote or duplicate key is a
`config` error that names the line, never the value.

**Effort.** Optional `AGENT_LAB_CLAUDE_EFFORT` adds `--effort <level>` to each launch. Accepted values,
from the installed CLI's `claude --help`: `low`, `medium`, `high`, `xhigh`, `max`. An invalid value fails
before any subprocess launch; unset omits the argument; a CLI that rejects the flag fails as
`config`/`unsupported_flag` with no retry or fallback. The effective effort is part of the Claude run's
`config_hash`; the general mock/Gemini hash is unchanged.

A successful run makes exactly **two** CLI launches that share one context (one deadline from
`AGENT_LAB_DEADLINE_SECONDS`, one request allowance of 2, one usage tally):

1. **Plan.** The model returns a `QueryPlan`: 2 to 6 search queries, each tagged `supports_fear` or
   `challenges_fear`, with both sides present. It is validated locally (`extra="forbid"`, query length
   capped by retrieval's own limit). It cannot name URLs, tools or commands.
2. **Retrieve (application).** The application runs each query through the existing `search_sources`
   and reads at most 2 results per query with `read_source` (at most 12 reads, 60,000 source
   characters; for labor-market fears two of those reads go to BLS releases, see above), inside the same retrieval session, allowlist and digests as the Gemini path. Each search or
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
`AGENT_LAB_CLAUDE_SYNTHESIS_OUTPUT_TOKENS` (default 20,000). Each is
checked against the CLI's *reported* usage after that response: an allocation expectation, not a
service-side cap. An owner-reported observation of 5,820 total output tokens is not measured synthesis usage and does not prove subscription capacity for a run. Every live use needs the per-launch
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
  automatic paid fallback. The expected live use is one research run; see the phase allocations above.
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

## Unreviewed rendering and local n8n delivery (#36)

**Unreviewed mode.** `agent-lab calm-down --unreviewed --out <dir> -- <fear>` is an explicit opt-in that
skips only the human support approval. Every other check (citations, quotes, numbers, negation, balance,
schema, bundle integrity, output safety) still runs; a failure renders nothing. No approval, review hash or
reviewer is recorded: the summary, `mechanical-report.json` and `bundle.json` say human review is
`absent`, and the edition carries a prominent **"Unreviewed, AI-generated"** banner in HTML and text (the
fixture label is kept too). Mechanical acceptance does not establish that a claim is factually supported.
Without the flag nothing changes: research stops at `needs_review`, and `calm-down-finalize` still needs a
person's review file. `mechanical-report.json` is bound to the exact `email.html`/`email.txt` digests.

**Transport.** Per the owner's direction on #36, host execution goes through the **n8n SSH node** to the
host; the owner reported that n8n -> SSH -> host connectivity and subscription-login visibility work. We did
not test that path, any model request, Gmail, or the runner under n8n. The export
`n8n/agent-lab-calm-down-email.json` is inactive with one manual trigger and no schedule or webhook. It
contains no credential, address or host path: attach the SSH and Gmail credentials in n8n yourself, and
define `AGENT_LAB_CALM_DOWN_PYTHON` (interpreter that can import `agent_lab`),
`AGENT_LAB_CALM_DOWN_RUNNER` (path to `scripts/calm-down-delivery.py`), `AGENT_LAB_CALM_DOWN_ROOT` and
`AGENT_LAB_CALM_DOWN_RECIPIENT` in the SSH login environment. No new listener or infrastructure is added.

**Flow.** Manual trigger -> fear field -> a Code node bounds the fear and base64-encodes it (so only
`[A-Za-z0-9+/=]` ever reaches the remote shell) -> SSH `run` -> gate -> SSH `claim` -> gate -> Gmail node
(HTML body of the validated `email.html`, fixed subject, recipient from the runner's private configuration)
-> SSH `ack sent`, or `ack unknown` from the Gmail error output. The SSH node does not fail on a non-zero
exit code, so the gates stop the workflow on any refusal. All SSH and Gmail nodes have retries disabled.

**Host runner** (`scripts/calm-down-delivery.py`, standard library only). It decodes the fear, validates
local configuration, reserves an attempt, launches `python -m agent_lab calm-down --unreviewed --out <generated> -- <fear>`
as an argument array with `shell=False`, forced `claude-cli`/`claude-sonnet-5-5`/live retrieval, and
validates the result. It never sends email.

- **Private state.** `AGENT_LAB_CALM_DOWN_ROOT` must be an absolute, owner-only (no group/other bits) real
  directory with `state/` and `executions/` inside. `runner init` creates them and an empty ledger. The
  owner activates by writing `active` into `state/ALLOWANCE` (after activating the allowance in
  `CONTRIBUTING.md`); anything else, or deleting the file, revokes.
- **Attempts.** Before research, one line is appended and fsynced to `state/ledger.jsonl` under a lock: at most
  3 per America/New_York day (23- and 25-hour DST days included), a fourth is refused, and a missing,
  corrupt, torn or unreadable ledger is treated as exhausted. A crash, timeout or failure keeps the
  reservation. One research run at a time (a busy run lock refuses without reserving). The runner is a Linux
  child subreaper (refused before reserving if unavailable); on timeout, interruption or normal exit it
  SIGKILLs and reaps every descendant, including Claude processes in their own sessions, before releasing
  the run lock. Limit: if the runner itself is SIGKILLed, the research CLI keeps the lock until it exits, but a
  Claude process orphaned by a simultaneous kill of the CLI is not tracked. Direct manual CLI
  runs must follow the same ledger: run `calm-down-delivery.py reserve` first and stop if it refuses.
- **Execution directory.** A fresh, exclusively created `0700` directory named by the execution id. Before
  `ready` and again at `claim`, the runner reads fixed file names with `O_NOFOLLOW` and requires: matching
  execution id, the same run id in the CLI summary, report and bundle, successful research, `complete`
  mechanical status with no problems, live retrieval and no fixture source, unreviewed mode with human
  review absent, matching HTML/text digests, the banner in both bodies, no fixture label, and no `<script>`.
  Anything missing, stale, changed, symlinked or outside the directory refuses and marks the execution
  `invalid`.
- **Delivery claim.** States: `researching`, `failed`, `ready`, `send_attempted`, `sent`, `unknown`,
  `invalid`. `claim` moves `ready` -> `send_attempted` durably under a lock *before* returning the HTML.
  There is no edge back to `ready`, so concurrent claims, re-executions, restarts, timeouts, ambiguous Gmail
  failures and lost acknowledgements cannot lead to an automatic second send. This is at-most-once
  *attempt* semantics, not exactly-once delivery: an email can be lost, and nothing recovers it. A
  `send_attempted` or `unknown` execution needs the owner to look (`runner status`).

**Verification and its limits.** `tests/test_calm_down_email.py` runs offline with a fake CLI, temporary
state and a Gmail stub; its workflow interpreter follows the export's connections and exercises state
transitions. Parsing the JSON and running that interpreter does **not** show the export imports into or
runs on native n8n, that the SSH node passes the environment, or that Gmail accepts the message. Native
n8n/SSH/host runner/Gmail behavior and live research are unverified here.

## Kubernetes / n8n

No new infrastructure is needed. The existing image and Job pattern work: override the container args with
`["calm-down", "<fear>", "--out", "/data/runs/calm-down"]`, and collect the files from the runs volume.
Nothing here schedules runs or sends email (the manual workflow above sends only when the owner triggers it). The personal-use allowance permits manually
triggering local n8n only; it does not permit schedules, webhooks, or Kubernetes Claude execution.
