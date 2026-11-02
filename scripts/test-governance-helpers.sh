#!/usr/bin/env bash
# Regression tests for scripts/check-scope.sh and .claude/hooks/guard-bash.py.
# Uses throwaway repositories only; guarded commands are passed to the hook as
# JSON and never executed.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
scope="$root/scripts/check-scope.sh"
hook="$root/.claude/hooks/guard-bash.py"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0

expect() { # expect <want-exit> <got-exit> <label>
  if [[ "$1" == "$2" ]]; then
    echo "ok   $3"
  else
    echo "FAIL $3 (want exit $1, got $2)"
    fails=$((fails + 1))
  fi
}

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
repo="$tmp/repo"
git init -q -b main "$repo"
git -C "$repo" commit -q --allow-empty -m base
git -C "$repo" checkout -q -b feature
echo x >"$repo/a.txt"
git -C "$repo" add a.txt
git -C "$repo" commit -q -m a
orphan="$(git -C "$repo" commit-tree "$(git -C "$repo" mktree </dev/null)" -m orphan)"
echo y >"$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -q -m readme
git -C "$repo" branch -q other HEAD

# --- check-scope.sh -----------------------------------------------------------
run_scope() { (cd "$repo" && "$scope" "$@" >/dev/null 2>&1); echo $?; }

expect 0 "$(run_scope --base main --allowed a.txt --allowed README.md)" "scope: allowed paths pass"
expect 1 "$(run_scope --base main --allowed a.txt)" "scope: unauthorized path fails"
expect 2 "$(run_scope --base HEAD:README.md --allowed nope)" "scope: blob base rejected"
expect 2 "$(run_scope --base "$orphan" --allowed nope)" "scope: base without merge base rejected"
expect 2 "$(run_scope --base no-such-ref --allowed nope)" "scope: unknown base rejected"

# Comparison failure after validation: shim git so `diff` fails.
shim="$tmp/shim"
mkdir -p "$shim"
real_git="$(command -v git)"
cat >"$shim/git" <<EOF
#!/usr/bin/env bash
[[ "\$1" == diff ]] && { echo "fatal: simulated diff failure" >&2; exit 128; }
exec "$real_git" "\$@"
EOF
chmod +x "$shim/git"
expect 2 "$(PATH="$shim:$PATH" run_scope --base main --allowed nope)" "scope: failed git comparison is an error, not PASS"

# --- guard-bash.py ------------------------------------------------------------
hook_exit() { # hook_exit <command> [cwd]
  python3 -c 'import json,sys; print(json.dumps({"tool_input":{"command":sys.argv[1]},"cwd":sys.argv[2]}))' \
    "$1" "${2:-$repo}" | python3 "$hook" 2>/dev/null
  echo $?
}

git -C "$repo" checkout -q main
for prefix in "git" "git -C $repo" "git -C$repo" "git -c core.pager=cat" "git -ccore.pager=cat" \
              "git -C$repo -cuser.name=x" "git --no-pager"; do
  expect 2 "$(hook_exit "$prefix add .")" "hook blocks: $prefix add ."
  expect 2 "$(hook_exit "$prefix add -A")" "hook blocks: $prefix add -A"
  expect 2 "$(hook_exit "$prefix commit -m x")" "hook blocks: $prefix commit on main"
  expect 2 "$(hook_exit "$prefix push --force origin feature")" "hook blocks: $prefix push --force"
  expect 2 "$(hook_exit "$prefix push origin main")" "hook blocks: $prefix push origin main"
  expect 2 "$(hook_exit "$prefix reset --hard")" "hook blocks: $prefix reset --hard"
  expect 2 "$(hook_exit "$prefix clean -fd")" "hook blocks: $prefix clean -fd"
  expect 2 "$(hook_exit "$prefix checkout .")" "hook blocks: $prefix checkout ."
done
expect 0 "$(hook_exit "gh pr merge 1")" "hook allows: gh pr merge"
expect 2 "$(hook_exit "gh pr merge 1 --admin")" "hook blocks: gh pr merge --admin"
expect 2 "$(hook_exit "gh --repo o/r pr merge 1 --admin")" "hook blocks: gh global flags + --admin"
expect 2 "$(hook_exit "env GH_HOST=github.com gh pr merge 1 --admin=true")" "hook blocks: env prefix + --admin="
expect 0 "$(hook_exit "gh pr view 1 --json title")" "hook allows: gh pr view"

# -C<dir> must resolve the target repo: committing on feature elsewhere is fine,
# while the same command aimed at a repo on main is blocked.
feature="$tmp/feature"
git -C "$repo" worktree add -q "$feature" feature
expect 0 "$(hook_exit "git -C$feature commit -m x" "$tmp")" "hook allows: git -C<feature-wt> commit"
expect 2 "$(hook_exit "git -C$repo commit -m x" "$tmp")" "hook blocks: git -C<main-wt> commit"

