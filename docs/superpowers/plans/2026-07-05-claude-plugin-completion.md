# Claude Code Plugin Completion (jaunt-claude-plugin 1.0.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **This run:** executed via a dynamic Workflow of opus@high subagents driving `codex exec -m gpt-5.5` at reasoning-effort medium (user directive), with a codex@high whole-branch review at the end of the workflow.

**Goal:** Complete the jaunt-claude-plugin MVP for general usage — ship the three prose/script roadmap items (`/jaunt:doctor`, `/jaunt:convert`, `first-build-reviewer` agent), harden both hooks, make the plugin installable straight from GitHub, add deterministic pytest coverage, and publish a dedicated docs page with examples.

**Architecture:** The plugin stays a pure CLI-wrapper (no MCP server — `--json` is the machine interface). Organizing principle carried through every component: *paid paths deliberate, free paths frictionless; fail-open — a broken env never blocks editing or session start.* New deterministic collection lives in shell scripts (`doctor.sh`, `guard.sh`); judgment lives in skills; adversarial first-build review lives in an agent. A repo-root `.claude-plugin/marketplace.json` makes `claude plugin marketplace add creatorrr/jaunt` work; the nested per-plugin marketplace.json is removed (one source of truth).

**Tech Stack:** Claude Code plugin format (plugin.json / hooks.json / skills / agents), bash + python3 (stdlib only: json, tomllib), pytest (existing repo suite), fumadocs MDX (docs-site).

## Global Constraints

- Plugin version: **1.0.0** in `jaunt-claude-plugin/.claude-plugin/plugin.json` AND root `.claude-plugin/marketplace.json` (must match; tested).
- **No pyproject.toml version bump** — the PyPI package does not release with this PR (user directive; release trigger fires only on pyproject.toml changes).
- All hook/skill paths use `${CLAUDE_PLUGIN_ROOT}`; never relative paths.
- Only `plugin.json` inside `jaunt-claude-plugin/.claude-plugin/`.
- Every script: `#!/usr/bin/env bash`, executable bit, fail-open (`exit 0` on any env problem for hooks), no dependencies beyond bash + python3 + uv (jq NOT assumed).
- Skill frontmatter: `name` + `description` required; description ≤1024 chars, written for auto-invocation matching.
- Skills that spend money (`convert`) get `disable-model-invocation: true`. Diagnostic/free skills stay model-invocable.
- Stub-body doctrine in all prose: `raise NotImplementedError` (ty rejects `...` under concrete return annotations); the forms are digest-equal.
- Cost taxonomy language must match `skills/working-with-jaunt/SKILL.md`: none / prose (~$0 refreeze) / structural (paid) / fingerprint (free re-stamp).
- Repo lint gates apply: `uv run ruff check .`, `uv run ruff format`, `uv run pytest -q` all green; line-length 100, py312+.
- Docs pages are MDX under `docs-site/content/docs/`; navigation via sibling `meta.json`.

## File Structure

```
.claude-plugin/marketplace.json          # NEW — repo-root marketplace (add creatorrr/jaunt)
jaunt-claude-plugin/
  .claude-plugin/plugin.json             # MODIFY — 1.0.0, homepage
  .claude-plugin/marketplace.json        # DELETE — superseded by repo-root marketplace
  hooks/hooks.json                       # MODIFY — guard via guard.sh, timeouts
  scripts/guard.sh                       # NEW — owning-project-resolving guard wrapper
  scripts/session-status.sh              # MODIFY — project cap + lower timeout
  scripts/doctor.sh                      # NEW — deterministic health collection
  scripts/resolve-project.sh             # unchanged
  skills/build/SKILL.md                  # MODIFY — wire first-build-reviewer into step 5
  skills/working-with-jaunt/SKILL.md     # unchanged
  skills/doctor/SKILL.md                 # NEW — /jaunt:doctor
  skills/convert/SKILL.md                # NEW — /jaunt:convert
  agents/first-build-reviewer.md         # NEW — adversarial first-build review agent
  README.md                              # MODIFY — install from GitHub, new component table
tests/test_claude_plugin.py              # NEW — deterministic plugin artifact tests
docs-site/content/docs/guides/claude-code-plugin.mdx   # NEW — dedicated page w/ examples
docs-site/content/docs/guides/meta.json  # MODIFY — add page after coding-agents
docs-site/content/docs/guides/coding-agents.mdx        # MODIFY — link the plugin
docs/hooks.md                            # MODIFY — plugin packages this hook
README.md                                # MODIFY — one-paragraph plugin pointer
CLAUDE.md                                # MODIFY — project-layout entry for the plugin dir
```

