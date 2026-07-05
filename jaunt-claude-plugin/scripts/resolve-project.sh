#!/usr/bin/env bash
# Resolve a file or directory to its OWNING jaunt project directory
# (nearest ancestor containing jaunt.toml). In a multi-project repo,
# running jaunt from the wrong directory routes output/check against
# the wrong roots — always resolve first, then cd there.
set -eu

p="${1:?usage: resolve-project.sh <path>}"
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
