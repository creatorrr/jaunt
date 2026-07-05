---
name: working-with-jaunt
description: Use when reading or editing a jaunt spec module (jaunt.magic_module / @jaunt.magic), anything under __generated__/, a jaunt.toml, or generated .pyi stubs — or when jaunt build/check/status appears in the task. Explains what each kind of edit costs, staleness semantics, and the fix-forward rules.
---

# Working with jaunt

Jaunt is spec-driven Python codegen: a docstring-contract stub IS the spec;
`jaunt build` generates the body into `__generated__/` and swaps it in at
import time. Humans and agents work at the spec level.

## Iron rules

1. **Never edit `__generated__/**` or generated `.pyi` stubs.** Edit the spec
   docstring, rebuild, review the diff, commit spec + generated + `.pyi`
   together. Hand patches die on the next regen.
2. **Fix forward through the spec.** Generated code failing tests means the
   docstring didn't pin the behavior — add the missing rule and rebuild.
3. **Run jaunt from the owning project directory.** Multi-project repos have
   one `jaunt.toml` per adopted package. Resolve first:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-project.sh" <file>` → cd there.
4. **Trust fresh specs.** When `jaunt status` says fresh, the docstring IS the
   behavior. Read `__generated__/` only when reviewing a build diff or
   debugging a suspected generation defect.

## What an edit costs (digest taxonomy)

| You change | Digest class | Consequence |
|---|---|---|
| Formatting, comments, whitespace | none (AST-based) | Nothing — no restale |
| Docstring prose, same meaning | prose | Semantic-gate refreeze, ~$0 |
| Docstring behavior, signature, stub shape, constants, `magic_module(prompt=...)` | structural | **Paid rebuild** (model call; $1–$20+ per module) |
| jaunt version bump, `[codex]` or `[build].instructions` change | fingerprint | Free model-less re-stamp on next build |

Check before you spend: `uv run jaunt status --json` — `stale_changes` tells
you which class each stale module is.

## Authoring specs

- Stub body is `raise NotImplementedError` — not `...` (ty rejects empty
  bodies on annotated functions; the forms are digest-equal).
- Module style (≥1.4): one `jaunt.magic_module(__name__)` at the top; every
  bare docstring-contract stub below is governed. Handwritten symbols (real
  bodies, or any non-jaunt decorator) coexist untouched. Sealed methods
  inside a whole-class spec: `@jaunt.sig`.
- `@jaunt.magic()` is the per-symbol escape hatch: decorated symbols that
  should be governed, or specs consumed at import time. Module-level code
  that calls/instantiates a governed spec sees the pre-rebind stub — move it
  into a function or mark that spec `@jaunt.magic`.
- **Contracts must be self-contained.** Generation cannot see sibling
  modules' docstrings; inline cross-module invariants into the contract or
  the `magic_module(prompt=...)`. If the contract depends on mutable state
  (env vars, monkeypatched module attrs), say so — it must be read at call
  time, not import time.
- Shared generation guidance lives ONCE in `jaunt.toml [build].instructions`;
  module-wide guidance in `magic_module(prompt=...)`; per-symbol `prompt=`
  only for genuinely symbol-specific hints.

## Multi-project repos

Until per-module root resolution lands (queued for jaunt 1.6), one jaunt
project per adopted package — specs spanning multiple source roots are a
hard exit-2 error (≥1.5.1). The `[codex]` and `[build].instructions` blocks
must stay **byte-identical** across the repo's jaunt.toml files: both feed
the generation fingerprint, and drift restales the project — but it
re-stamps free on the next build when specs are unchanged (only a paired
structural/prose edit bills).

## Command cheat sheet

```bash
uv run jaunt status --json     # stale modules + why (deterministic, free)
uv run jaunt check             # CI drift gate; exit 4 = stale or orphaned
uv run jaunt clean --orphans   # remove artifacts whose spec was deleted
uv run jaunt log -n 20         # committed change journal
uv run jaunt specs             # spec inventory + dependency graph
uv run jaunt instructions      # project-aware primer
```

Exit codes: 0 ok · 2 config/discovery · 3 generation · 4 check/test · 5 wait timeout.

**Advisories** (printed at the end of builds) are the highest-signal output
jaunt produces — spec ambiguities or suspected dep bugs the generator noticed
while implementing. Always read them and surface them to the user verbatim.