---

### Task 1: Manifests — plugin.json 1.0.0 + repo-root marketplace

**Files:**
- Modify: `jaunt-claude-plugin/.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json` (repo root)
- Delete: `jaunt-claude-plugin/.claude-plugin/marketplace.json`

**Interfaces:**
- Produces: plugin name `jaunt` (skill namespace `/jaunt:*`), marketplace name `jaunt-plugins`, version string `1.0.0` consumed by Task 6 tests and Task 7/8 docs.

- [ ] **Step 1: Update plugin.json**

```json
{
  "name": "jaunt",
  "version": "1.0.0",
  "description": "Spec-driven Python codegen (jaunt) support: generated-code guard hook, session freshness map, plan-first cost-aware build workflow, conversion protocol, project doctor, first-build reviewer agent, and spec-authoring knowledge.",
  "author": { "name": "Jaunt Contributors" },
  "homepage": "https://jaunt.ing/docs/guides/claude-code-plugin",
  "repository": "https://github.com/creatorrr/jaunt",
  "license": "MIT",
  "keywords": ["jaunt", "spec-driven", "code-generation", "python", "llm", "claude-code"]
}
```

- [ ] **Step 2: Create repo-root `.claude-plugin/marketplace.json`**

```json
{
  "name": "jaunt-plugins",
  "owner": { "name": "Jaunt Contributors" },
  "metadata": {
    "description": "Claude Code plugins for the Jaunt spec-driven code generation framework",
    "version": "1.0.0"
  },
  "plugins": [
    {
      "name": "jaunt",
      "source": "./jaunt-claude-plugin",
      "description": "Spec-driven Python codegen (jaunt) support: generated-code guard hook, session freshness map, plan-first cost-aware build workflow, conversion protocol, project doctor, first-build reviewer agent, and spec-authoring knowledge.",
      "version": "1.0.0",
      "author": { "name": "Jaunt Contributors" },
      "category": "development",
      "keywords": ["jaunt", "spec-driven", "code-generation", "python", "llm", "claude-code"]
    }
  ]
}
```

- [ ] **Step 3: Delete the nested marketplace.json; verify with `python3 -m json.tool` on both files; commit** (`feat(plugin): 1.0.0 manifests + repo-root marketplace`)

---

### Task 2: Hook hardening — guard.sh + session-status polish

**Files:**
- Create: `jaunt-claude-plugin/scripts/guard.sh` (chmod +x)
- Modify: `jaunt-claude-plugin/hooks/hooks.json`
- Modify: `jaunt-claude-plugin/scripts/session-status.sh`

**Interfaces:**
- Consumes: PreToolUse stdin JSON payload (`tool_name`, `tool_input.{file_path,path,notebook_path}`).
- Produces: guard decision JSON on stdout (pass-through from `jaunt guard`), or silent exit 0.

**Why:** the MVP ran `uv run jaunt guard` from the session cwd — in a multi-project repo the guard resolves `generated_dir` from the *wrong* config, and in non-jaunt repos every Edit paid uv startup. guard.sh resolves the OWNING project from the payload path first (same rule as resolve-project.sh), fast-exits when there is none, and runs the guard from that directory.

- [ ] **Step 1: Write `scripts/guard.sh`**

```bash
#!/usr/bin/env bash
# PreToolUse guard wrapper: resolve the owning jaunt project from the tool-call
# payload path, then run `jaunt guard` from there so generated_dir comes from
# the RIGHT config. Fail-open: any env problem exits 0 (never blocks editing).
set -u

payload=$(cat 2>/dev/null) || exit 0
[ -z "$payload" ] && exit 0

path=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = p.get("tool_input") or {}
for k in ("file_path", "path", "notebook_path"):
    v = ti.get(k)
    if v:
        print(v)
        break
' 2>/dev/null) || exit 0
[ -z "$path" ] && exit 0

dir=$(dirname "$path" 2>/dev/null) || exit 0
case "$dir" in /*) ;; *) dir="${CLAUDE_PROJECT_DIR:-$PWD}/$dir" ;; esac
while [ -n "$dir" ] && [ "$dir" != "/" ]; do
  [ -f "$dir/jaunt.toml" ] && break
  dir=$(dirname "$dir")
done
[ -f "$dir/jaunt.toml" ] || exit 0

cd "$dir" 2>/dev/null || exit 0
if command -v jaunt >/dev/null 2>&1; then
  printf '%s' "$payload" | timeout 8 jaunt guard 2>/dev/null || true
else
  printf '%s' "$payload" | timeout 8 uv run --no-sync jaunt guard 2>/dev/null || true
fi
exit 0
```

