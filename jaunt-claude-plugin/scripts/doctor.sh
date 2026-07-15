#!/usr/bin/env bash
# Deterministic, read-only Jaunt health check. Never builds or calls a model.
# Each check fails open so a broken tool cannot block the surrounding agent.
set -u

root="${JAUNT_WORKSPACE_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}"
if [ -d "$root" ]; then
  root=$(cd "$root" 2>/dev/null && pwd)
else
  root="$PWD"
fi
uv_cache="${UV_CACHE_DIR:-${PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/jaunt-plugin-uv-cache-${UID:-user}}}}"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
plugin_root=$(cd "$script_dir/.." && pwd)

if [ -f "$plugin_root/.claude-plugin/plugin.json" ]; then
  plugin_host="Claude"
  hook_settings=(
    "$root/.claude/settings.json"
    "$root/.claude/settings.local.json"
  )
else
  plugin_host="Codex"
  hook_settings=(
    "$root/.codex/hooks.json"
    "$root/.codex/config.toml"
  )
fi

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

workspace_mode() {
  python3 - "$1" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    data = tomllib.loads(open(sys.argv[1], "rb").read().decode())
except Exception:
    print("unknown")
    raise SystemExit
target = data.get("target")
if not isinstance(target, dict):
    print("py")
    raise SystemExit
has_py = isinstance(target.get("py"), dict)
has_ts = isinstance(target.get("ts"), dict)
print("mixed" if has_py and has_ts else "ts" if has_ts else "py")
PY
}

status_timeout="${JAUNT_PLUGIN_STATUS_TIMEOUT_SECONDS:-120}"

status_json() {
  local dir="$1"
  shift
  local output status
  output=$(
    (
      cd "$dir" &&
        UV_CACHE_DIR="$uv_cache" run_timeout "$status_timeout" \
          bash "$script_dir/resolve-workspace.sh" --run . status "$@" --json --progress none
    ) 2>/dev/null
  )
  status=$?
  if [ -n "$output" ]; then
    printf '%s' "$output"
  elif [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    printf '{"command":"status","ok":false,"error":{"kind":"timeout","message":"status timed out after %s seconds"}}\n' "$status_timeout"
  elif [ "$status" -ne 0 ]; then
    printf '{"command":"status","ok":false,"error":{"kind":"process","message":"status command exited %s"}}\n' "$status"
  fi
}

summarize_status() {
  python3 -c '
import collections, json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if status.get("ok") is not True:
    error = status.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    diagnostics = error.get("diagnostics", []) if isinstance(error, dict) else []
    details = [str(item.get("code") or "diagnostic") for item in diagnostics if isinstance(item, dict)]
    suffix = " [" + ", ".join(dict.fromkeys(details[:3])) + "]" if details else ""
    print("status unavailable" + (f": {message}" if message else "") + suffix)
    sys.exit(0)
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
    "stub": "deterministic .pyi re-emission",
}
line = f"{len(fresh)} fresh"
if stale:
    reasons = [f"{kind}: {count} ({labels.get(kind, kind)})" for kind, count in sorted(counts.items())]
    line += f", {len(stale)} stale [" + "; ".join(reasons) + "]"
if orphans:
    line += f", {len(orphans)} orphans (run jaunt clean --orphans with the selected runner)"
print(line)
' 2>/dev/null
}

summarize_typescript() {
  python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    sys.exit(0)
targets = status.get("targets")
target = targets.get("ts", {}) if isinstance(targets, dict) else {}
target_failed = isinstance(target, dict) and target.get("ok") is False
if status.get("ok") is not True and (not target or target_failed):
    error = status.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    diagnostics = error.get("diagnostics", []) if isinstance(error, dict) else []
    details = []
    for item in diagnostics[:3]:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("code") or "diagnostic")
        if item.get("message"):
            detail += ": " + str(item["message"])
        details.append(detail)
    suffix = " [" + "; ".join(details) + "]" if details else ""
    compiler_unavailable = any(str(item.get("code") or "").startswith("JAUNT_TS_COMPILER") for item in diagnostics if isinstance(item, dict))
    label = "worker/compiler unavailable" if compiler_unavailable else "TypeScript status unavailable"
    print(label + (f": {message}" if message else "") + suffix)
    sys.exit(0)
