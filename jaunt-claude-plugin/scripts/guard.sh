#!/usr/bin/env bash
# PreToolUse guard wrapper: resolve the owning jaunt project from the tool-call
# payload path, then run `jaunt guard` from there so generated_dir comes from
# the RIGHT config. Fail-open: any env problem exits 0 (never blocks editing).
set -u

# Resolve an available timeout wrapper: GNU `timeout`, Homebrew `gtimeout`, or
# neither (stock macOS) — in which case run the command with no timeout at all
# rather than 127ing and mis-reporting the tool as missing.
if command -v timeout >/dev/null 2>&1; then
  _timeout_bin=timeout
elif command -v gtimeout >/dev/null 2>&1; then
  _timeout_bin=gtimeout
else
  _timeout_bin=""
fi
run_timeout() {
  local secs="$1"
  shift
  if [ -n "$_timeout_bin" ]; then
    "$_timeout_bin" "$secs" "$@"
  else
    "$@"
  fi
}

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
# `jaunt guard` resolves generated_dir from the payload's `cwd` when present
# (Claude Code sets it to the SESSION cwd), which would defeat the owning-
# project cd above. Rewrite cwd to the owning project so the right config wins.
payload=$(printf '%s' "$payload" | python3 -c '
import json, sys
p = json.load(sys.stdin)
p["cwd"] = sys.argv[1]
print(json.dumps(p))
' "$dir" 2>/dev/null) || exit 0
[ -z "$payload" ] && exit 0
# A jaunt on PATH may be stale (e.g. a version-manager shim for a pre-1.3
# install with no `guard` subcommand). Trust it only when it exits 0;
# otherwise fall back to the project env via uv.
if command -v jaunt >/dev/null 2>&1; then
  out=$(printf '%s' "$payload" | run_timeout 8 jaunt guard 2>/dev/null)
  if [ $? -eq 0 ]; then
    printf '%s' "$out"
    exit 0
  fi
fi
printf '%s' "$payload" | run_timeout 8 uv run --no-sync jaunt guard 2>/dev/null || true
exit 0
