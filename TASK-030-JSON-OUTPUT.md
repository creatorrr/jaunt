---
id: "030"
title: Machine-Readable JSON Output
status: done
priority: 2
effort: small
depends: []
---

# TASK-030: Machine-Readable JSON Output

## Problem

All output was human-formatted stderr text. Agents couldn't parse it.

## Solution (Implemented)

- Added `--json` flag to both `build` and `test` commands
- In JSON mode:
  - Progress bars are suppressed
  - Structured JSON is emitted to stdout via `_emit_json()`
  - Errors still go to stderr
- Build output shape:
  ```json
  {"command": "build", "ok": true, "generated": [...], "skipped": [...], "failed": {}}
  ```
- Test output shape:
  ```json
  {"command": "test", "ok": true, "exit_code": 0}
  ```
- Error output shape:
  ```json
  {"command": "build", "ok": false, "error": "..."}
  ```
- Tests added: `test_cli_json_mode.py`
