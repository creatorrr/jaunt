# Building With Coding Agents — A Principles Framework

**Date:** 2026-06-29
**Status:** Living reference. Doubles as a roadmap compass for jaunt.
**Shape:** Hybrid — three altitudes (developer · tool · factory) over one set of
cross-cutting laws, with the unresolved tensions kept honest rather than flattened.

---

## 0. The throughline

> **When generation becomes cheap and non-deterministic, value migrates to two
> scarce, durable things: the _specification_ of intent and the _deterministic
> verification_ of output. Everything else is downstream of that.**

This is not a 2025 observation; it is an old one that agents finally made
unavoidable. Jack Reeves argued in 1992 that *source code is the design* and that
"building" software (compilation) is nearly free — so the expensive, valuable work
was always design, never production. Fred Brooks separated *essential* complexity
(the irreducible conceptual work) from *accidental* complexity (the incidental
friction of expressing it) and warned there is no silver bullet for the former.
Coding agents are the largest accidental-complexity reducer we have ever shipped:
they make the *typing* nearly free. What they cannot do is make the *deciding* free.

So the bill moves — and the 2025–26 data is *consistent with* that shift, not proof
of it. Faros telemetry reports teams adopting agents merge far more changes (~98% more
PRs) while review time rises ~91% — but the same report finds org-level outcomes flat,
and the 2025 DORA report frames AI as an *amplifier*: throughput and product
performance up, delivery stability down. A controlled trial (METR) found experienced
OSS developers ~19% *slower* with AI while *feeling* ~20% faster — though METR's own
Feb-2026 update warns that result is biased by task/developer opt-out and may now
*understate* speedup, so treat it as an early-2025 mature-codebase finding, not a
standing verdict. Read together, the honest claim is narrow: generation got cheap,
specification and verification did not, and the constraint moved downstream. Build
everything — your codebase, your tools, your organization — around protecting and
scaling those two.

### The three altitudes

The same idea recurs at three zoom levels, which are the three parts of this
document:

| Altitude | Who/what | This document's Part |
|---|---|---|
| **Developer** | one human + agent + a codebase | Part 1 |
| **Tool** | a framework that encodes the practices (jaunt) | Part 2 |
| **Factory** | an organization, many agents, the pipeline-as-product | Part 3 |

### The cross-cutting laws (named once, threaded throughout)

| # | Law | One line | Small example |
|---|---|---|---|
| **L1** | Value migrates to spec + verification | The two things worth scarce attention when typing is free. | A failing test handed to the agent does more than a paragraph describing the bug. |
| **L2** | The spec is the durable artifact; code is a projection | Maintain intent, not just output. | Edit a `@jaunt.magic` docstring; the `__generated__/` body is rebuilt from it. |
| **L3** | Push work down into deterministic layers | Spend the model only on the irreducible residue. | Strip formatting before deciding a spec "changed," so a reformat triggers no work. |
| **L4** | The model proposes; deterministic checks dispose | A wrong model verdict should cost money, never correctness. | `jaunt check` reruns committed tests with no API key — the model never gives the verdict. |
| **L5** | Specify *what*, not *how* | Over-spec kills design latitude; under-spec passes wrong code. | "Return results sorted by score, descending" — not `sorted(key=...)`. |
| **L6** | Architecture is an amplifier | Bounded units multiply quality; tangle multiplies *confident* mistakes. | Ten focused files let the agent read only what it edits; one 2k-line module forces guessing. |
| **L7** | Verification capacity must scale with generation capacity | Or the constraint just moves to review. | Merge 10× the PRs and it's review capacity, not generation, that decides throughput. |
| **L8** | Keep the human at the gate as director, not author | Taste = knowing what to trust. | You approve the contract and the diff's intent; you don't hand-type the body. |
| **L9** | Pin the artifact, not the sampler | LLMs aren't reproducible; regenerate only on input change. | The build re-runs only when the spec, model, or prompt-template digest changes. |
| **L10** | Standardize the path, not the thought | Golden paths raise the floor without capping the ceiling. | `jaunt init` drops every project onto the same validated layout. |
| **L11** | Rewrite from the spec, not the code | Regeneration is cheap now — and as safe as your contract is complete. | Point a newer model at the unchanged docstring; the committed battery proves the rewrite. |
| **L12** | Treat everything the model reads and pulls as untrusted | Prompt injection and slopsquatting are input, not edge cases. | The build fails if generated code imports a package absent from your declared deps. |
| **L13** | Make engineering compound | Each task should leave the system better at the next one. | Every fixed bug becomes a battery case, so it can't silently regress. |

### How to use this document

