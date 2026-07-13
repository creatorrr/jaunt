#!/usr/bin/env bash
# Deterministic, read-only Jaunt health check. Never builds or calls a model.
# Each check fails open so a broken tool cannot block the surrounding agent.
set -u

root="${JAUNT_WORKSPACE_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}"
[ -d "$root" ] || root="$PWD"
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

echo "== environment"
if command -v codex >/dev/null 2>&1; then
  codex_version_output=$(run_timeout 30 codex --version 2>&1)
  codex_version_status=$?
  codex_version=$(printf '%s\n' "$codex_version_output" | python3 -c '
import sys
for line in sys.stdin:
    line = line.strip()
    if line and not line.startswith("WARNING:"):
        print(line)
        break
' 2>/dev/null || true)
  if [ "$codex_version_status" -eq 0 ] && [ -n "$codex_version" ]; then
    echo "- codex: $codex_version"
  else
    echo "- codex: unavailable"
  fi
  codex_auth=$(run_timeout 30 codex login status 2>&1)
  codex_auth_status=$?
  codex_auth_line=$(printf '%s\n' "$codex_auth" | python3 -c '
import sys
for line in sys.stdin:
    line = line.strip()
    if line and not line.startswith("WARNING:"):
        print(line)
        break
' 2>/dev/null || true)
  if [ "$codex_auth_status" -eq 0 ] && [ -n "$codex_auth_line" ] && ! printf '%s' "$codex_auth_line" | grep -iq 'not authenticated'; then
    echo "- codex auth: $codex_auth_line"
  else
    echo "- codex auth: not authenticated (run 'codex login')"
  fi
else
  echo "- codex: NOT FOUND (builds require the Codex CLI)"
  echo "- codex auth: not authenticated (run 'codex login')"
fi
if command -v uv >/dev/null 2>&1; then
  jaunt_version=$(cd "$root" 2>/dev/null && UV_CACHE_DIR="$uv_cache" run_timeout 30 uv run --no-sync jaunt --version 2>&1)
  jaunt_version_status=$?
  if [ "$jaunt_version_status" -eq 0 ] && [ -n "$jaunt_version" ]; then
    echo "- jaunt: $jaunt_version"
  else
    echo "- jaunt: unavailable (run 'uv sync')"
  fi
else
  echo "- jaunt: unavailable (uv is not on PATH)"
fi
python_version=$(python3 --version 2>&1 || true)
echo "- python3: ${python_version:-NOT FOUND}"

echo
echo "== workspace health"
configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' 2>/dev/null | sort)
if [ -z "$configs" ]; then
  echo "- no jaunt.toml found under $root"
else
  total=$(printf '%s\n' "$configs" | wc -l | tr -d ' ')
  limit=12
  count=0
  while IFS= read -r cfg; do
    count=$((count + 1))
    if [ "$count" -gt "$limit" ]; then
      echo "- ...and $((total - limit)) more"
      break
    fi
    dir=$(dirname "$cfg")
    rel="${dir#"$root"}"; rel="${rel#/}"; [ -n "$rel" ] || rel="."
    out=$(cd "$dir" && UV_CACHE_DIR="$uv_cache" run_timeout 30 uv run --no-sync jaunt status --json --progress none 2>/dev/null || true)
    if [ -z "$out" ]; then
      echo "- $rel: status unavailable (invalid config, missing environment, or timeout)"
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
line = f"- {rel}: {len(fresh)} fresh"
if stale:
    reasons = [f"{kind}: {count} ({labels.get(kind, kind)})" for kind, count in sorted(counts.items())]
    line += f", {len(stale)} stale [" + "; ".join(reasons) + "]"
if orphans:
    line += f", {len(orphans)} orphans (run uv run jaunt clean --orphans)"
print(line)
' "$rel" 2>/dev/null || true)
    [ -n "$line" ] && echo "$line" || echo "- $rel: status unavailable"
  done <<<"$configs"
fi

echo
echo "== duplicate Claude/Codex hooks"
found=0
for settings in \
  "$root/.claude/settings.json" \
  "$root/.claude/settings.local.json" \
  "$root/.codex/hooks.json" \
  "$root/.codex/config.toml"
do
  if [ -f "$settings" ] && grep -Eq 'jaunt guard|scripts/(claude-|codex-)?guard\.sh' "$settings" 2>/dev/null; then
    echo "- $settings contains a hand-rolled Jaunt guard; remove it when the matching plugin hook is enabled"
    found=1
  fi
done
[ "$found" -eq 1 ] || echo "- no duplicate hand-rolled Jaunt guard hooks detected"

exit 0
