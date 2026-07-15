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

# Stay inside the active project/worktree. Prefer the nearest parent config so
# deeply nested packages are not hidden by the bounded recursive scan. A broad
# session cwd such as $HOME must not discover unrelated repositories below it.
root=$(cd "$root" 2>/dev/null && pwd -P) || exit 0
project_root=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null || true)
if [ -n "$project_root" ]; then
  project_root=$(cd "$project_root" 2>/dev/null && pwd -P) || project_root=""
fi
cursor="$root"
workspace_root=""
while :; do
  if [ -f "$cursor/jaunt.toml" ]; then
    workspace_root="$cursor"
    break
  fi
  [ "$cursor" = "/" ] && break
  [ -n "$project_root" ] && [ "$cursor" = "$project_root" ] && break
  cursor=$(dirname "$cursor")
done
if [ -n "$workspace_root" ]; then
  root="$workspace_root"
elif [ -n "$project_root" ] && [ -d "$project_root" ]; then
  : # Keep descendant discovery anchored at the session cwd.
else
  exit 0
fi

uv_cache="${UV_CACHE_DIR:-${PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/jaunt-plugin-uv-cache-${UID:-user}}}}"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

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

status_json() {
  local dir="$1"
  local secs="$2"
  shift 2
  (
    cd "$dir" &&
      UV_CACHE_DIR="$uv_cache" run_timeout "$secs" \
        bash "$script_dir/resolve-workspace.sh" --run . status "$@" --json --progress none
  ) 2>/dev/null
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
    codes = [str(item.get("code") or "diagnostic") for item in diagnostics if isinstance(item, dict)]
    suffix = " [" + ", ".join(dict.fromkeys(codes[:3])) + "]" if codes else ""
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
parts = [f"{kind}: {count} ({labels.get(kind, kind)})" for kind, count in sorted(counts.items())]
line = f"{len(fresh)} fresh"
if stale:
    line += f", {len(stale)} stale"
    if parts:
        line += " [" + "; ".join(parts) + "]"
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
if status.get("ok") is not True:
    error = status.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    diagnostics = error.get("diagnostics", []) if isinstance(error, dict) else []
    codes = [str(item.get("code") or "diagnostic") for item in diagnostics if isinstance(item, dict)]
    suffix = " [" + ", ".join(dict.fromkeys(codes[:3])) + "]" if codes else ""
    print("TS status unavailable" + (f": {message}" if message else "") + suffix)
    sys.exit(0)
targets = status.get("targets")
target = targets.get("ts", {}) if isinstance(targets, dict) else {}
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
        diagnostics.append(item)
codes = []
for item in diagnostics:
    code = str(item.get("code") or "diagnostic")
    if code not in codes:
        codes.append(code)
suffix = " [" + ", ".join(codes[:3]) + "]" if codes else ""
print(f"TS: {unbuilt_count} unbuilt, {invalid_count} invalid, {len(diagnostics)} diagnostics{suffix}")
' 2>/dev/null
}

configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' \
  -not -path "$root/.claude/worktrees/*" \
  -not -path "$root/.codex/worktrees/*" 2>/dev/null | sort)
if [ -n "$configs" ]; then
  bounded_configs=""
  while IFS= read -r cfg; do
    cfg_root=$(git -C "$(dirname "$cfg")" rev-parse --show-toplevel 2>/dev/null || true)
    if [ -n "$cfg_root" ]; then
      cfg_root=$(cd "$cfg_root" 2>/dev/null && pwd -P) || cfg_root=""
    fi
    if [ -n "$project_root" ]; then
      [ "$cfg_root" = "$project_root" ] || continue
    else
      [ -z "$cfg_root" ] || continue
    fi
    [ -z "$bounded_configs" ] || bounded_configs+=$'\n'
    bounded_configs+="$cfg"
  done <<<"$configs"
  configs="$bounded_configs"
fi
[ -n "$configs" ] || exit 0
total=$(printf '%s\n' "$configs" | wc -l | tr -d ' ')
limit=6
count=0

echo "Jaunt workspace freshness (run build/check/status from the workspace containing jaunt.toml):"
while IFS= read -r cfg; do
  count=$((count + 1))
  if [ "$count" -gt "$limit" ]; then
    remaining=$((total - limit))
    echo "- ...and $remaining more; inspect them with 'jaunt status --json' using the workspace runner"
    break
  fi
  dir=$(dirname "$cfg")
  rel="${dir#"$root"}"; rel="${rel#/}"; [ -n "$rel" ] || rel="."
  mode=$(workspace_mode "$cfg")
  ts_out=""
  if [ "$mode" = "ts" ]; then
    out=$(status_json "$dir" 6 --language ts || true)
    ts_out="$out"
  else
    out=$(status_json "$dir" 6 || true)
    if [ "$mode" = "mixed" ]; then
      ts_out=$(status_json "$dir" 6 --language ts || true)
    fi
  fi
  line=$(printf '%s' "$out" | summarize_status || true)
  [ -n "$line" ] || line="status unavailable"
  if [ "$mode" = "ts" ] || [ "$mode" = "mixed" ]; then
    ts_line=$(printf '%s' "$ts_out" | summarize_typescript || true)
    [ -n "$ts_line" ] || ts_line="TS status unavailable"
    line="$line; $ts_line"
  fi
  echo "- $rel: $line"
done <<<"$configs"
exit 0