- [ ] **Step 2: Rewire hooks.json**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/guard.sh\"",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/session-status.sh\"",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 3: session-status.sh polish** — cap at 12 projects (print `- …and N more (run 'uv run jaunt status' there manually)` when exceeded); drop per-project timeout 60 → 30; everything else unchanged.

- [ ] **Step 4: Verify** — `bash -n` both scripts; feed a fake payload through guard.sh from a tmp dir with/without jaunt.toml (expect silent exit 0 without; guard JSON with, when path is under `__generated__/`). Commit (`feat(plugin): owning-project guard wrapper + session-status caps`).

---

### Task 3: `/jaunt:doctor` — deterministic health check

**Files:**
- Create: `jaunt-claude-plugin/scripts/doctor.sh` (chmod +x)
- Create: `jaunt-claude-plugin/skills/doctor/SKILL.md`

**Interfaces:**
- Produces: plain-text sectioned report on stdout (sections: `== environment`, `== projects`, `== config drift`, `== duplicate guard hooks`); the skill interprets it. Consumed verbatim by Task 6 tests only for script syntax, and by Task 8 docs as an example.

**doctor.sh checks (all read-only, fail-open per check, total runtime bounded):**
1. `== environment`: `codex` CLI present + `codex --version`; `codex login status` (or "not authenticated"); jaunt importable per project root (`uv run --no-sync jaunt --version`); python3 version.
2. `== projects`: same discovery as session-status.sh (`find -maxdepth 5 -name jaunt.toml`, same exclusions); per project: fresh/stale counts with stale-class breakdown from `status --json` (`stale_changes`), orphans (with the `jaunt clean --orphans` pointer).
3. `== config drift`: parse every `jaunt.toml` with python3 `tomllib`; canonicalize (`json.dumps(..., sort_keys=True)`) the `[codex]` table and `build.instructions` value; if >1 project and any pair differs, print WHICH projects differ and on which of the two blocks — this drift restales (re-bills) every module in the drifted project.
4. `== duplicate guard hooks`: if `$CLAUDE_PROJECT_DIR/.claude/settings.json` exists and contains the string `jaunt guard`, flag that the plugin already ships this hook and the hand-rolled one can be deleted.

**skills/doctor/SKILL.md frontmatter:** `name: doctor`, description ~"Use when a jaunt project misbehaves, before a big build, or when asked to health-check jaunt setup — checks codex auth, per-project freshness/orphans, cross-project config drift, and duplicate guard hooks. Free and deterministic (no model calls, no builds)." Body: run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"` from the repo root, then interpret each section with a fix-table (drift → make blocks byte-identical BEFORE building; orphans → `uv run jaunt clean --orphans`; not authenticated → `codex login`; duplicate hook → delete hand-rolled entry). Emphasize: doctor never spends money; it tells you what a build WOULD bill.

- [ ] Steps: write doctor.sh → `bash -n` + run it against this repo (expect environment + ≥18 projects listed + no crash) → write SKILL.md → commit (`feat(plugin): /jaunt:doctor deterministic health check`).

---

### Task 4: `/jaunt:convert` — conversion protocol skill

**Files:**
- Create: `jaunt-claude-plugin/skills/convert/SKILL.md`

**Interfaces:**
- Consumes: `scripts/resolve-project.sh` (Task 0, exists), `/jaunt:build` protocol (existing skill), `first-build-reviewer` agent name (Task 5 — refer to it as "the `jaunt` plugin's `first-build-reviewer` agent").

