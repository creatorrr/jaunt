---
id: "060"
title: CLI Ergonomics (init, clean, status)
status: done
priority: 3
effort: medium
depends: ["030"]
---

# TASK-060: CLI Ergonomics

## Problem

The tool only has `build` and `test`. No `init`, no `clean`, no `status`.
New users have no onboarding path, debugging stale state is manual, and
there's no way to inspect the build graph without running a build.

## Deliverables

### `jaunt init`

Generate a starter `jaunt.toml` and an example spec file. Detect source layout.

```bash
jaunt init                    # interactive
jaunt init --layout src       # src/pkg/ layout
jaunt init --layout flat      # pkg/ at root
```

- Detect existing `pyproject.toml` to infer project name and source roots
- Generate a minimal spec file with `@jaunt.magic` example
- Create `jaunt.toml` with sensible defaults for detected layout
- Support `--provider openai|anthropic` to pre-configure

### `jaunt clean`

Delete all `__generated__/` directories.

```bash
jaunt clean                   # remove all generated files
jaunt clean --dry-run         # show what would be deleted
```

- Walk configured `source_roots` and `test_roots`
- Remove `__generated__/` dirs (respecting configured `generated_dir` name)
- Report what was deleted

### `jaunt status`

Show which modules are stale, current, or errored.

```bash
jaunt status
jaunt status --json           # for agent consumption
```

JSON output shape:
```json
{
  "modules": {
    "my_app.specs": {"status": "stale", "reason": "spec_changed"},
    "my_app.utils": {"status": "current", "digest": "sha256:abc123"}
  }
}
```

## Implementation Notes

- `init` and `clean` are straightforward — no LLM calls, no async
- `status` reuses `detect_stale_modules()` from `builder.py`
- All three should support `--json` output (depends on TASK-030)
- Add to `_build_parser()` subcommands in `cli.py`