These principles are not the end product; they are the **durable source from which
concrete guidance for an implementer model harness is compiled.** The prompts, skills,
hooks, and rules that drive a coding agent should be *derived* from these laws and
re-derived as models change — not hand-written and frozen. That division of labor is
deliberate: the hardest calls (most of all L5's over-spec/under-spec line) are
irreducibly matters of taste and architecture with no universal rule, so we encode
principles a harness can apply *in context* rather than hardcoding answers that won't
survive the next codebase or model. When a tension below has no clean resolution, that
is a feature — it marks where judgment, not a rule, must live.

---

## 1. Building with coding agents (developer altitude)

*Dominant laws at this altitude: **L1, L3, L4, L6, L8** — and where L11–L13 first
surface. The unit is one engineer directing one or a few agents inside a real
codebase.*

### 1.1 Give the agent a check it can run — that is the whole loop. (L4)
The single highest-leverage move is handing the agent a deterministic pass/fail
signal it can run itself: a test, a build, a type-check, a screenshot diff. Once such
a signal exists, the agent iterates to green without you in the loop for every step.
Everything else in agentic coding is in service of making that loop tight and
trustworthy.
**Tension:** a check the agent can run is also a check the agent can *game* —
retrofitted tests just bless whatever the agent already did. The signal is only as
honest as it was before the agent saw the code.

### 1.2 Tests are the spec; "first run the tests." (L1, L4)
Simon Willison's four-word prompt does three things at once: forces the agent to
discover the project, signals its shape, and puts it in a verification mindset. When
writing code is cheap, the test suite is where trust is actually stored — it is the
executable part of the specification.
**Tension:** example-based tests under-constrain (many wrong programs pass);
property-based tests constrain far more but cover narrower surface and cost more to
write. Most teams sit in the gap and call it covered.

### 1.3 Treat context as a curated budget; load it just-in-time. (L6-adjacent)
Model performance degrades as the window fills — every token competes for attention.
Anthropic's guidance is to find "the smallest set of high-signal tokens," hand the
agent lightweight identifiers (paths, queries) and let it pull detail at runtime
rather than pre-loading everything. Keep the always-on instruction file (CLAUDE.md /
AGENTS.md) small and near-universal; bloat there is paid every turn.
**Tension:** too little context is the *other* failure mode — at mabl, context drift
caused ~40% of task failures until per-repository context docs dropped it below 5%.
The skill is curation, not minimization.

### 1.4 Prefer deterministic enforcement over advisory prose. (L4)
A `PostToolUse` hook that runs the tests after every edit *guarantees* the action; a
sentence in CLAUDE.md only *suggests* it. Encode invariants where they fire
mechanically — hooks, the build graph, linters, type checks — not in prose the model
may or may not honor under context pressure.
**Tension:** mechanical enforcement is rigid by construction; over-constrain and you
spend your day fighting guardrails on the legitimate exceptions they cannot see.

### 1.5 Make intent the durable artifact; don't throw away the prompt. (L2)
Version-controlling the code while discarding the spec that produced it is backwards:
the spec carries the *why* that raw code throws away, and that "why" is what rots into
tech debt. Keep a maintained specification (with canonical examples that become
tests). Böckeler/Fowler's taxonomy names the levels — **spec-first** (spec kicks off,
then discarded), **spec-anchored** (spec maintained beside editable code),
**spec-as-source** (code is regenerated, `DO NOT EDIT`). A spec is also its **negative
space**: the non-goals, forbidden dependencies, performance bounds, and the things the
agent must *ask about* rather than infer. Agents confidently fill silence (Part 3);
the unsaid is where they go wrong.
**Tension:** the staleness fork is real and unsolved. Spec-first lets the spec rot
into stale docs — the exact problem SDD claims to fix. Spec-as-source forbids
hand-editing code, which many teams reject outright. And L2's "code is a projection" is
literally true only in spec-as-source systems — in normal software, production code,
schema migrations, data, telemetry, and incident history are each canonical in their
own right. The durable rule is to keep intent maintained *alongside* code, not to
demote code everywhere.

