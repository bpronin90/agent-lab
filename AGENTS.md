# AGENTS.md

Instructions for every coding agent working in this repository: Codex, Claude Code,
Copilot, Gemini, Cursor, and local, cloud, or remote sessions alike.

`CONTRIBUTING.md` is binding for agents. Read it before changing anything.

The owner keeps a fuller agent protocol outside this repository. On the owner's
machine it is restored as the Git-ignored `AGENTS.override.md`, which Codex loads in
place of this file and Claude Code imports through `CLAUDE.md`. It only adds detail
and never relaxes these rules. If `AGENTS.override.md` is absent, as in a fresh clone
or a cloud checkout, then `CONTRIBUTING.md` and this file are the whole protocol, and
anything they do not permit is not permitted.

Rules that hold in every environment:

- Work on a branch, never on the default branch. Never commit or push to the default
  branch. Never force-push a shared branch unless the owner explicitly directs it.
- Never reset, clean, stash away, or overwrite uncommitted work you did not create. If
  the checkout is dirty, work in a separate worktree.
- Stage explicit paths only (no `git add -A`, `git add .`, or `git commit -a`).
- Every PR body contains exactly one standalone `Issue: #<n>` or `Issue: none` line
  and the contract headings from `CONTRIBUTING.md`. Change only the Allowed Files, plus docs your change
  makes untrue, and run `bash scripts/check-scope.sh --allowed <path> ...` before handoff.
- Merge a PR only under the merge conditions in `CONTRIBUTING.md`. Never close issues
  (other than through a merged PR's `Closes #n`), deploy, release, change repository
  settings or visibility, touch credentials or secrets, or cause external side effects.
  Only the owner does these, and an ambiguous "looks good" is not authorization.
- Do not review or approve a head you produced.
- Report exactly which checks ran. Never present simulated, skipped, or unexecuted
  validation as proven.
- When a rule is unclear or a needed step needs owner authority, stop and ask.