for cmd in "git status" "git -C$feature status" "git -C $feature log --oneline" "git -c color.ui=never diff" \
           "git -C$feature add a.txt" "git -C $feature add README.md" "git -C$feature commit -m msg" \
           "git -C$feature push -u origin feature" "git -C$feature push --force-with-lease origin feature" \
           "git --no-pager log -1" "git -C$feature reset --soft HEAD~1" "git -C$feature checkout -- a.txt"; do
  expect 0 "$(hook_exit "$cmd" "$feature")" "hook allows: $cmd"
done

# --- codex-review.sh ----------------------------------------------------------
# gh and codex are PATH shims; no real GitHub or Codex call is made.
review="$root/scripts/codex-review.sh"
cshim="$tmp/cshim"
mkdir -p "$cshim"
good="$(printf 'a%.0s' {1..40})"
other="$(printf 'b%.0s' {1..40})"
cat >"$cshim/gh" <<'EOF'
#!/usr/bin/env bash
# `pr view ... headRefOid` pops the next head from $FAKE_HEADS (one per line); the last repeats.
if [[ "$*" == *headRefOid* ]]; then
  [[ "${GH_FAIL:-}" == 1 ]] && { echo "gh: simulated failure" >&2; exit 1; }
  head -n1 "$FAKE_HEADS"
  if [[ $(wc -l <"$FAKE_HEADS") -gt 1 ]]; then sed -i 1d "$FAKE_HEADS"; fi
elif [[ "$*" == *"pr diff"* ]]; then echo "diff --git a/x b/x"
else echo '{"title":"t"}'; fi
EOF
cat >"$cshim/codex" <<'EOF'
#!/usr/bin/env bash
# Records its argv, fails fast if stdin is not closed, then writes the review per $CODEX_MODE.
echo "$*" >"$CODEX_ARGS"
if read -r -t 2 _; then :; else [[ $? -gt 128 ]] && { echo "stdin left open" >&2; exit 9; }; fi
while [[ $# -gt 0 ]]; do [[ "$1" == --output-last-message ]] && out="$2"; shift; done
case "${CODEX_MODE:-ok}" in
  fail) echo "boom" >&2; exit 1 ;;
  empty) : >"$out" ;;
  noverdict) echo "looks fine" >"$out" ;;
  *) printf 'No findings.\n\nVERDICT=APPROVED\nPR: #7\nCommit: %s\nFindings: none\n' "$FAKE_SHA" >"$out" ;;
esac
EOF
chmod +x "$cshim/gh" "$cshim/codex"

run_review() { # run_review <pr> <sha> [heads...]; prints exit code
  local pr="$1" sha="$2"; shift 2
  printf '%s\n' "$@" >"$tmp/heads"
  PATH="$cshim:$PATH" FAKE_HEADS="$tmp/heads" FAKE_SHA="$good" CODEX_ARGS="$tmp/codex.args" \
    "$review" "$pr" "$sha" >"$tmp/review.out" 2>"$tmp/review.err"
  echo $?
}

expect 2 "$("$review" >/dev/null 2>&1; echo $?)" "review: no arguments rejected"
expect 2 "$(run_review x "$good" "$good")" "review: non-numeric PR rejected"
expect 2 "$(run_review 7 abc123 "$good")" "review: short SHA rejected"
expect 2 "$(run_review 7 "${good^^}" "$good")" "review: uppercase SHA rejected"
expect 2 "$(run_review 7 "$good" "$other")" "review: stale head rejected before codex runs"
[[ ! -e "$tmp/codex.args" ]] && echo "ok   review: codex not launched on head mismatch" || { echo "FAIL review: codex launched on mismatch"; fails=$((fails + 1)); }
expect 3 "$(GH_FAIL=1 run_review 7 "$good" "$good")" "review: gh failure is an error"
expect 0 "$(run_review 7 "$good" "$good")" "review: matching head succeeds"
grep -q -- "--sandbox read-only" "$tmp/codex.args" && grep -q -- "--output-last-message" "$tmp/codex.args" \
  && echo "ok   review: read-only sandbox and output file used" || { echo "FAIL review: sandbox/output flags missing"; fails=$((fails + 1)); }
[[ -s "$(cat "$tmp/review.out")" ]] && echo "ok   review: prints the saved review path" || { echo "FAIL review: no review path"; fails=$((fails + 1)); }
expect 3 "$(CODEX_MODE=fail run_review 7 "$good" "$good")" "review: codex failure is an error"
expect 3 "$(CODEX_MODE=empty run_review 7 "$good" "$good")" "review: empty review rejected"
expect 3 "$(CODEX_MODE=noverdict run_review 7 "$good" "$good")" "review: review without VERDICT record rejected"
expect 3 "$(run_review 7 "$good" "$good" "$other")" "review: head moved during review rejected"

echo
if ((fails)); then
  echo "$fails test(s) failed"
  exit 1
fi
echo "All governance helper tests passed"
