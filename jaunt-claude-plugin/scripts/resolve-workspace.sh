#!/usr/bin/env bash
# Resolve a file or directory to the nearest Jaunt workspace: the closest
# ancestor containing jaunt.toml. One workspace may route several packages.
set -eu

p="${1:?usage: resolve-workspace.sh <path>}"
if [ -d "$p" ]; then
  dir=$(cd "$p" && pwd)
else
  dir=$(cd "$(dirname "$p")" && pwd)
fi

while [ "$dir" != "/" ]; do
  if [ -f "$dir/jaunt.toml" ]; then
    echo "$dir"
    exit 0
  fi
  dir=$(dirname "$dir")
done

echo "no jaunt.toml found above $1" >&2
exit 1
