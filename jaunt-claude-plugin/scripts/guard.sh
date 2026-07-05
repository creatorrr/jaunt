#!/usr/bin/env bash
# PreToolUse guard wrapper: resolve the owning jaunt project from the tool-call
# payload path, then run `jaunt guard` from there so generated_dir comes from
# the RIGHT config. Fail-open: any env problem exits 0 (never blocks editing).
set -u

payload=$(cat 2>/dev/null) || exit 0
[ -z "$payload" ] && exit 0

path=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = p.get("tool_input") or {}
for k in ("file_path", "path", "notebook_path"):
    v = ti.get(k)
    if v:
        print(v)
        break
' 2>/dev/null) || exit 0
[ -z "$path" ] && exit 0

dir=$(dirname "$path" 2>/dev/null) || exit 0
case "$dir" in /*) ;; *) dir="${CLAUDE_PROJECT_DIR:-$PWD}/$dir" ;; esac
while [ -n "$dir" ] && [ "$dir" != "/" ]; do
  [ -f "$dir/jaunt.toml" ] && break
  dir=$(dirname "$dir")
done
[ -f "$dir/jaunt.toml" ] || exit 0

cd "$dir" 2>/dev/null || exit 0
if command -v jaunt >/dev/null 2>&1; then
  printf '%s' "$payload" | timeout 8 jaunt guard 2>/dev/null || true
else
  printf '%s' "$payload" | timeout 8 uv run --no-sync jaunt guard 2>/dev/null || true
fi
exit 0
