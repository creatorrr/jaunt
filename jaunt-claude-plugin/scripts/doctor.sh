#!/usr/bin/env bash
# /jaunt:doctor — deterministic, read-only jaunt health check. Never spends
# money (no builds, no model calls). Fail-open per check; always exits 0.
set -u
root="${CLAUDE_PROJECT_DIR:-$PWD}"

echo "== environment"
if command -v codex >/dev/null 2>&1; then
  codex_version=$(timeout 30 codex --version 2>&1)
  codex_version_status=$?
  if [ "$codex_version_status" -eq 0 ] && [ -n "$codex_version" ]; then
    echo "- codex: $codex_version"
  else
    echo "- codex: unavailable"
  fi
  codex_auth=$(timeout 30 codex login status 2>&1)
  codex_auth_status=$?
  codex_auth_line=$(printf '%s\n' "$codex_auth" | python3 -c '
import sys
for line in sys.stdin:
    line = line.strip()
    if line:
        print(line)
        break
' 2>/dev/null || true)
  if [ "$codex_auth_status" -eq 0 ] && [ -n "$codex_auth_line" ] && printf '%s' "$codex_auth_line" | grep -viq 'not authenticated'; then
    echo "- codex auth: $codex_auth_line"
  else
    echo "- codex auth: not authenticated (run 'codex login')"
  fi
else
  echo "- codex: NOT FOUND (install the Codex CLI — builds need it)"
  echo "- codex auth: not authenticated (run 'codex login')"
fi

jaunt_version=$(cd "$root" 2>/dev/null && timeout 30 uv run --no-sync jaunt --version 2>&1)
jaunt_version_status=$?
if [ "$jaunt_version_status" -eq 0 ] && [ -n "$jaunt_version" ]; then
  echo "- jaunt: $jaunt_version"
else
  echo "- jaunt: unavailable (run 'uv sync' in the project)"
fi
python_version=$(python3 --version 2>&1 || true)
echo "- python3: $python_version"

echo
echo "== projects"
configs=$(find "$root" -maxdepth 5 -name jaunt.toml \
  -not -path '*/.venv/*' -not -path '*/node_modules/*' \
  -not -path '*/.jaunt/*' -not -path '*/.git/*' 2>/dev/null | sort)
if [ -z "$configs" ]; then
  echo "- no jaunt.toml found under $root"
else
  total=$(printf '%s\n' "$configs" | wc -l | tr -d ' ')
  limit=12
  count=0
  for cfg in $configs; do
    count=$((count + 1))
    if [ "$count" -gt "$limit" ]; then
      remaining=$((total - limit))
      echo "- …and $remaining more (run 'uv run jaunt status' there manually)"
      break
    fi
    dir=$(dirname "$cfg")
    rel="${dir#"$root"}"; rel="${rel#/}"; [ -z "$rel" ] && rel="."
    out=$(cd "$dir" && timeout 30 uv run --no-sync jaunt status --json --progress none 2>/dev/null || true)
    if [ -z "$out" ]; then
      echo "- $rel: status unavailable (run 'uv run jaunt status' there manually)"
      continue
    fi
    line=$(printf '%s' "$out" | python3 -c '
import collections
import json
import sys

try:
    s = json.load(sys.stdin)
except Exception:
    sys.exit(0)

rel = sys.argv[1]
fresh = s.get("fresh") or []
stale = s.get("stale") or []
orphans = s.get("orphans") or []
changes = s.get("stale_changes") or {}
costs = {
    "structural": "paid rebuild",
    "stub": "paid",
    "prose": "~$0 refreeze",
    "fingerprint": "free re-stamp",
}

line = f"- {rel}: {len(fresh)} fresh"
if stale:
    counts = collections.Counter(str(changes.get(m) or "structural") for m in stale)
    parts = []
    for cls in ("structural", "fingerprint", "prose", "stub"):
        if counts.get(cls):
            parts.append(f"{cls}: {counts[cls]} ({costs[cls]})")
    for cls in sorted(k for k in counts if k not in costs):
        parts.append(f"{cls}: {counts[cls]}")
    line += f", {len(stale)} stale"
    if parts:
        line += " [" + ", ".join(parts) + "]"
if orphans:
    line += f", {len(orphans)} orphans (run '\''uv run jaunt clean --orphans'\'')"
print(line)
' "$rel" 2>/dev/null || true)
    if [ -n "$line" ]; then
      echo "$line"
    else
      echo "- $rel: status unavailable (run 'uv run jaunt status' there manually)"
    fi
  done
fi

echo
echo "== config drift"
python3 - "$root" $configs <<'PY' || echo "- config drift check unavailable"
import json
import os
import sys
import tomllib

try:
    root = sys.argv[1]
    configs = sys.argv[2:]
    groups: dict[str, list[str]] = {}
    values: dict[str, dict[str, object]] = {}

    for cfg in configs:
        try:
            with open(cfg, "rb") as f:
                config = tomllib.load(f)
        except Exception:
            continue

        project = os.path.dirname(cfg)
        try:
            rel = os.path.relpath(project, root)
        except Exception:
            rel = project
        if rel == ".":
            rel = "."

        value = {
            "codex": config.get("codex") or {},
            "build.instructions": (config.get("build") or {}).get("instructions"),
        }
        signature = json.dumps(value, sort_keys=True)
        groups.setdefault(signature, []).append(rel)
        values[signature] = value

    if sum(len(paths) for paths in groups.values()) < 2:
        print("- single project (or none) — no cross-project drift possible")
    elif len(groups) == 1:
        n = sum(len(paths) for paths in groups.values())
        print(f"- OK: [codex] and build.instructions byte-identical across {n} projects")
    else:
        print(
            "- DRIFT: [codex]/build.instructions differ across projects — this restales "
            "(re-bills) every module in the drifted project."
        )
        differing: list[str] = []
        codex_values = {json.dumps(v["codex"], sort_keys=True) for v in values.values()}
        instructions_values = {
            json.dumps(v["build.instructions"], sort_keys=True) for v in values.values()
        }
        if len(codex_values) > 1:
            differing.append("[codex]")
        if len(instructions_values) > 1:
            differing.append("build.instructions")

        for signature, paths in sorted(groups.items(), key=lambda item: sorted(item[1])):
            del signature
            print("  - group: " + ", ".join(sorted(paths)))
        if differing:
            print("  differing blocks: " + ", ".join(differing))
except Exception:
    print("- config drift check unavailable")
PY

echo
echo "== duplicate guard hooks"
settings="$root/.claude/settings.json"
if [ -f "$settings" ] && grep -q 'jaunt guard' "$settings" 2>/dev/null; then
  echo "- $settings contains a hand-rolled 'jaunt guard' hook — the jaunt plugin already ships this PreToolUse guard; delete the hand-rolled entry to avoid double-running."
else
  echo "- no duplicate hand-rolled guard hook detected"
fi

exit 0
