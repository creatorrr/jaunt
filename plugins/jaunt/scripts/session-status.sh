#!/usr/bin/env bash
# SessionStart hook: inject a bounded freshness summary for each Jaunt
# workspace found below the session cwd. Fail open on every environment error.
set -u

payload=$(cat 2>/dev/null || true)
payload_cwd=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
cwd = value.get("cwd") if isinstance(value, dict) else None
if isinstance(cwd, str) and cwd:
    print(cwd)
' 2>/dev/null || true)
root="${payload_cwd:-${JAUNT_WORKSPACE_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}}"
[ -d "$root" ] || exit 0
uv_cache="${UV_CACHE_DIR:-${PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/jaunt-plugin-uv-cache-${UID:-user}}}}"

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

configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' 2>/dev/null | sort)
[ -n "$configs" ] || exit 0
total=$(printf '%s\n' "$configs" | wc -l | tr -d ' ')
limit=12
count=0

echo "Jaunt workspace freshness (run build/check/status from the workspace containing jaunt.toml):"
while IFS= read -r cfg; do
  count=$((count + 1))
  if [ "$count" -gt "$limit" ]; then
    remaining=$((total - limit))
    echo "- ...and $remaining more; inspect them with 'uv run jaunt status --json'"
    break
  fi
  dir=$(dirname "$cfg")
  rel="${dir#"$root"}"; rel="${rel#/}"; [ -n "$rel" ] || rel="."
  out=$(cd "$dir" && UV_CACHE_DIR="$uv_cache" run_timeout 8 uv run --no-sync jaunt status --json --progress none 2>/dev/null || true)
  if [ -z "$out" ]; then
    echo "- $rel: status unavailable"
    continue
  fi
  line=$(printf '%s' "$out" | python3 -c '
import collections, json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rel = sys.argv[1]
fresh = status.get("fresh") or []
stale = status.get("stale") or []
orphans = status.get("orphans") or []
changes = status.get("stale_changes") or {}
counts = collections.Counter(str(changes.get(module) or "structural") for module in stale)
labels = {
    "structural": "implementation rebuild",
    "prose": "semantic gate: refreeze or rebuild",
    "fingerprint": "deterministic re-stamp",
    "re-stamp": "deterministic re-stamp",
    "stub": "deterministic .pyi re-emission when implementation inputs are unchanged",
}
parts = [f"{kind}: {count} ({labels.get(kind, kind)})" for kind, count in sorted(counts.items())]
line = f"- {rel}: {len(fresh)} fresh"
if stale:
    line += f", {len(stale)} stale"
    if parts:
        line += " [" + "; ".join(parts) + "]"
if orphans:
    line += f", {len(orphans)} orphans (run uv run jaunt clean --orphans)"
print(line)
' "$rel" 2>/dev/null || true)
  [ -n "$line" ] && echo "$line" || echo "- $rel: status unavailable"
done <<<"$configs"
exit 0
