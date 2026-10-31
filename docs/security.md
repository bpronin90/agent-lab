# Security model and free-tier limitations

## Security
| Area | Control |
| --- | --- |
| Secrets | Only `GEMINI_API_KEY`. It is read from the environment and never logged or stored in run records. `.env` is git- and docker-ignored. In CI it is a GitHub secret; in K8s it is an optional Secret created with `kubectl`. |
| Tools | `calculate` parses with `ast` and accepts only numbers and `+ - * /` (no names, calls or `**`), with a 200-character limit. `current_time` and `lookup_note` are read-only. Demo tools never touch the network, the filesystem or a shell. The `calm_down` retrieval tools are offline fixtures by default. With the explicit opt-in `AGENT_LAB_CALM_DOWN_RETRIEVAL=live`, they make read-only HTTPS GETs: OpenAlex plus an allowlist of hosts, every redirect re-checked, non-public IPs refused, no credentials sent, and size, time and count caps (see [experiments/calm-down.md](experiments/calm-down.md)). No tool writes, executes, or touches the filesystem. |
| Prompt injection | Tools have no side effects, so injected instructions cannot cause harm. Retrieved source text is wrapped in untrusted-data markers, and Calm Down output is mechanically verified before anything is rendered. Fantasy GM outputs are `advisory_only: true` by contract, and roster changes are never automated. Treat tool and external text as untrusted. |
| Runaway loops / cost | `UsageLimits` caps model requests and tool calls; `max_tokens` caps response size; the thinking budget is capped below it. Transient retries are bounded by attempt count and a wall-clock deadline, and never repeat a run once a tool has executed. The K8s Job has a 300 s deadline and `backoffLimit: 1`. The workflow has a 10 min timeout and a concurrency group. |
| Containers | Non-root UID 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges` / `allowPrivilegeEscalation: false`, seccomp `RuntimeDefault`, no service-account token. |
| Supply chain | Python dependencies are installed from hash-locked files (`--require-hashes`). Actions are pinned to full commit SHAs, and CI fails on an unpinned `uses:`. The k3d binary is checksum-verified. Dependabot proposes updates. |
| CI | `permissions: contents: read`. User input reaches shell steps only through `env`, never `${{ }}` interpolation. The schedule is gated by a repository variable. |
| Supabase | Not connected in Phase 1. The proposed schema is isolated, uses RLS with no policies, and grants no privileges to `anon` or `authenticated`. Its intended writer is a server-side job with the service-role key, which must never reach a browser. |
| Data | Run records contain prompts and outputs. Don't put personal data in prompts until a retention policy exists (Phase 2). Only synthetic Fantasy GM fixtures are committed. |

Known gaps:
- Images are not signed or scanned.
- The base image is pinned by tag, not digest (Dependabot watches it).
- The Supabase row above describes the Phase 1 proposal; `docs/phase-2.md` §1.2 and P2.4 supersede it.
- The local JSONL log grows without limit.

## Free-tier limitations
- **Gemini API free tier:** low per-minute and per-day request limits that Google changes over time.
  Check https://ai.google.dev/gemini-api/docs/rate-limits. Free-tier prompts **may be used by Google to
  improve its products**, so don't send private data. A `429` makes the run fail and get recorded;
  PydanticAI does not retry it. One demo run costs about 2–3 requests.
- **GitHub Actions:** free minutes for private repos are limited per month (public repos are free).
  `ci.yml` plus `k3d-smoke.yml` use roughly 5–8 minutes per PR, and a weekly agent run about 1 minute.
  Scheduled workflows are disabled automatically after 60 days without repository activity, and
  scheduled start times can be delayed.
- **Database capacity:** persistence is not connected in Phase 1. A later deployment must fit
  the selected database plan and resource limits without adding paid services. Keep run
  payloads small and review retention before storing real data. Deployment-specific
  accounts, plans, and infrastructure relationships belong in private configuration.
- **K3s/k3d:** free. Running K3s around the clock needs an always-on machine, which conflicts with
  "no laptop must stay on", so GitHub Actions is the always-available scheduler. K3s is for learning
  and for any spare always-on device you already own.