### 1.6 Force intent explicit before code: Spec → Plan → Tasks → Implement. (L1)
The structured progression (GitHub Spec Kit; Kiro's requirements/design/tasks) exists
to catch ambiguity when it is cheap — on the page, before it is multiplied across an
implementation. Phase gates are where a human's judgment is highest-leverage.
**Tension:** push specificity far enough and the spec approaches the complexity of the
code it replaces — "the return of waterfall." A spec precise enough to regenerate from
reliably may cost as much as just writing it.

### 1.7 Architect for AI-readability: small, cohesive, vertical slices. (L6)
Bounded modules with clear interfaces let an agent reason depth-first without loading
the whole repo, and they let concurrent agents work with fewer merge conflicts. The
same property that makes code testable makes it agent-legible. When a file grows large,
that is the signal it is doing too much — for the agent *and* for you.
**Tension:** micro-modularity has its own navigation cost; fragment too far and the
agent burns its context budget just stitching the pieces back together.

### 1.8 Isolate exploration in subagents; fan out independent work. (L6)
Give noisy investigation (broad search, log trawls) its own context window and keep
only the distilled summary in the orchestrator. Dispatch parallel workers on genuinely
independent subtasks; serialize anything that shares state.
**Tension:** multi-agent systems can beat single agents substantially on
research-shaped tasks but cost an order of magnitude more tokens and add coordination
and emergent-failure complexity. A longer loop can just lengthen the review queue.

### 1.9 The human is director and reviewer; cultivate taste, own the merge. (L8)
The agent writes; the human decides what to trust and owns the integration. The scarce
skill shifts from authoring to judgment — architectural consistency, knowing which
plausible-looking diff is actually right. "The loop automates the typing, not the
judgment."
**Tension:** review is now the bottleneck (Part 3), and it is unclear that more or
better AI reviewers help rather than adding one more sensor a human must still
adjudicate.

### 1.10 Don't trust the productivity *feeling* — instrument it. (L7, L8)
METR's 2025 RCT is the cautionary datum: experienced developers felt 20% faster and
were 19% slower. The perception gap means vibes are not evidence. Measure cycle time,
defect rates, and trust-per-change, not tokens emitted or lines produced. Note too that
*which work you attempt* shifts: METR reported 30–50% of developers withheld tasks they
didn't want randomized away from AI — so adoption changes the task mix, not just the
speed on a fixed task.
**Tension:** the gains are real somewhere — greenfield work and less-experienced
developers show clear lift; the mature-codebase case is the contested one — and the
headline number is fragile. METR's own Feb-2026 update says its measurement is biased
by opt-out and may now *understate* speedup. Believe the trend in your own instrument,
not any single study's number.

### 1.11 Isolate concerns; keep behavior local. (L6)
Give each unit one job and a small, named interface, so an agent can change it without
loading its neighbors. The deeper rule underneath is **minimize behavior-at-a-distance:**
an agent, like a reviewer, predicts what code does by reading it, so anything that smears
behavior across files it never opened is where confident mistakes are born. This is also
the honest frame for the inheritance-vs-composition question. Progressive disclosure —
read a high abstraction, descend only when needed — genuinely suits an agent, and a
*shallow, honest "is-a"* hierarchy delivers it better than a sprawling dependency-injection
graph. But that benefit comes from locality and progressive disclosure, not from
inheritance as a mechanism: a deep mixin/MRO tower is the worst case, because a leaf's
behavior is scattered up an ancestor chain the agent must load in full, and editing a base
silently breaks subclasses it never saw. Composition's win is explicit wiring; its loss is
deep, runtime-resolved indirection the agent can't statically trace. The variable that
actually matters is **static discoverability and bounded dispatch** — "how many files
must I open before I can predict what this change does?" — and it indicts both sides
equally: deep inheritance, mixins, MRO tricks, service locators, dynamic DI containers,
plugin registries, and runtime monkeypatching all hide behavior somewhere else. So the
default is composition with explicit constructor wiring and protocol/interface seams;
allow *shallow* inheritance only for stable framework contracts or genuine taxonomies.
The "shallow honest is-a beats sprawling DI" caveat is real — but it doesn't rehabilitate
inheritance broadly. The single sharpest offender is **closure-captured local state and
implicit dependencies**: variables a function closes over, mutable state captured in a
callback, an implicit dependency resolved at runtime. It is invisible at the call site,
usually unannotated, and worse than either inheritance or explicit composition because
there is no declared seam to read at all. Annotate it, or avoid it. And this is a place to
spend tooling: prioritize tools that **surface, locate, and bound** what a unit depends on
and captures — names, line ranges, the closure's free variables — so a hidden dependency
becomes a visible, navigable fact instead of something the agent must simulate execution to
discover.
**Tension:** the categorical claim "inheritance beats composition for agents" is half-right
and worth the argument — see the coda.

### 1.12 The dependency calculus inverted. (L12)
Agents collapse the cost of writing small utilities to near zero, while every dependency
keeps its full, permanent cost: supply-chain risk, version churn, and an external contract
you don't control. So the bar to take a dependency rose. Write small things yourself; take
a dependency only when it is substantial enough that you couldn't cheaply regenerate it
*and* it is both well-maintained (patch latency) *and* popular (many eyes ≈ security). A
small dependency is now usually worse than the code you would write in its place.
**Tension:** "cheaply regenerate" tempts you to reinvent crypto, parsers, and date math —
exactly the domains where a popular, audited dependency is non-negotiable. The calculus is
a heuristic, not a license to reinvent everything.

### 1.13 Treat everything the model reads and pulls as untrusted. (L12)
Two distinct new attack surfaces, both serious. **Prompt injection:** any content the
agent ingests — an issue, a web page, a tool result, a dependency's README — can carry
instructions, and the model cannot reliably separate data from command. Establish trust
boundaries, give tools least privilege, and require human approval for outward or
destructive actions (Willison's "lethal trifecta": private data + untrusted content + a way
to exfiltrate is the danger condition). **Slopsquatting:** the agent itself becomes the
vector — models confidently hallucinate package names that attackers pre-register. The
USENIX 2025 study measured ~5.2% hallucination for commercial models and ~21.7% for open
ones; a 2026 replication puts frontier models nearer 5–6%. The rate is falling, but the
*exploitable* property is its predictability — a large share of hallucinated names recur
across re-runs — not the rate. Screen and pin dependencies, verify provenance, and never
let an agent install what it merely named. Prompt injection and slopsquatting are two
entries in a wider class: OWASP's 2026 *Top 10 for Agentic Applications* adds goal hijack,
tool misuse, excessive agency, identity abuse, memory poisoning, and cascading
multi-agent failure. The governing principle is **grant least _agency_, not just least
privilege** — constrain what the agent is allowed to *decide and do*, not only what it can
read.
**Tension:** the strongest mitigations (no untrusted content, no exfiltration path,
human-in-loop on every action) directly throttle the autonomy that makes agents useful.
Security and capability trade against each other here.

### 1.14 Rewrite from the spec, not the code. (L11)
Classic wisdom (Spolsky, "Things You Should Never Do") was: never rewrite, because code
embodies years of accumulated bug-fixes and edge-case knowledge a rewrite discards. Two
things changed. The cost of the rewrite collapsed — the model writes it — and, crucially,
*if* the hard-won knowledge is captured as spec + tests, the rewrite no longer discards it.
So the calculus inverts **exactly to the degree your contract preserves the edge cases.** A
clean reimplementation against a strong battery, with a better model and no legacy debt, is
now often cheaper *and* better than patching.
**Tension:** with thin tests, the rewrite rediscovers every bug Spolsky warned about. And
even with good *unit* tests it stays dangerous for stateful systems, schema/data
migrations, performance-sensitive paths, and emergent integration behavior — places where
the hard-won knowledge lives in production data and interactions a battery can't capture.
L11 is safest for bounded, mostly-pure, well-tested units; treat it as a scalpel, not a
policy. The cheapness is real; the *safety* is a function of contract coverage and blast
radius — which is why this principle is downstream of L2.

### 1.15 Make engineering compound. (L13)
Each task should leave the system better at the next one. Capture learnings — what broke,
the non-obvious constraint, the pattern that worked — as durable, maintained context the
next iteration consults. Every Inc's "compound engineering" automates exactly this: Plan →
Work → Review → **Compound**, where an agent distills each task's learnings into a
structured wiki and the compound↔plan loop becomes the flywheel. The per-repo "operating
manual" is the same idea (mabl cut context-drift failures from ~40% to <5% with one).
Maintain it like code, because it *is* the code's memory.
**Tension:** captured learnings rot exactly like comments and docs do, and a stale
"learning" is worse than none because it is trusted. Larson's caution also applies: this
gets absorbed into the base tools soon, so invest in the *practice*, not a particular
plugin.

### 1.16 Keep changes reviewably small. (L7, L8)
Agents make it trivial to produce a huge, coherent-looking diff — which is exactly the
wrong thing when review is the bottleneck (L7) and the human owns the merge (L8). Faros
telemetry found AI adoption came with ~154% larger PRs and ~9% more bugs per developer:
batch size is its own lever. Cap the unit of change to what a human can actually hold in
their head, and make the agent split work rather than land it in one pass.
**Tension:** small PRs add coordination and sequencing overhead, and some changes (a
rename, a codegen refresh, a mechanical migration) are *genuinely* large and atomic —
splitting them is busywork. The rule is "reviewably small," not "small."

### 1.17 Shrink the review surface; raise the altitude of review. (L7, L8)
Review is the bottleneck (1.9, Part 3), and the durable fix is not "review faster" — it is
to **reduce what a human must look at.** Push low-level abstractions *below the line* of
human attention, into the purview of deterministic checks and good-enough (often local)
models, so the human reviews intent and contracts while the generated implementation is
reviewed *by its tests*, not by eye. This is the strategic answer to L7 and a core premise
of spec-driven tools: the unit of human review becomes the spec diff, not the code diff.
Two corollaries: (a) optimize review *precision* — every false-positive review spends the
scarcest resource and trains reviewers to rubber-stamp; (b) "below the line" is only safe
to the degree the lower layer is genuinely verified — shrink an *unverified* surface and
you have hidden a bug, not removed a chore (L4).
**Tension:** raising the altitude concentrates risk in the contract and the checks. If the
spec is wrong or the tests are weak, you have removed the one human who might have caught it
by reading the code. The altitude you review at can only be as high as your verification is
trustworthy.

### 1.18 Push determinism down: deterministic preprocessing, then local inference. (L3, L9)
Spend the model on the smallest, cleanest residue. First normalize deterministically —
tree-sitter / AST canonicalization strips formatting, comments, and cosmetic noise before
anything model-shaped runs (jaunt's Layer A is exactly this). Then, where a model judgment
*is* needed, prefer a **deterministic local model** (e.g. steadytext: fixed seed, greedy
decoding, on-device, "same input → same output") over a network API. That choice does
double duty: it is cheap and private enough to run *everywhere* (which is how you afford to
verify at all, point from L7), and it makes the model step *reproducible* — converting a
non-deterministic network dependency into something closer to a pure function, which is
what L9 actually wants. The bet: on-device models are nearly good enough for these narrow,
well-scoped judgments and keep improving.
**Tension:** determinism is not correctness — a deterministic local model is reproducibly
wrong when it is wrong, so it still needs the L4 backstop. And local models lag frontier
models in capability; the narrower and more preprocessed the question, the safer the
substitution, which is why the preprocessing comes first.

---

## 2. Through the jaunt lens (tool altitude)

*Dominant laws at this altitude: **L2, L3, L4, L9.** A spec-driven framework is what
it looks like to bake Part 1's practices into a tool instead of a habit.*

Jaunt is a direct bet on the throughline. You write intent as decorator-marked Python
stubs — `@jaunt.magic` for implementations, `@jaunt.test` for tests — with the cleaned
docstring as the behavioral contract, and jaunt generates real modules into
`__generated__/` via the Codex CLI. The interesting thing is not that it generates
code; lots of things do. It is *how it decides when not to, and who has final say.*
Read through 1.17, jaunt's premise is to **raise the altitude of review**: the human
reviews the docstring contract and the battery; the generated implementation lives below
the line, owned by its tests. And read through 1.18, its layered design — deterministic
AST-normalized digest first, a cheap model only on the residue — is the
"push determinism down" principle made concrete (a deterministic *local* gate per
steadytext would be the natural completion).

### 2.1 Where jaunt embodies the laws — and where it's still design

> **Honesty marker.** Magic mode, Contract mode (`contract/` — derive, battery, drift,
> strength, runner), input-keyed digests, `generation_fingerprint`, the model-free
> `jaunt check`, and the dependency graph are **shipped**. Smart change detection
> (AST-normalized digest + cheap-model semantic gate + re-freeze) is **designed and
> Codex-reviewed but not yet in `src/jaunt/`** — it is the headline roadmap item (§2.3).
> Below, designed-not-shipped items are marked *(designed)*.

- **L2 — spec is the durable artifact.** *(shipped)* The docstring *is* the contract, in
  both modes. Magic mode makes English canonical and the generated code a disposable
  build artifact. Contract mode inverts it: committed code is canonical, the docstring is
  the contract, and jaunt derives a committed pytest battery instead of an implementation.
- **L3 — push work into deterministic layers.** Freshness today is input-keyed but hashes
  *raw* source, so a ruff reformat or comment edit still flips the digest and forces a
  full rebuild. The fix — *smart change detection*: an AST-normalized contract digest
  (Layer A, deterministic, ignores cosmetic noise) with a cheap-model semantic gate
  (Layer B) only on a genuine prose change — is *(designed)*, not yet code. The
  *instinct* is in the codebase; this specific mechanism is still on paper.
- **L4 — the model proposes, deterministic checks dispose.** Jaunt's spine and sharpest
  instinct. *Shipped:*
  - `jaunt check` and `jaunt status` are deterministic, offline, and need no API key.
    The CI gate never calls a model.
  - The contract deriver **never returns a verdict** ("does this code satisfy the
    contract?"). It *derives falsifiable checks* that then run deterministically, so drift
    becomes "derived case #7 failed on input X" — reproducible and locatable.
  - The **mutation-based strength score** catches the silent failure of
    prose-as-contract: a docstring that reads like a spec but pins nothing. Mutate the
    body, re-run the battery; a contract that survives a broken body is decoration.

  *Designed (part of smart change detection):* **fail-safe to REBUILD** on any ambiguity
  (a wrong gate verdict costs a rebuild, never silent drift) and **validate-before-
  re-freeze** (a re-freeze must never certify code a fresh build's gates would reject).
  The right properties — not yet shipped.
- **L9 — pin the artifact, not the sampler.** Freshness is an input-keyed SHA-256
  digest over spec source + decorator kwargs + transitive dependency digests;
  regenerate only when inputs change. The generated tree is machine-owned (`DO NOT
  EDIT`) with one explicit, tracked escape hatch (`@jaunt.preserve`) — the clean
  separation that let protobuf and OpenAPI generators scale, not the abandoned
  round-trip-region path.
- **L6 — architecture as amplifier.** Generation is per-module with a real dependency
  graph (explicit `deps=` plus AST inference, topologically sorted, cycle-detected), so
  the blast radius of a change is computed, not guessed.
- **Meta (L4 + L8 applied to jaunt's own process).** The smart-change-detection design
  was adversarially reviewed by Codex at high reasoning effort against the real source,
  and that review *reshaped* it (gate strategy flipped from drift-check to contract-text
  diff; six correctness fixes to Layer A). The tool's own design process runs the same
  loop it sells: model proposes, an independent check disposes.
- **L11 — rewrite is the native operation.** Jaunt's whole premise *is* rewrite-from-spec:
  edit the docstring, or just point a better model at the unchanged contract, and
  regenerate. The committed battery and mutation strength score are precisely the
  "preserve the hard-won edge cases" mechanism that makes L11 safe. Where most codebases
  must *earn* the right to rewrite, a jaunt module rewrites by default and proves itself
  against its committed tests — jaunt is what L11 looks like when it is the default path,
  not the exception.
- **L12 — the sandbox is a real lever, with one open surface.** Codex runs under a
  configurable `sandbox` (`workspace-write`, etc.), so generation has bounded blast radius
  by design. The surface jaunt uniquely opens: generated code can embed a hallucinated or
  injection-suggested import, which no general-purpose guard catches (see roadmap item 8).

### 2.2 Positioning against the field

In the spec-first → spec-anchored → spec-as-source taxonomy, jaunt is unusual: it
**ships both ends** as coexisting, decorator-selected modes. Magic mode is
spec-as-source (Tessl's end); Contract mode is spec-anchored. Kiro and Spec Kit are
spec-first and commit hand-edited code; Tessl regenerates and forbids hand-edits.
(Honest scope: Contract mode today covers **top-level sync functions**, not full parity
with Magic mode — strategically strong, currently narrow.)

More importantly, Böckeler's survey names a gap that *none* of Kiro, Spec Kit, or
Tessl close: **ongoing spec↔code drift**, plus the absence of incremental,
input-keyed change detection. Jaunt ships exactly the two things that attack that gap —
an input-keyed incremental digest and a model-free deterministic CI gate
(`jaunt check`) — and its *designed* smart change detection is a textbook instance of
the Meta-ACH two-tier pattern: a cheap deterministic filter, the model only on genuine
ambiguity, and a deterministic backstop confirming the verdict. Shipping that gate is
what would turn the competitive claim from "betting on" to "demonstrating."

That is the honest competitive claim: jaunt is not "another SDD tool," it is the one
betting that **determinism and incrementality are the unsolved part**, and building
there.

### 2.3 Roadmap compass (concrete do-next, each tied to a law)

**Priority order (correctness/security before efficiency):** do **1** (fingerprint
completeness) and **8** (generated-import provenance) first — they close *correctness and
security* holes. Then ship smart change detection itself (the umbrella for items **4–5**,
which presuppose the gate exists). The efficiency items earn their place only once the
correctness ones are closed. Numbering below is by topic, not priority.

1. **Close the residual gaps in the causal-input key. (L9)** Good news first: jaunt
   already does the Bazel-style thing for most of the causal set. `generation_fingerprint`
   folds in engine, `codex_model`, `reasoning_effort`, sandbox, and build instructions,
   and that fingerprint **gates staleness** (`builder.py:176-178`) — so a model or effort
   change already busts the cache. Two residual gaps remain, and both let an upgrade
   silently serve stale code: (a) under the Codex engine, jaunt's own **prompt templates**
   are not in the fingerprint — `prompt_parts` is populated only for non-codex engines
   (`generate/fingerprint.py`), so editing `prompts/*.md` or a `[prompts]` override won't
   invalidate anything; (b) `engine` is the string `"codex"`, a **name, not a version** —
   a Codex CLI or underlying model-snapshot upgrade that changes behavior won't bust the
   key. Fold the effective prompt-template digest (Codex path too) and a Codex
   engine/binary version into the fingerprint. *Smallest, highest-correctness-leverage
   item here.*
2. **Mark committed generated code second-class. (L2 / commit-vs-regenerate)** Add
   `__generated__/** linguist-generated=true -diff` to `.gitattributes`. This resolves
   the commit-vs-regenerate debate without choosing a side: reproducible self-contained
   checkouts and a model-free CI gate, without polluting PR diffs. Caveat (Codex): this is
   only safe if the *review target* shifts to the spec diff + battery diff + provenance —
   otherwise you are hiding behavior, not just noise.
3. **Properties over examples for the contract core. (L5)** Contract mode today derives
   examples + errors and defers properties. Property derivation (Hypothesis-backed) is a
   stronger contract and closes the "tests pass but behavior is wrong" gap — but only with
   *real invariants/oracles*; properties generated from vague prose are decorative
   randomness, not a stronger spec. Keep a few canonical examples for grounding the model
   and the human reader.
4. **Make the nano gate auditable and cheaply backstopped. (L4, L7)** Log every
   `EQUIVALENT` verdict with its old/new prose, and consider a sampled deterministic
   re-derivation (or battery run) to catch field false-KEEPs. Meta-ACH's lesson:
   equivalence is undecidable, so they *always* confirm a positive verdict with a
   generated killing test. Jaunt's fail-safe bias is right; add observability so a rare
   wrong KEEP is detectable after the fact.
5. **Add a semantic-equivalence normalization rung to Layer A. (L4)** Beyond AST-token
   normalization, fold literal-equivalence (`1337` == `0x539`), redundant parens, and
   safe reorders to cut over-rebuilds further — but keep the recall-safe bias: err
   toward rebuild, because a false *negative* (silent drift) is the worst outcome, far
   worse than an unnecessary rebuild. (SemanticDiff / GumTree lineage.)
6. **Treat spec-authoring as the paved road. (L1, L10)** The Claude Code plugin/skills
   work (plan.md) is the factory primitive hiding in plain sight: the golden path for
   *writing a good spec* is the highest-leverage standardization jaunt can ship. The
   strength score is the floor-enforcement that keeps that road honest.
7. **Round-trip ambiguity detection. (L8)** A flagged non-goal worth promoting:
   re-derive prose from an unchanged input and compare. Low agreement surfaces an
   *ambiguous spec* to the human director before it causes drift downstream — verifying
   the spec, not just the code.
8. **Screen generated imports against a provenance allowlist. (L12)** Generated code can
   import a package the model hallucinated (slopsquatting) or one an injected docstring
   asked for. Add a deterministic post-generation check that every import in
   `__generated__/` resolves to a declared, pinned dependency — fail the build otherwise.
   It is a model-free gate (fits L4) and it closes the one supply-chain hole a codegen tool
   uniquely opens. This is arguably more urgent than items 5–7, because it is a
   *correctness/security* gap, not an efficiency one.

---

## 3. Building software factories (organization altitude)

*Dominant laws at this altitude: **L1, L7, L10, L12, L13.** The unit is a pipeline that
turns specifications into verified, integrated changes — at the scale of many agents and
many repositories.*

### 3.1 The factory is the artifact, not the code. (L1)
You are no longer writing software; you are building and tuning the system that builds
software. The leverage is in the pipeline — specs, evals, gates, rollout controls —
because generation, and increasingly *re*generation of whole subsystems as better models
arrive (L11), is commoditized.
**Tension:** is "factory" even the right metaphor? Manufacturing has cheap design and
expensive replicated production; software is the inverse (Reeves). "Industrializing"
software risks lavishing effort on the step that was already free. The honest reframe:
the factory produces *verified design changes*, not units.

### 3.2 Specification is the scarce resource. (L1, L5)
Precise, unambiguous specs are the highest-leverage input, because a vague spec
multiplies errors across every parallel agent run. Spend your best people here.
**Tension:** "agents clarify intent" is false. An agent cannot resolve an
under-specified requirement; it confidently fills the gap. Ambiguity is amplified, not
absorbed.

### 3.3 Fund verification like you fund production. (L7)
Tests, evals, policies, audit logs, and rollout controls deserve the same investment as
the feature work. The 2025–26 data is blunt: generation volume up, review time up
proportionally — the constraint moved downstream and most orgs haven't funded it there.
**Tension:** throughput without trust is negative work; AI-authored changes carry more
issues per change, so unscaled verification turns extra output into extra liability.

### 3.4 Apply Theory of Constraints continuously. (L7)
Find today's bottleneck along spec → generation → review → integration, exploit it,
then re-find it — because it moves. Adopting agents *fastest* is not the win; relieving
the *current* constraint is.
**Tension:** the bottleneck migrates silently. Solve generation and it becomes review;
solve review and it becomes integration or spec quality. The org that measures wins;
the one that assumes loses.

### 3.5 Parallelize independent work; serialize shared state. (L6)
Fleets shine on independent, repetitive work — migrations, test backfills, lint sweeps
across many repos (Devin-style fleets, Nubank-style refactors). Orchestration —
deciding what can run concurrently — becomes a first-class engineering skill.
**Tension:** naive fan-out causes redundant or conflicting work; the coordination cost
can eat the parallelism gain.

### 3.6 Avoid a single orchestration chokepoint. (resilience)
A centralized supervisor is a structural failure mode: if it dies, the swarm collapses,
and as you scale it becomes the constraint. Design coordination to degrade gracefully.
**Tension:** decentralized coordination is harder to reason about and audit than a
single conductor — you trade a throughput ceiling for debuggability.

### 3.7 Make the build/feedback loop fast and deterministic. (L4, L7)
Reproducible environments, fast CI, reliable regression detection. A flaky pipeline
poisons every parallel run at once, and the factory's throughput is gated by its
slowest *reliable* check, not its fastest optimistic one.

### 3.8 Capture institutional knowledge as reusable assets. (L1)
Encode patterns, skills, and domain languages so the factory accelerates over time.
This is the original Software Factories "product line" insight — vindicated — but done
with natural-language specs + tests + skills as the flexible domain language, not the
rigid DSLs that sank the 2004 version.
**Tension:** standardized assets can ossify; novel problems still need bespoke design
that the paved road actively discourages.

### 3.9 Standardize the path, not the thought. (L10)
Golden paths and paved roads let agents and humans default to the validated, secure
workflow — the platform-engineering primitive with measured DORA lift. Standardize the
*defaults*, not the destination.
**Tension:** the same paved road that raises the floor can cap the ceiling if it becomes
the only road.

### 3.10 Optimize for trust-per-change, not lines-per-day. (L1, L8)
The unit of value is a *verifiably correct merged change*. Volume without confidence is
motion, not progress.

### 3.11 Engineering compounds, or it does not scale. (L13)
A factory that does not learn re-pays every lesson on every run. Make the "compound" step
institutional: agents capture per-task learnings into shared, maintained context that
future runs consult, turning one-off fixes into permanent capability. This is the modern,
honest form of the Software Factories "product line" thesis — reusable assets, but as living
specs, tests, and learnings rather than rigid DSLs.
**Tension:** shared learnings are shared blast radius — a wrong "learning" propagates to
every agent at once, so the compounding mechanism needs the same verification gate as code.

### 3.12 Defend the supply chain and the trust boundary at fleet scale. (L12)
At one-developer scale a prompt injection or a slopsquatted package is a bad day; across a
fleet acting on many repos and untrusted inputs, it is a breach that propagates. Screen
dependencies centrally, pin and verify provenance, sandbox agent execution, and treat every
external input the fleet ingests as hostile by default.
**Tension:** centralized screening is another chokepoint (3.6) and another queue; security
controls fight throughput at exactly the scale where throughput is the whole point.

### 3.13 Build many thin sub-factories, not one monolith. (L7, L10; resolves 3.1/3.6)
The honest form of "the factory" is not one giant pipeline but **many thin vertical
sub-factories** — each owning a narrow slice end to end and wired to a real **user-feedback
signal** (bug reports *and* improvement signals) wherever one legitimately exists. This
resolves two tensions at once: it dodges the single-orchestrator failure (3.6) by keeping
units independent, and it answers the metaphor objection (3.1) because each slice exposes
and relieves *its own* constraint (Theory of Constraints, locally). The feedback signal is
load-bearing: a sub-factory with no signal is a code cannon, not a factory.
**Tension:** vertical slices still share cross-cutting concerns — auth, data model,
security, the design system. Slice too thin and they duplicate or quietly diverge on
exactly those; the craft is cutting along genuine seams, and some concerns must stay
horizontal and shared.

### 3.14 Historical callout — learn from why the first software factories stalled.
> Microsoft's Software Factories (Greenfield & Short, 2004) got the durable part right:
> reuse of domain assets, software product lines, raising the abstraction above
> hand-coding. They got the fatal part wrong: they demanded heavy upfront formalism
> (DSLs, metamodels, MDA tooling) that was brittle and vendor-locked, and — per Reeves —
> they automated *production* (compilation, already nearly free) while leaving *design*
> (specification, the actual cost) untouched. The agent era inherits the asset-reuse
> thesis and replaces the rigid DSL with natural-language specs + tests + skills. The
> mistake to not repeat: **automate specification and verification, not just typing.**

---

## 4. Live tensions — and our current stance

These are the debates the framework does not pretend to settle. A principle that hides its
tension is propaganda — so each is stated honestly, followed by **our current stance**
(2026-06), which is a working bet, not a closed verdict.

1. **Does agentic coding make experienced developers faster?** METR's 2025 RCT found
   ~19% slower while feeling ~20% faster — but its own Feb-2026 update says the measure is
   biased by task/developer opt-out and may now *understate* speedup, so the headline is
   an early-2025 mature-OSS finding, not a standing verdict. Greenfield and junior gains
   look real; the mature-codebase case is genuinely contested.
   **Stance:** stop arguing the average; instrument it continuously and cheaply — small,
   fast, local models can measure this on-device, and that capability is nearly good enough
   now and improving. Track the task *mix*, not just speed on a fixed task.
2. **How far toward spec-as-source dare you go?** Determinism is bounded — regenerating
   from the *same* spec yields different code (Böckeler). A spec is a reliable "source"
   only if generation is deterministic enough, which pushes specs toward code-level
   precision: "the return of waterfall."
   **Stance:** hedge (jaunt ships both modes), and *expand the deterministic envelope*
   (tension 5) so "deterministic enough" becomes reachable for bounded slices — but do not
   go full spec-as-source where the ground truth is weak (tension 8).
3. **Is "software factory" even the right metaphor?** If coding *is* design (Reeves) and
   essential complexity is irreducible (Brooks), then the factory framing risks
   optimizing the free step.
   **Stance:** drop the monolith image. Build **many thin vertical sub-factories** with
   real user-feedback signals (3.13); "the factory" is the per-slice spec-and-verification
   pipeline, not a code assembly line.
4. **Does scaling verification actually net out?** More AI reviewers may help — or may
   just add another sensor a human must adjudicate, lengthening the queue.
   **Stance:** this is *the* bottleneck now, so spend here first. It nets out only if you
   (a) shrink the review surface and raise its altitude (1.17), (b) push judgment onto
   cheap local models with deterministic backstops (1.18), and (c) ruthlessly minimize
   false-positive reviews — every false alarm spends the scarcest resource.
5. **How much determinism is enough?** You can pin the artifact but never the sampler
   (batch-variance nondeterminism is below the API line).
   **Stance:** more is reachable than "you can't pin the sampler" implies. Deterministic
   preprocessing (tree-sitter / AST canonicalization) plus deterministic *local* inference
   (steadytext: fixed seed, greedy, on-device) move most of the residue into reproducible
   territory; the irreducible remainder stays behind a deterministic, model-free check
   (1.18, L9).
6. **Contract precision: over-spec vs under-spec.** Pin behavior tightly enough to test
   and you risk encoding the implementation (every refactor becomes a contract edit);
   leave it loose and wrong-but-passing code slips through. Meyer's pre/post/invariant
   separation is the mitigation, not the cure.
   **Stance:** there is no universal threshold — this is irreducibly a matter of taste and
   architecture, and saying so is honest, not evasive. It is precisely *why* this document
   exists: to compile into context-sensitive guidance for the implementer harness (see
   "How to use this document") rather than a hardcoded rule that won't survive the next
   codebase.
7. **Inheritance vs composition for agents.** Does inheritance's top-down readability
   beat composition's breadth, or does its implicit dispatch (MRO, `super`, methods
   inherited from elsewhere) make leaf behavior *less* legible by smearing it up the
   ancestor chain?
   **Stance (1.11):** the axis is explicitness and locality, so **shallow + explicit beats
   broad + implicit.** Shallow inheritance can beat sprawling composition; the worst
   offender on either side is closure-captured / implicit state — annotate it or avoid it,
   and build tooling to surface and bound what a unit captures.
8. **Rewrite vs preserve (Spolsky, conditionalized).** "Never rewrite from scratch" was
   right when code was the *only* repository of hard-won knowledge. It becomes wrong as
   that knowledge migrates into spec + tests — but no one agrees on when coverage is
   *enough* to trust a from-scratch regeneration over a battle-tested incumbent.
   **Stance:** rewrite is safe *iff* two conditions hold — the system actually followed
   these principles (durable spec, contract captured) **and** there is a well-defined
   ground truth (a real battery). Absent either, preserve. The conditions are the whole
   answer.

---

## Sources

**Foundational (why the bill moves to design):**
- Jack W. Reeves, *What Is Software Design?* (C++ Journal, 1992) — source code *is* the
  design; build is nearly free. https://www.developerdotstar.com/mag/articles/reeves_design.html
- Fred Brooks, *No Silver Bullet — Essence and Accident in Software Engineering* (1986) —
  essential vs accidental complexity. https://worrydream.com/refs/Brooks_1986_-_No_Silver_Bullet.pdf
- Bertrand Meyer, *Applying Design by Contract* — specify *what*, never *how*.
  https://se.inf.ethz.ch/~meyer/publications/computer/contract.pdf

**Building with agents (Part 1):**
- Anthropic, *Effective context engineering for AI agents* —
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, *Writing effective tools for AI agents* —
  https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, *How we built our multi-agent research system* —
  https://www.anthropic.com/engineering/multi-agent-research-system
- Simon Willison, *Agentic Engineering Patterns: First Run the Tests* —
  https://simonwillison.net/guides/agentic-engineering-patterns/first-run-the-tests/
- Addy Osmani, *Code Review in the Age of AI* — https://addyo.substack.com/p/code-review-in-the-age-of-ai
- METR, *Measuring the Impact of Early-2025 AI on Experienced OSS Developer Productivity* —
  https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/ — the
  19%-slower / felt-faster result.
- METR, *We're Changing our Developer-Productivity Experiment Design* (Feb 2026) —
  https://metr.org/blog/2026-02-24-uplift-update/ — the 2025 result is biased by opt-out
  and may now *understate* speedup; don't quote 19% as a current general claim.
- Faros AI, *The AI Productivity Paradox Report 2025* —
  https://www.faros.ai/blog/ai-software-engineering — source of the ~98% more PRs / ~91%
  more review time / ~154% larger PRs / ~9% more bugs figures; org-level gains flat.
- Google Cloud / DORA, *2025 DORA Report* —
  https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report —
  AI is an amplifier: throughput and product performance up, delivery stability down.
- Augment Code, *The 80% Problem: AI Agents Ship Fast But Create Hidden Technical Debt* —
  https://www.augmentcode.com/guides/the-80-percent-problem-ai-agents-technical-debt

**Spec-driven & contract-driven codegen (Part 2):**
- Birgitta Böckeler (ThoughtWorks), *Understanding Spec-Driven Development: Kiro,
  spec-kit, Tessl* — the spec-first/anchored/as-source taxonomy and the unsolved-drift
  finding. https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html
- GitHub Spec Kit — https://github.com/github/spec-kit
- Amazon Kiro, *Specs* — https://kiro.dev/docs/specs/
- Simon Maple (Tessl), *From Code-Centric to Spec-Centric* —
  https://tessl.io/blog/from-code-centric-to-spec-centric/
- Sean Grove (OpenAI), *The New Code* — code as a lossy projection of the spec.
  https://www.youtube.com/watch?v=8rABwKRsec4
- Thinking Machines Lab, *Defeating Nondeterminism in LLM Inference* — why you cannot
  pin the sampler. https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/
- SteadyText — deterministic on-device generation/embeddings (fixed seed, greedy, llama.cpp);
  "same input → same output," a reproducible local model step (L9, 1.18).
  https://pypi.org/project/steadytext/
- tree-sitter — incremental parser / concrete syntax trees; the deterministic preprocessing
  layer that strips cosmetic noise before any model runs (1.18, jaunt Layer A).
  https://tree-sitter.github.io/tree-sitter/
- Bazel, *Remote Caching* — action key over inputs + command + env + toolchain.
  https://bazel.build/remote/caching
- Meta Engineering, *LLMs Are the Key to Mutation Testing (ACH)* — recall-oriented LLM
  pre-filter, deterministic killing-test backstop.
  https://engineering.fb.com/2025/09/30/security/llms-are-the-key-to-mutation-testing-and-better-compliance/
- Gojko Adzic, *Specification by Example, 10 Years Later* —
  https://gojko.net/2020/03/17/sbe-10-years.html
- GitHub Docs, *linguist-generated / customizing changed files* —
  https://docs.github.com/en/repositories/working-with-files/managing-files/customizing-how-changed-files-appear-on-github

**Software factories (Part 3):**
- Greenfield & Short, *Software Factories* (Microsoft, 2004) — the founding text.
- Addy Osmani, *The Factory Model: How Coding Agents Changed Software Engineering* —
  https://addyosmani.com/blog/factory-model/
- DevOps.com, *The Bottleneck Isn't Coding Anymore. It's Verification* —
  https://devops.com/the-bottleneck-isnt-coding-anymore-its-verification/
- Cognition, *Devin's 2025 Performance Review* (fleets across many repos) —
  https://cognition.ai/blog/devin-annual-performance-review-2025
- DORA, *Platform Engineering capability* — https://dora.dev/capabilities/platform-engineering/
- platform-engineering.org, *Golden Paths & Paved Roads* —
  https://www.platform-engineering.org/deciding-platform-scope/aligning-on-product-features/golden-paths-and-paved-roads
- Chris Swan, *Agentic product development and Theory of Constraints* —
  https://blog.thestateofme.com/2026/01/18/agentic-product-development-and-theory-of-constraints/

**Dependencies, security & rewrites (cross-cutting — L11/L12/L13):**
- Will Larson, *Notes on Every Inc's "Compound Engineering"* — Plan/Work/Review/Compound;
  automating the compound step is the flywheel. (Originated by Every Inc / Kieran Klaassen;
  plugin: github.com/EveryInc/compound-engineering-plugin)
  https://lethain.com/everyinc-compound-engineering/
- Joel Spolsky, *Things You Should Never Do, Part I* (2000) — the classic "never rewrite
  from scratch"; the position L11 conditionally inverts.
  https://www.joelonsoftware.com/2000/04/06/things-you-should-never-do-part-i/
- Simon Willison, *prompt injection* writing (incl. "the lethal trifecta for AI agents") —
  private data + untrusted content + exfiltration is the danger condition.
  https://simonwillison.net/tags/prompt-injection/
- Cloud Security Alliance, *Slopsquatting: AI Code Hallucinations Fuel Supply Chain
  Attacks* — the agent as supply-chain vector; term coined by Seth Larson (PSF).
  https://labs.cloudsecurityalliance.org/research/csa-research-note-slopsquatting-ai-supply-chain-20260419-csa/
- Spracklen et al., *We Have a Package for You! …Package Hallucinations…* (USENIX Security
  2025) — primary source: ~5.2% commercial / ~21.7% open-model hallucination; hallucinated
  names recur predictably across runs. https://www.usenix.org/conference/usenixsecurity25/presentation/spracklen
- OWASP, *Top 10 for Agentic Applications 2026* — broadens L12 beyond prompt/deps to goal
  hijack, tool misuse, excessive agency, identity abuse, memory poisoning, cascading
  failure. https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
- Every, *Compound Engineering Gets an Upgrade* — the pattern is still evolving; treat it
  as a practice to test, not settled evidence. https://every.to/p/compound-engineering-gets-an-upgrade
