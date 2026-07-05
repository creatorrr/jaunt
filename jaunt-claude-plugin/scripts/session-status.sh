#!/usr/bin/env bash
# SessionStart hook: inject a one-line jaunt freshness map per project.
# Fail-open by design — a broken env must never block session start.
set -u

root="${CLAUDE_PROJECT_DIR:-$PWD}"

configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' 2>/dev/null | sort)
[ -z "$configs" ] && exit 0

echo "jaunt projects in this repo — run jaunt build/check/status from the OWNING directory:"
for cfg in $configs; do
  dir=$(dirname "$cfg")
  rel="${dir#"$root"}"; rel="${rel#/}"; [ -z "$rel" ] && rel="."
  out=$(cd "$dir" && timeout 60 uv run --no-sync jaunt status --json --progress none 2>/dev/null)
  if [ -z "$out" ]; then
    echo "- $rel: status unavailable (run 'uv run jaunt status' there manually)"
    continue
  fi
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rel = sys.argv[1]
fresh, stale, orphans = s.get("fresh") or [], s.get("stale") or [], s.get("orphans") or []
changes = s.get("stale_changes") or {}
line = f"- {rel}: {len(fresh)} fresh"
if stale:
    parts = []
    for m in stale:
        why = changes.get(m)
        parts.append(f"{m} ({why})" if why else m)
    line += f", {len(stale)} STALE: " + "; ".join(str(p) for p in parts)
    line += " — fingerprint-only staleness re-stamps free; structural staleness is a paid rebuild"
if orphans:
    line += f", {len(orphans)} orphans (jaunt clean --orphans)"
print(line)
' "$rel"
done
exit 0