**Frontmatter:** `name: convert`, `disable-model-invocation: true`, `argument-hint: "[module-or-file]"`, description ~"Convert an existing handwritten Python module into a jaunt spec (docstring contract + generated body). Walks churn triage, characterization tests first, contract distillation, stub conversion, build, gate, and first-build line review. Costs real money at the build step."

**Body — the protocol (numbered phases, distilled from the mem-mcp adoption campaign / FEEDBACK findings):**
1. **Triage — is this module a good target?** Good: pure logic, self-contained, real churn, existing tests. Bad: heavy I/O orchestration, import-time side effects, module-level consumers of would-be specs, <50 LOC trivia. If bad → stop and say why.
2. **Safety net first.** Existing tests are the gate; if coverage is thin, write characterization tests against CURRENT behavior before touching the module (they must pass pre- and post-conversion, unchanged).
3. **Resolve the project.** `bash "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-project.sh" <file>`; no jaunt.toml → one project per package: `jaunt.toml` with `source_roots=["."]` at the package's import root (multi-root specs are a hard exit-2 since 1.5.1). `[codex]`/`[build].instructions` byte-identical with sibling projects.
4. **Distill the contract.** Docstring = full behavioral contract, self-contained (generation can't see sibling docstrings — inline cross-module invariants or put them in `magic_module(prompt=...)`); pin every behavior the tests assert; state mutable-state timing (read at call time vs import time).
5. **Convert to stub.** `jaunt.magic_module(__name__)` at top; each converted symbol's body → `raise NotImplementedError`; handwritten symbols stay (real bodies coexist untouched); import-time-consumed specs get per-symbol `@jaunt.magic`. Run `uv run jaunt specs` and review `newly_governed` — every entry must be intentional.
6. **Preview the spend.** `uv run jaunt status --json` — new modules are structural (paid). State the expected bill before building.
7. **Build + gate** via the `/jaunt:build` protocol (advisories verbatim; `jaunt check` exit 0; pre-existing tests unchanged; ruff/ty).
8. **First-build line review.** Dispatch the plugin's `first-build-reviewer` agent on the generated diff vs the contract — the failure class no gate catches is behavior the spec doesn't pin.
9. **Commit** spec + `__generated__/` + `.pyi` together; message notes the module converted and the bill.

Include a short "when it goes wrong" table reusing build-skill triage rows (undeclared import / exit-2 multi-root / ty errors / test failures → fix-forward through the spec, never patch the body).

- [ ] Steps: write SKILL.md → self-check frontmatter + ≤1024-char description → commit (`feat(plugin): /jaunt:convert conversion protocol`).

---

### Task 5: `first-build-reviewer` agent + build-skill wiring

**Files:**
- Create: `jaunt-claude-plugin/agents/first-build-reviewer.md`
- Modify: `jaunt-claude-plugin/skills/build/SKILL.md` (step "5. Review the `__generated__/` diff…")

**Agent file format:** markdown with YAML frontmatter `name: first-build-reviewer`, `description` (when to dispatch: after a module's FIRST successful jaunt build, to adversarially review generated code against its docstring contract), `tools: Read, Grep, Glob, Bash`. Body = system prompt:
- Inputs it expects in the dispatch prompt: spec file path, generated file path (and `.pyi`), the contract docstring(s).
- Mission: find **contract-silence divergence** — behavior present in the generated body that the docstring does not pin (defaults chosen, error types raised, edge-case handling, ordering/stability, timezone/locale/encoding assumptions, mutation vs copy, boundary conditions).
- For each finding, output: the behavior, the docstring's silence, and the ONE-LINE docstring addition that would pin it (fix-forward: the deliverable is spec edits, never body patches). Classify: DIVERGENCE-RISK (tests could pass while behavior is wrong) vs PINNED-OK.
- Explicitly forbidden: proposing edits to `__generated__/**`; restyling; performance nits unless contract-relevant.

**build/SKILL.md wiring:** in gate step 5, replace the bare "line-review the body against the contract" sentence with: on FIRST build of a module, dispatch the plugin's `first-build-reviewer` agent with spec path + generated path; apply its docstring additions (prose-class edits refreeze ~free; structural ones re-bill — say so before applying) — keep the existing contract-silence sentence as the rationale.

- [ ] Steps: write agent → wire build skill → commit (`feat(plugin): first-build-reviewer agent, wired into /jaunt:build gate`).

---

### Task 6: Deterministic pytest coverage for plugin artifacts

**Files:**
- Create: `tests/test_claude_plugin.py`

**Interfaces:**
- Consumes: final file shapes from Tasks 1–5. Repo root discovered via `Path(__file__).resolve().parents[1]`.

**Tests (stdlib only — json, tomllib not needed, subprocess for `bash -n`; follow existing test-file style):**

```python
"""Deterministic checks for the Claude Code plugin artifacts (jaunt-claude-plugin/)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "jaunt-claude-plugin"


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fields: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def test_plugin_manifest_shape():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "jaunt"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_marketplace_at_repo_root_points_at_plugin():
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["version"] == manifest["version"] == market["metadata"]["version"]
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()
    assert not (PLUGIN / ".claude-plugin" / "marketplace.json").exists()


def test_only_manifest_inside_dot_claude_plugin():
    assert [p.name for p in (PLUGIN / ".claude-plugin").iterdir()] == ["plugin.json"]


def test_hooks_reference_existing_executable_scripts():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for groups in hooks["hooks"].values()
        for group in groups
        for h in group["hooks"]
    ]
    assert commands, "hooks.json defines no commands"
    for command in commands:
        for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}([^\"' ]+)", command):
            script = PLUGIN / ref.lstrip("/")
            assert script.is_file(), f"missing {ref}"
            assert script.stat().st_mode & 0o111, f"not executable: {ref}"


def test_scripts_are_valid_bash():
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("bash unavailable")
    for script in sorted((PLUGIN / "scripts").glob("*.sh")):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_skills_and_agents_have_frontmatter():
    docs = sorted(PLUGIN.glob("skills/*/SKILL.md")) + sorted(PLUGIN.glob("agents/*.md"))
    names = set()
    for doc in docs:
        fields = _frontmatter(doc.read_text())
        assert fields.get("name"), f"{doc}: missing name"
        assert fields["name"] not in names, f"duplicate skill/agent name {fields['name']}"
        names.add(fields["name"])
        assert 0 < len(fields.get("description", "")) <= 1024, f"{doc}: bad description"
    assert {"build", "working-with-jaunt", "doctor", "convert", "first-build-reviewer"} <= names


def test_convert_skill_is_user_invoked_only():
    fields = _frontmatter((PLUGIN / "skills" / "convert" / "SKILL.md").read_text())
    assert fields.get("disable-model-invocation") == "true"
```

- [ ] Steps: write test file → `uv run pytest tests/test_claude_plugin.py -q` (all pass) → `uv run ruff check tests/test_claude_plugin.py` + `ruff format` → full `uv run pytest -q` → commit (`test: deterministic checks for the claude code plugin artifacts`).

---

### Task 7: Plugin README refresh

**Files:**
- Modify: `jaunt-claude-plugin/README.md`

Keep the voice and the organizing-principle framing. Changes:
1. Drop the "Revives the plugin dropped in #55" sentence to a single trailing "History" line; lead with what the plugin does today.
2. Component table gains rows: `skills/doctor/` (env + drift + orphans + duplicate-hook health check, free), `skills/convert/` (conversion protocol; scar: ad-hoc conversions billing before a safety net exists), `agents/first-build-reviewer.md` (scar: contract-silence divergence caught by no deterministic gate), `scripts/guard.sh` (scar: wrong-project guard config in multi-project repos).
3. Installation section becomes (GitHub marketplace first):

```bash
claude plugin marketplace add creatorrr/jaunt
claude plugin install jaunt@jaunt-plugins
```

with the from-a-clone (`claude plugin marketplace add .` at repo root) and one-session (`claude --plugin-dir ./jaunt-claude-plugin`) alternatives.
4. Roadmap section shrinks to the two genuinely-deferred items (daemon monitor notifications, cost ledger) plus the meta-wish paragraph; note doctor/convert/reviewer shipped in 1.0.0.
5. Link to the docs page: https://jaunt.ing/docs/guides/claude-code-plugin.

- [ ] Commit (`docs(plugin): README for 1.0.0 — install from GitHub, full component table`).

---

### Task 8: Docs — dedicated page + cross-links

**Files:**
- Create: `docs-site/content/docs/guides/claude-code-plugin.mdx`
- Modify: `docs-site/content/docs/guides/meta.json` (insert `"claude-code-plugin"` after `"coding-agents"`)
- Modify: `docs-site/content/docs/guides/coding-agents.mdx`
- Modify: `docs/hooks.md`
- Modify: `README.md` (repo root)
- Modify: `CLAUDE.md` (project-layout section)

**claude-code-plugin.mdx** — frontmatter `title: Claude Code Plugin`, `description: "First-party Claude Code plugin: guard hook, session freshness map, and cost-aware build/convert/doctor workflows."`. Sections, each with a concrete example:
1. **Install** — the three install paths from Task 7 (marketplace add creatorrr/jaunt is primary).
2. **What loads into a session** — the SessionStart freshness map (show a real-looking 3-project example line output incl. one STALE-with-reason and one orphan) and the always-on `working-with-jaunt` knowledge (cost taxonomy table reproduced in brief).
3. **The guard hook** — worked example: agent tries to Edit `src/app/__generated__/billing.py`, hook answers with the ask-decision + owning-spec pointer (show the JSON the hook returns); explain owning-project resolution and fail-open.
4. **`/jaunt:build`** — the four-step plan-first protocol with an example transcript: status classification (fingerprint vs structural), the "these 2 modules will bill" confirmation, advisories surfaced, check + tests gate, first-build review dispatch.
5. **`/jaunt:doctor`** — sample report output (all four sections) and what each finding means.
6. **`/jaunt:convert`** — the nine-phase protocol summarized with a worked jwt-style example (module → characterization tests → contract → stub → build → review); explicit "this step bills" callout.
7. **`first-build-reviewer`** — what contract-silence divergence is, one example finding (generated body returns naive datetime; docstring silent; one-line pin suggested).
8. **Design notes** — no MCP server (CLI `--json` is the interface), fail-open hooks, paid-deliberate/free-frictionless; duplicate hand-rolled guard cleanup note.
Ends with `Next:` link chain consistent with siblings.

**coding-agents.mdx edits:** (a) intro paragraph: after "without a plugin or a server to install", add a sentence + link — for Claude Code specifically there IS a first-party plugin packaging all of this (guard hook, freshness map, build/convert/doctor skills). (b) In "Keeping Agents Out Of `__generated__/`", add one line: the plugin ships this hook prewired with owning-project resolution.

**docs/hooks.md:** add a leading line: the `jaunt` Claude Code plugin (`jaunt-claude-plugin/`, docs at /docs/guides/claude-code-plugin) packages this hook; the snippet below is the hand-rolled equivalent.

**README.md (repo root):** in the appropriate integrations/agents area, add a short "Claude Code plugin" paragraph with the two install commands and a link to the docs page.

**CLAUDE.md:** add `jaunt-claude-plugin/  # Claude Code plugin (marketplace at .claude-plugin/marketplace.json)` to the Project Layout block.

- [ ] Steps: write mdx + meta.json + edits → verify meta.json parses and page names match filenames → commit (`docs: dedicated Claude Code plugin page + cross-links`).

---

### End gates (after all tasks, before PR)

- [ ] `uv run pytest -q` — full suite green (1332 + new).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` (CI auto-formats otherwise; run format locally first).
- [ ] docs-site build: `cd docs-site && npm ci && npm run build` — MDX must compile (background, long).
- [ ] codex@high whole-branch review (user directive: BEFORE self-review) → fix confirmed findings.
- [ ] Self-review pass (superpowers:requesting-code-review) + verify skill on the touched surfaces.
- [ ] PR: branch `feat/claude-code-plugin` → main. Title: `feat: Claude Code plugin 1.0.0 — guard + freshness hooks, build/doctor/convert skills, first-build reviewer`. No pyproject bump.

## Self-Review (plan)

- Spec coverage: complete (roadmap 1–3 shipped; 4–5 deliberately deferred and documented as such; install-from-GitHub; docs page + examples; tests; no PyPI bump). ✓
- Placeholder scan: prose-artifact tasks (3,4,5,7,8) specify exact structure + required content instead of full byte-for-byte text — deliberate, since authoring is delegated to opus@high+codex agents with full repo context; scripts/json/tests are given in full. ✓
- Consistency: version 1.0.0 everywhere; agent name `first-build-reviewer` in Tasks 4,5,6,8; skill names doctor/convert match test expectations; marketplace name `jaunt-plugins` in Tasks 1,7,8. ✓
