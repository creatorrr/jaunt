#!/usr/bin/env bash
# PreToolUse hook: block Write/Edit operations targeting __generated__/ files.
# These files are managed by Jaunt and should not be edited directly.

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')

if [[ "$file_path" == *"__generated__/"* ]]; then
  echo '{"decision":"block","reason":"Do not edit __generated__/ files directly. Modify the spec stub and run jaunt build (or /jaunt-build) to regenerate."}'
  exit 2
else
  exit 0
fi
