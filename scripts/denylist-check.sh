#!/usr/bin/env bash
# Fails if any deployment-specific identifier appears in the working tree, the full git history
# (content, paths and commit metadata). The pattern comes from the DENYLIST_REGEX environment variable
# (an extended, case-insensitive regex) and is never stored in this repository.
# Matches are never printed: only the place they were found, so a log can't leak what it protects.
set -euo pipefail
: "${DENYLIST_REGEX:?set DENYLIST_REGEX}"
found=0
check() { # $1 = label; $2.. = command whose output is searched
  local label="$1"; shift
  if grep -E -i -q -a -- "$DENYLIST_REGEX" < <("$@" 2>/dev/null); then echo "deny-list match in: $label"; found=1; fi
}
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  if grep -E -i -q -a -- "$DENYLIST_REGEX" "$f"; then echo "deny-list match in tracked file: $f"; found=1; fi
done < <(git ls-files -z)
check "file paths" git ls-files
check "commit messages or authors" git log --all --format='%an%n%ae%n%cn%n%ce%n%B'
check "history (diffs)" git log --all -p --no-color --format=
check "history (file paths)" git log --all --name-only --format=
check "tags" git tag -l --format='%(refname) %(contents)'
check "branch names" git branch -a --format='%(refname)'
if [ "$found" -ne 0 ]; then
  echo "::error::deployment-specific identifiers found; run this script locally with the pattern to see them"
  exit 1
fi
echo "deny-list: clean (tree and full history)"
