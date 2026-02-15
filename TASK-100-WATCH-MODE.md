---
id: "100"
title: Watch Mode
status: done
priority: 6
effort: medium
depends: ["060"]
---

# TASK-100: Watch Mode

## Problem

No way to automatically rebuild when spec files change. Users (and agents)
must manually re-run `jaunt build` after every edit. This breaks the iterative
development loop.

## Deliverables

### `jaunt watch`

```bash
jaunt watch                   # build on spec file change
jaunt watch --test            # build + test on change
jaunt watch --json            # emit JSON events per rebuild
```

### Behavior

- Watch all Python files under configured `source_roots` and `test_roots`
- Trigger rebuild when a file containing `@jaunt.magic` or `@jaunt.test` changes
- Debounce rapid changes (e.g., 200ms window) to avoid redundant builds
- Only rebuild stale modules (leverage existing incremental build logic)
- Show clear output on each rebuild cycle:
  ```
  [watch] change detected: src/my_app/specs.py
  [watch] building 1 module...
  [watch] done (0.8s)
  ```

### Dependencies

- Use `watchfiles` (pure Python, async-friendly) — add as optional dep
- Avoid `watchdog` (C extension, heavier)

```toml
[project.optional-dependencies]
watch = ["watchfiles>=1.0.0"]
```

## Implementation Notes

- `jaunt watch` is essentially a loop: detect changes → call `cmd_build()`
  (or `cmd_test()`) → wait for next change
- The incremental build system already handles "only rebuild what's stale" —
  watch mode just needs to re-trigger it
- Consider `Ctrl+C` handling for clean shutdown
- JSON mode should emit one JSON object per rebuild cycle
