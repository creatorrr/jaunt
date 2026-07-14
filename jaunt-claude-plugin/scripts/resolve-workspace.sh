#!/usr/bin/env bash
# Resolve a file or directory to the nearest Jaunt workspace: the closest
# ancestor containing jaunt.toml. One workspace may route several packages.
# With --run, execute Jaunt from that workspace using the first suitable
# runner: an installed `jaunt`, a uv project environment, or `uvx jaunt`.
set -eu
uv_cache="${UV_CACHE_DIR:-${PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/jaunt-plugin-uv-cache-${UID:-user}}}}"
export UV_CACHE_DIR="$uv_cache"

run_mode=0
if [ "${1:-}" = "--run" ]; then
  run_mode=1
  shift
fi

p="${1:?usage: resolve-workspace.sh [--run] <path> [jaunt-arguments ...]}"
shift
if [ -d "$p" ]; then
  dir=$(cd "$p" && pwd)
else
  dir=$(cd "$(dirname "$p")" && pwd)
fi

while [ "$dir" != "/" ]; do
  if [ -f "$dir/jaunt.toml" ]; then
    root="$dir"
    break
  fi
  dir=$(dirname "$dir")
done

if [ ! -f "${root:-}/jaunt.toml" ]; then
  echo "no jaunt.toml found above $p" >&2
  exit 1
fi

if [ "$run_mode" -eq 0 ]; then
  echo "$root"
  exit 0
fi

cd "$root"
minimum_version=$(python3 -c '
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as handle:
        version = tomllib.load(handle).get("version")
except Exception:
    version = 2
print("1.7.1" if version == 2 else "1.6.2")
' jaunt.toml 2>/dev/null || true)
[ -n "$minimum_version" ] || minimum_version="1.7.1"
version_is_compatible() {
  python3 - "$1" "$minimum_version" <<'PY' >/dev/null 2>&1
import re, sys
match = re.search(r"(?<![0-9])(\d+)\.(\d+)\.(\d+)", sys.argv[1])
minimum = tuple(int(part) for part in sys.argv[2].split("."))
raise SystemExit(0 if match and tuple(map(int, match.groups())) >= minimum else 1)
PY
}
compatible_path_jaunt() {
  local output
  output=$(jaunt --version 2>/dev/null) || return 1
  version_is_compatible "$output"
}
compatible_uv_jaunt() {
  local output
  output=$(uv run --no-sync jaunt --version 2>/dev/null) || return 1
  version_is_compatible "$output"
}
has_uv_project() {
  local candidate="$root"
  while [ "$candidate" != "/" ]; do
    if [ -f "$candidate/pyproject.toml" ] || [ -f "$candidate/uv.lock" ]; then
      return 0
    fi
    candidate=$(dirname "$candidate")
  done
  return 1
}

if command -v jaunt >/dev/null 2>&1 && compatible_path_jaunt; then
  exec jaunt "$@"
fi
if command -v uv >/dev/null 2>&1 && has_uv_project && compatible_uv_jaunt; then
  exec uv run --no-sync jaunt "$@"
fi
if command -v uvx >/dev/null 2>&1; then
  exec uvx jaunt "$@"
fi

echo "jaunt unavailable: install it, use a uv project, or install uvx" >&2
exit 127
