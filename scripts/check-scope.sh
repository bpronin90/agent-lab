#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/check-scope.sh --allowed <path> [--allowed <path> ...]
                         [--allowed-file <file>] [--base <ref>]

The base defaults to origin/HEAD, then origin/main, then origin/master.

Scope-checks base-relative and staged paths against the explicitly supplied
Allowed Files, while reporting unstaged and untracked paths informationally.
Allowed directory paths may end with '/'. The script never changes Git state.
Exit status is 0 when every scope-checked path is allowed, 1 when unauthorized
paths exist, and 2 for usage or Git errors.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

base=""
declare -a allowed=()
declare -a allowed_files=()

while (($#)); do
  case "$1" in
    --allowed)
      (($# >= 2)) || die "--allowed requires a path"
      allowed+=("$2")
      shift 2
      ;;
    --allowed-file)
      (($# >= 2)) || die "--allowed-file requires a file"
      allowed_files+=("$2")
      shift 2
      ;;
    --base)
      (($# >= 2)) || die "--base requires a Git ref"
      base="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if ((${#allowed[@]} == 0 && ${#allowed_files[@]} == 0)); then
  die "supply at least one --allowed path or --allowed-file"
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a Git repository"
cd "$repo_root"

if [[ -z "$base" ]]; then
  if ref="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"; then
    base="$ref"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    base="origin/main"
  elif git rev-parse --verify origin/master >/dev/null 2>&1; then
    base="origin/master"
  elif git rev-parse --verify main >/dev/null 2>&1; then
    base="main"
  else
    die "cannot infer a base ref; pass --base <ref>"
  fi
fi
git rev-parse --verify --quiet "$base^{commit}" >/dev/null || die "base is not a valid commit: $base"
git merge-base "$base" HEAD >/dev/null 2>&1 || die "no merge base between $base and HEAD"

normalize() {
  local path="$1"
  path="${path#./}"
  printf '%s' "$path"
}

for file in "${allowed_files[@]}"; do
  [[ -f "$file" ]] || die "allowed-file not found: $file"
done

while IFS= read -r path; do
  [[ -n "$path" ]] && allowed+=("$(normalize "$path")")
done < <(
  for file in "${allowed_files[@]}"; do
    sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$file"
  done
)

# Capture each Git enumeration at top level so a failure is fatal; errors inside
# process substitution would otherwise be silently ignored.
committed="$(git diff --name-only "$base...HEAD")" || die "git diff against base failed: $base"
staged="$(git diff --cached --name-only)" || die "git diff --cached failed"
unstaged="$(git diff --name-only)" || die "git diff failed"
untracked="$(git ls-files --others --exclude-standard)" || die "git ls-files failed"

declare -a patch_changed=()
while IFS= read -r path; do
  [[ -n "$path" ]] && patch_changed+=("$path")
done < <(
  printf '%s\n' "$committed" "$staged" | sed '/^$/d' | sort -u
)

declare -a worktree_only=()
while IFS= read -r path; do
  [[ -n "$path" ]] || continue
  skip=false
  for patch_path in "${patch_changed[@]}"; do
    if [[ "$path" == "$patch_path" ]]; then
      skip=true
      break
    fi
  done
  [[ "$skip" == true ]] || worktree_only+=("$path")
done < <(
  printf '%s\n' "$unstaged" "$untracked" | sed '/^$/d' | sort -u
)

echo "Base: $base"
echo "PR/staged paths checked:"
if ((${#patch_changed[@]} == 0)); then
  echo "  (none)"
else
  printf '  %s\n' "${patch_changed[@]}"
fi
if ((${#worktree_only[@]})); then
  echo "Informational unstaged/untracked paths (not scope-checked):"
  printf '  %s\n' "${worktree_only[@]}"
fi

is_allowed() {
  local path="$1" entry
  for entry in "${allowed[@]}"; do
    entry="$(normalize "$entry")"
    [[ -n "$entry" ]] || continue
    if [[ "$entry" == */ ]]; then
      [[ "$path" == "$entry"* ]] && return 0
    elif [[ "$path" == "$entry" ]]; then
      return 0
    fi
  done
  return 1
}

declare -a unauthorized=()
for path in "${patch_changed[@]}"; do
  is_allowed "$path" || unauthorized+=("$path")
done

if ((${#unauthorized[@]})); then
  echo "Unauthorized paths:"
  printf '  %s\n' "${unauthorized[@]}"
  exit 1
fi

echo "Scope check: PASS"