unbuilt = target.get("unbuilt", status.get("unbuilt", []))
invalid = target.get("invalid", status.get("invalid", {}))
unbuilt_count = len(unbuilt) if isinstance(unbuilt, (list, dict)) else 0
invalid_count = len(invalid) if isinstance(invalid, (list, dict)) else 0
raw = list(status.get("diagnostics") or [])
if isinstance(invalid, dict):
    for items in invalid.values():
        if isinstance(items, list):
            raw.extend(item for item in items if isinstance(item, dict))
diagnostics = []
seen = set()
for item in raw:
    if not isinstance(item, dict):
        continue
    key = (str(item.get("code") or "diagnostic"), str(item.get("message") or ""), str(item.get("path") or ""))
    if key not in seen:
        seen.add(key)
        diagnostics.append(key)
details = []
for code, message, path in diagnostics[:3]:
    detail = code + (f": {message}" if message else "")
    if path:
        detail += f" ({path})"
    details.append(detail)
suffix = " [" + "; ".join(details) + "]" if details else ""
print(f"worker/compiler ready; {unbuilt_count} unbuilt, {invalid_count} invalid, {len(diagnostics)} diagnostics{suffix}")
' 2>/dev/null
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
if [ -f "$root/jaunt.toml" ]; then
  jaunt_version=$(cd "$root" 2>/dev/null && UV_CACHE_DIR="$uv_cache" run_timeout 30 bash "$script_dir/resolve-workspace.sh" --run . --version 2>&1)
  jaunt_version_status=$?
else
  jaunt_version=""
  jaunt_version_status=1
fi
if [ "$jaunt_version_status" -eq 0 ] && [ -n "$jaunt_version" ]; then
  echo "- jaunt: $jaunt_version"
else
  echo "- jaunt: unavailable (install jaunt, use a uv project, or install uvx)"
fi
python_version=$(python3 --version 2>&1 || true)
echo "- python3: ${python_version:-NOT FOUND}"
node_version=$(run_timeout 10 node --version 2>/dev/null || true)
echo "- node: ${node_version:-NOT FOUND}"
npm_version=$(run_timeout 10 npm --version 2>/dev/null || true)
echo "- npm: ${npm_version:-NOT FOUND}"

echo
echo "== workspace health"
configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' \
  -not -path "$root/.claude/worktrees/*" \
  -not -path "$root/.codex/worktrees/*" 2>/dev/null | sort)
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
    mode=$(workspace_mode "$cfg")
    ts_out=""
    if [ "$mode" = "ts" ]; then
      out=$(status_json "$dir" --language ts || true)
      ts_out="$out"
    else
      out=$(status_json "$dir" || true)
      if [ "$mode" = "mixed" ]; then
        ts_out="$out"
      fi
    fi
    line=$(printf '%s' "$out" | summarize_status || true)
    [ -n "$line" ] && echo "- $rel: $line" || echo "- $rel: status unavailable (invalid config, missing environment, or timeout)"
    if [ "$mode" = "ts" ] || [ "$mode" = "mixed" ]; then
      ts_line=$(printf '%s' "$ts_out" | summarize_typescript || true)
      if [ -n "$ts_line" ]; then
        echo "- $rel TypeScript: $ts_line"
      else
        echo "- $rel TypeScript: worker/compiler unavailable (check Node, npm install, @usejaunt/ts, and TypeScript >=5.8 <7)"
      fi
    fi
  done <<<"$configs"
fi

echo
echo "== duplicate $plugin_host hooks"
found=0
for settings in "${hook_settings[@]}"; do
  if [ -f "$settings" ] && grep -Eq 'jaunt guard|scripts/(claude-|codex-)?guard\.sh' "$settings" 2>/dev/null; then
    echo "- $settings contains a hand-rolled Jaunt guard; remove it when the $plugin_host plugin hook is enabled"
    found=1
  fi
done
[ "$found" -eq 1 ] || echo "- no duplicate hand-rolled $plugin_host Jaunt guard hooks detected"

exit 0
