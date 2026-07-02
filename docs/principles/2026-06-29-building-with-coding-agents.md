# Building With Coding Agents — A Principles Framework

**Date:** 2026-06-29
**Status:** Living reference. Doubles as a roadmap compass for jaunt.
**Last synced with `src/jaunt/`:** 2026-07-01 — the shipped/designed markers in Part 2
were re-verified against the code on this date.
**Shape:** Hybrid — three altitudes (developer · tool · factory) over six core
laws plus named corollaries, with the unresolved tensions kept honest rather than
flattened.

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

The laws come in two tiers. **Six core laws** are the axioms — none is derivable
from the others, and each marks an independent axis (value, verdict, structure,
judgment, trust, time). The rest are **named corollaries**: real, load-bearing
principles, but each follows from a core law plus a fact this document already
states. Numbering is historical and stable — corollaries keep their original
L-numbers so cross-references stay valid. (L14 is retired: its content — checker
independence — is now the second condition of L4.)

**Core laws:**

| # | Law | One line | Small example |
|---|---|---|---|
| **L1** | Value migrates to spec + verification | The two things worth scarce attention when typing is free. | A failing test handed to the agent does more than a paragraph describing the bug. |
| **L4** | The model proposes; a verdict counts only if it is deterministic *and* independent of what it judges | A wrong model verdict should cost money, never correctness — and an oracle the implementer can see is one it games. | `jaunt check` reruns committed tests with no API key; the implementer gets pass/fail, never `expected 42, got 41`. |
| **L6** | Architecture is an amplifier | Bounded units multiply quality; tangle multiplies *confident* mistakes. | Ten focused files let the agent read only what it edits; one 2k-line module forces guessing. |
| **L8** | The human is the terminal oracle | Verification chains end in a person: the spec itself has no external check, so the human directs intent, owns the merge, and is accountable for what ships — and taste is produced by practice, so maintain it like the asset it is. | You approve the contract and the diff's intent; you don't hand-type the body. |
| **L12** | Treat everything the model reads and pulls as untrusted | Prompt injection and slopsquatting are input, not edge cases. | The build fails if generated code imports a package absent from your declared deps. |
| **L13** | Make engineering compound | Each task should leave the system better at the next one. | Every fixed bug becomes a battery case, so it can't silently regress. |

**Corollaries** (each hangs under a core law):

| # | Corollary | Of | Derivation | Small example |
|---|---|---|---|---|
| **L2** | The spec is the durable artifact; maintain intent alongside code | L1 | Maintain the scarce asset, not just its output. | Edit a `@jaunt.magic` docstring; the `__generated__/` body is rebuilt from it. |
| **L3** | Push work down into deterministic layers | L4 | Minimize the non-deterministic residue the model must judge. | Strip formatting before deciding a spec "changed," so a reformat triggers no work. |
| **L5** | Specify *what*, not *how* | L1 | How to write the asset without destroying its value. | "Return results sorted by score, descending" — not `sorted(key=...)`. |
| **L7** | Verification capacity must scale with generation capacity | L1 | Fund the scarce asset's other half, or the constraint just moves to review. | Merge 10× the PRs and it's review capacity, not generation, that decides throughput. |
| **L9** | Pin the artifact, not the sampler | L4 | Determinism extended over time: regenerate only on input change. | The build re-runs only when the spec, model, or prompt-template digest changes. |
| **L10** | Standardize the path, not the thought | L13 | Paved roads are compounded learning made the default. | `jaunt init` drops every project onto the same validated layout. |
| **L11** | Rewrite from the spec, not the code | L1 | Cash the asset in — as safe as your contract is complete. | Point a newer model at the unchanged docstring; the committed battery proves the rewrite. |

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

*Dominant laws at this altitude: **L1, L4, L6, L8** (with corollary L3) — and where L11–L13 first
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

### 1.9 The human is the terminal oracle; cultivate taste, own the merge. (L8)
The agent writes; the human decides what to trust and owns the integration. The deeper
reason this is a *law* and not an efficiency preference: every verification chain ends
in a person. L4 demands each verdict have an oracle independent of its author — the spec
for code, the battery for behavior — but the spec *itself* has no external check. The
chain has to terminate somewhere, and it terminates in human intent; the human gate is
the one verifier that is neither deterministic nor independent, and it is the carve-out
where L4's conditions cannot be met. That is also why accountability stays human even
where AI review is strong: someone must be answerable for what ships. The scarce skill
shifts from authoring to judgment — architectural consistency, knowing which
plausible-looking diff is actually right. "The loop automates the typing, not the
judgment." And judgment has a supply chain: taste is produced by practice, so a workflow
that removes all authorship must find another way to grow its reviewers.
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
behavior across files it never opened is where confident mistakes are born. The variable
that actually matters is **static discoverability and bounded dispatch** — "how many
files must I open before I can predict what this change does?"

That variable, not ideology, settles the inheritance-vs-composition question — and it
indicts both sides. A deep mixin/MRO tower scatters a leaf's behavior up an ancestor
chain the agent must load in full, and editing a base silently breaks subclasses it never
saw; but service locators, dynamic DI containers, plugin registries, and runtime
monkeypatching hide behavior somewhere else just as thoroughly. Progressive disclosure —
read a high abstraction, descend only when needed — genuinely suits an agent, and a
*shallow, honest "is-a"* hierarchy can deliver it better than a sprawling
dependency-injection graph; but that benefit comes from locality, not from inheritance as
a mechanism, so it does not rehabilitate inheritance broadly. The default: composition
with explicit constructor wiring and protocol/interface seams; allow *shallow*
inheritance for stable framework contracts or genuine taxonomies.

The single sharpest offender is **closure-captured local state and implicit
dependencies**: variables a function closes over, mutable state captured in a callback, a
dependency resolved at runtime. It is worse than either deep inheritance or explicit
composition because there is no declared seam to read at all — invisible at the call
site, usually unannotated. Annotate it, or avoid it. And this is a place to spend
tooling: prioritize tools that **surface, locate, and bound** what a unit depends on and
captures — names, line ranges, a closure's free variables — so a hidden dependency
becomes a visible, navigable fact instead of something the agent must simulate execution
to discover.
**Tension:** the categorical claim "inheritance beats composition for agents" is half-right
and worth the argument — see live tension 7 (§4).

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
human attention, into the purview of deterministic checks and good-enough small
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

### 1.18 Push determinism down: deterministic preprocessing, then the smallest model that works. (L3, L9)
Spend the model on the smallest, cleanest residue. First normalize deterministically —
tree-sitter / AST canonicalization strips formatting, comments, and cosmetic noise before
anything model-shaped runs (jaunt's Layer A is exactly this). Then, where a model judgment
*is* needed, hand the narrowed residue to the smallest, cheapest model that is reliable on
it (jaunt's Layer B gate runs a small model, never the build model), and key the judgment
by its inputs — model id, effort, prompt digest — so it re-runs only when something real
changes (L9). That is what makes verification affordable enough to run *everywhere* (L7):
the narrower and more preprocessed the question, the smaller the model you can spend on it.
**Tension:** cheap is not correct — a small model is confidently wrong within budget, so it
still needs the L4 backstop (fail-safe to rebuild; a deterministic check holds the
verdict). And keying inputs is not pinning outputs: the sampler below the API line stays
non-deterministic, which is why the deterministic layers, not the model, must dispose.

### 1.19 Treat tests as a held-out set; keep the implementer and tester blind to each other. (L4 — the independence condition)
The check in 1.1 is only honest if the code's author did not write it to pass: a
retrofitted test blesses whatever the agent already did (1.1's own tension), and a test
written while staring at the implementation just restates it. The fix is the oldest one in
evaluation — a **held-out set**. Split the harness into two agents, an **Implementer** and
a **Tester**, with an information barrier between them: neither reads the other's source,
and their only shared ground truth is the spec. Three lineages already learned this the
hard way — **independent verification & validation** (aerospace keeps the V&V team
organizationally separate so it cannot inherit the dev team's blind spots), the
**Chinese-wall / Brewer–Nash** information barrier from finance, and the ML
**train/test split** (let the model touch the test set and the accuracy number is fiction).
It is **Goodhart's law** with a compiler: once *passing the test* is the target, the test
stops measuring — an agent optimizing a visible suite is doing **reward hacking** by another
name. This is not hypothetical. Recent benchmarks measure agents doing exactly it:
SpecBench finds a "reward-hacking gap" between pass rates on the visible per-feature suite
an agent iterates against and the held-out suite that composes those features; ImpossibleBench
mutates tests to conflict with the spec and scores how often agents take the spec-violating
shortcut; EvilGenie catches frontier agents (Codex, Claude Code) hardcoding expected outputs
and editing test files when the environment makes it easy.

The barrier is **asymmetric**, and that is the subtle part. Source-blindness is symmetric;
*output*-blindness is not, because **a test's output can contain the answer key and an
implementation's output cannot.** The Tester may freely run the Implementation as a black
box — observing that it returns `41` leaks nothing, because the Tester derived the expected
`42` from the spec independently. The Tester's hazard runs the other way: if it derives its
expectations *from* observed behavior, the tests collapse into characterization snapshots
that bless bugs as correct — the classic **test-oracle problem**, where the verdict of
correctness must come from the spec, a reference model, or a metamorphic relation, never
from the system under test. The Implementer is held stricter: a failing assertion
(`expected 42, got 41`) literally hands over the held-out target, so it must receive a
**bounded, redacted** signal, not the raw failure.

And "coarse pass/fail" is not enough on its own — Dwork et al.'s reusable-holdout result is
blunt: query a held-out set enough times and you overfit it even at one bit of feedback per
round, because many rounds turn the gate into a search oracle. So the real shape is
**tiered**: a generous *public* suite the Implementer owns and sees in full (diffs, traces —
game it freely, it is not the gate); a *private* validation tier that returns only a
contract-area label, no inputs or expected values; and a *final* held-out tier that returns
one suite-level pass/fail at low attempt count. Even test *names* leak —
`test_empty_list_returns_zero` is already an answer — so the private tiers use opaque ids or
broad contract-clause names. This also dissolves the obvious objection ("redact the feedback
and the agent can't debug"): the public tier is exactly where it debugs; only the gate is
held out.

Finally — the move that makes this work with agents specifically — **state the discipline
*and its rationale* to both agents, as policy and not just setup.** Told it will never see
the tests and gets only a verdict, the Implementer cannot specialize to specific cases and
instead invests in covering the whole contract; told the Implementer is blind, the Tester
treats its suite as the real gate and writes adversarial coverage rather than a mirror. Make
it explicit: instruct the Implementer not to probe, infer, or specialize to the hidden tests,
and instruct the Tester to commit its oracle logic *before* observing behavior (or to mark
any behavior-driven finding as non-oracular until re-derived from the spec). The analogy is a
closed-book exam graded by an independent examiner — announcing it in advance changes how you
study.
**Tension:** independence assumes a spec good enough to *be* the shared oracle (1.6, 3.2) —
with a thin contract, the held-out tests silently *become* the requirements, and a wall built
too high just starves the Implementer of the context it needs to resolve genuine ambiguity.
And the independence can be illusory: two agents on the same base model with the same prompt
share blind spots, so the barrier buys less than it looks unless the two are genuinely
diversified — a different model, a different framing, or an adversarial tester.

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
AST-normalized digest first (Layer A), a small-model gate only on the residue (Layer B) —
is the "push determinism down" principle made concrete, and shipped.

### 2.1 Where jaunt embodies the laws — and what's still open

> **Honesty marker** *(re-verified against `src/jaunt/` on 2026-07-01)*. Magic mode,
> Contract mode (`contract/` — derive, battery, drift, strength, runner), input-keyed
> digests, `generation_fingerprint` (now covering prompt templates and the Codex CLI
> version), the model-free `jaunt check`, the dependency graph, **smart change detection**
> (Layer A AST-normalized digests + Layer B semantic gate + re-freeze,
> `change_detection.py`), generated-import provenance screening (`validation.py`), and
> the held-out implementer barrier (`heldout.py`) are all **shipped**. What remains open
> lives in the roadmap (§2.3): gate audit logging, property derivation, deeper Layer A
> normalization, the spec-authoring paved road, and round-trip ambiguity detection.

- **L2 — spec is the durable artifact.** *(shipped)* The docstring *is* the contract, in
  both modes. Magic mode makes English canonical and the generated code a disposable
  build artifact. Contract mode inverts it: committed code is canonical, the docstring is
  the contract, and jaunt derives a committed pytest battery instead of an implementation.
- **L3 — push work into deterministic layers.** *(shipped)* Freshness is computed from an
  AST-normalized contract digest (Layer A — deterministic, so a ruff reformat, comment
  edit, or quote-style change triggers no work), and a genuine prose-only change goes to
  a small-model semantic gate (Layer B): judged equivalent, the module is **re-frozen** —
  header digests rewritten over the validated, unchanged body — instead of rebuilt.
  `--json` reports these under `"refrozen"`.
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

  *Shipped with smart change detection:* **fail-safe to REBUILD** on any ambiguity —
  structural changes, missing snapshots, validation failures, and any gate error all
  resolve to a rebuild, so a wrong gate verdict costs money, never silent drift — and
  **validate-before-re-freeze** (a re-freeze never certifies code a fresh build's gates
  would reject).
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
- **L12 — the sandbox is a real lever, and the codegen-specific surface is closed.**
  *(shipped)* Codex runs under a configurable `sandbox` (`workspace-write`, etc.), so
  generation has bounded blast radius by design. And the surface a codegen tool uniquely
  opens — generated code embedding a hallucinated or injection-suggested import — is now
  guarded deterministically: validation fails the build if `__generated__/` imports a
  package absent from the project's declared dependencies (roadmap item 8, done).
- **L4 (independence condition) — keep the checker independent of the author.** *(shipped, both sides)* The
  **tester-side** barrier holds at two layers: the test generator's context never includes
  generated implementation source (`dependency_generated_modules` is empty for tests — it
  gets only public dependency signatures and the contract), and `public_api_only` (on by
  default) rejects tests that import the generated module, touch `__globals__`/private
  attributes, or monkeypatch the target — with a deliberate
  `@jaunt.test(public_api_only=False)` white-box opt-out. The **implementer-side** barrier
  is now explicit rather than incidental (`heldout.py`, roadmap item 9): repair feedback
  is tiered exactly as 1.19 prescribes. Example-tier cases — canonical examples, fairly
  part of the shared spec — surface full failure detail; derived battery cases are
  redacted to an opaque id plus exception class (`derived#3: AssertionError`), with a
  leak-assertion guard on the redacted output and `--no-redact-derived` as the deliberate
  debug escape hatch. A repair loop can no longer binary-search the committed battery.

### 2.2 Positioning against the field

In the spec-first → spec-anchored → spec-as-source taxonomy, jaunt is unusual: it
**ships both ends** as coexisting, decorator-selected modes. Magic mode is
spec-as-source (Tessl's end); Contract mode is spec-anchored. Kiro and Spec Kit are
spec-first and commit hand-edited code; Tessl regenerates and forbids hand-edits.
(Honest scope: Contract mode today covers **top-level sync functions**, not full parity
with Magic mode — strategically strong, currently narrow.)

More importantly, Böckeler's survey names a gap that *none* of Kiro, Spec Kit, or
Tessl close: **ongoing spec↔code drift**, plus the absence of incremental,
input-keyed change detection. Jaunt ships exactly the three things that attack that gap —
an input-keyed incremental digest, a model-free deterministic CI gate (`jaunt check`),
and smart change detection: a textbook instance of the Meta-ACH two-tier pattern — a
cheap deterministic filter, the model only on genuine ambiguity, fail-safe to a rebuild.
With the gate shipped, the competitive claim has moved from "betting on" to
"demonstrating."

That is the honest competitive claim: jaunt is not "another SDD tool," it is the one
betting that **determinism and incrementality are the unsolved part**, and building
there.

### 2.3 Roadmap compass (concrete do-next, each tied to a law)

**Status (2026-07-01):** the first wave is done. Items **1, 2, 8, 9** — fingerprint
completeness, generated-code gitattributes, import provenance, and the implementer-side
held-out barrier — are shipped, as is smart change detection itself (the umbrella items
4–5 presupposed). What remains is ordered correctness-before-efficiency: **4** (gate
auditability) first, then **3, 5, 6, 7**. Numbering below is by topic, not priority;
shipped items are kept for the record, marked ✅.

1. ✅ **Close the residual gaps in the causal-input key. (L9)** *Shipped.* The
   `generation_fingerprint` already folded in engine, `codex_model`, `reasoning_effort`,
   sandbox, and build instructions, and gates staleness; it now also folds in the
   effective **prompt-template digests** on the Codex path — the always-on preamble
   (`codex_preamble.md`) included, so editing a template or a `[prompts]` override
   regenerates what it should — and the **Codex CLI version** (`codex --version`, via
   `[codex] fingerprint_cli_version`, default on), so an engine upgrade busts the cache
   instead of silently serving stale code.
2. ✅ **Mark committed generated code second-class. (L2 / commit-vs-regenerate)**
   *Shipped:* `.gitattributes` carries `__generated__/** linguist-generated=true -diff` —
   reproducible self-contained checkouts and a model-free CI gate, without polluting PR
   diffs. The caveat stands: this is only safe because the *review target* shifts to the
   spec diff + battery diff + provenance — otherwise you are hiding behavior, not noise.
3. **Properties over examples for the contract core. (L5)** Contract mode today derives
   examples + errors and defers properties. Property derivation (Hypothesis-backed) is a
   stronger contract and closes the "tests pass but behavior is wrong" gap — but only with
   *real invariants/oracles*; properties generated from vague prose are decorative
   randomness, not a stronger spec. Keep a few canonical examples for grounding the model
   and the human reader.
4. **Make the semantic gate auditable and cheaply backstopped. (L4, L7)** The gate ships
   and `--json` reports re-frozen modules, but there is no per-verdict audit trail yet:
   log every `EQUIVALENT` verdict with its old/new prose, and consider a sampled
   deterministic re-derivation (or battery run) to catch field false-KEEPs. Meta-ACH's
   lesson: equivalence is undecidable, so they *always* confirm a positive verdict with a
   generated killing test. Jaunt's fail-safe bias is right; add observability so a rare
   wrong KEEP is detectable after the fact. *Now the top open item.*
5. **Add a semantic-equivalence normalization rung to Layer A. (L4)** Beyond AST-token
   normalization, fold literal-equivalence (`1337` == `0x539`), redundant parens, and
   safe reorders to cut over-rebuilds further — but keep the recall-safe bias: err
   toward rebuild, because a false *negative* (silent drift) is the worst outcome, far
   worse than an unnecessary rebuild. (SemanticDiff / GumTree lineage.)
6. **Treat spec-authoring as the paved road. (L1, L10)** *Partly shipped:* `jaunt
   instructions` emits a project-aware agent primer, and builtin/auto skills seed the
   Codex workspace. The remaining leverage is the golden path for *writing a good spec* —
   the highest-leverage standardization jaunt can ship. The strength score is the
   floor-enforcement that keeps that road honest.
7. **Round-trip ambiguity detection. (L8)** A flagged non-goal worth promoting:
   re-derive prose from an unchanged input and compare. Low agreement surfaces an
   *ambiguous spec* to the human director before it causes drift downstream — verifying
   the spec, not just the code.
8. ✅ **Screen generated imports against a provenance allowlist. (L12)** *Shipped:*
   validation now fails the build when code in `__generated__/` imports a package that
   doesn't resolve to a declared dependency — a deterministic, model-free gate (fits L4)
   that closes the one supply-chain hole a codegen tool uniquely opens, whether the
   import was hallucinated (slopsquatting) or suggested by an injected docstring.
9. ✅ **Make the implementer-side held-out barrier explicit, not incidental. (L4)**
   *Shipped* (`heldout.py`): repair feedback is tiered — full detail from example-tier
   cases (the shared spec), an opaque id plus exception class from derived battery cases,
   a leak-assertion guard on the redacted output, and `--no-redact-derived` as the
   explicit debug escape hatch — so a repair loop cannot binary-search the committed
   tests (Dwork's adaptive-holdout failure). The prerequisite for *safe* auto-repair is
   in place.

---

## 3. Building software factories (organization altitude)

*Dominant laws at this altitude: **L1, L4, L12, L13** (with corollaries L7, L10). The unit is a pipeline that
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

### 3.14 Keep generation and verification independent — at the org level too. (L4, L7)
The held-out discipline of 1.19 is also an *organizational* control. At fleet scale, the
agents (or teams) that author implementations and the ones that author the acceptance oracle
should be independent — the way safety-critical shops keep **IV&V** organizationally separate
from development (NASA/IEEE split it into technical, managerial, and financial independence).
The factory's held-out set becomes an institution: a private acceptance suite, owned by the
verification side, that no implementer fleet can read or edit, feeding back tiered,
query-budgeted signals rather than raw diffs. And diversify the two sides — different models
or framings — because a fleet that implements *and* grades with one model has one set of
blind spots, not two.
**Tension:** independence is one more coordination cost and one more queue (3.6, 3.12), and a
verification side too isolated from intent rejects correct work for the wrong reasons — so it
only nets out if the shared oracle is a genuinely good spec, the very thing 3.2 names as
scarce.

### 3.15 Historical callout — learn from why the first software factories stalled.
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
   **Stance:** stop arguing the average; instrument your own pipeline continuously and
   cheaply. Track the task *mix*, not just speed on a fixed task.
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
   cheap small models with deterministic backstops (1.18), and (c) ruthlessly minimize
   false-positive reviews — every false alarm spends the scarcest resource.
5. **How much determinism is enough?** You can pin the artifact but never the sampler
   (batch-variance nondeterminism is below the API line).
   **Stance:** more is reachable than "you can't pin the sampler" implies. Deterministic
   preprocessing (tree-sitter / AST canonicalization) shrinks the residue, and input-keyed
   pinning (model id, effort, prompt digests) makes the remaining model step re-run only
   on real change; the irreducible remainder stays behind a deterministic, model-free
   check (1.18, L9).
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
9. **Held-out independence vs. debuggability (and the spec it assumes).** Treating tests as
   a held-out set and keeping the implementer blind to them (1.19) prevents gaming — but a
   strict barrier starves the implementer of context for genuine ambiguity, and the whole
   construction assumes a spec good enough to be the shared oracle (tensions 2, 6; 3.2).
   Push independence too far and you either rebuild forever against a thin spec or hide a
   bug behind a tester that misread intent.
   **Stance:** tier the barrier, don't make it a wall. The implementer owns a generous
   public suite it sees in full; only the committed battery is held out, with redacted,
   query-bounded feedback. Independence pays most when the two agents are diversified
   (different model or framing) — two clones of one model share blind spots, and the barrier
   then buys little.

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

**Independence & held-out verification (Part 1 §1.19 / L4's independence condition):**
- Dwork, Feldman, Hardt, Pitassi, Reingold & Roth, *Generalization in Adaptive Data Analysis
  and Holdout Reuse* (NeurIPS 2015) — querying a held-out set adaptively overfits it even at
  one bit of feedback per round. https://arxiv.org/abs/1506.02629 — companion *Science* (2015)
  paper, *The reusable holdout: Preserving validity in adaptive data analysis*.
  https://www.science.org/doi/10.1126/science.aaa9375
- NIST CSRC glossary, *Independent Verification & Validation (IV&V)* — review, analysis, and
  testing by an objective third party, independent of development (NASA/IEEE split it into
  technical, managerial, and financial independence).
  https://csrc.nist.gov/glossary/term/independent_verification_and_validation
- Brewer & Nash, *The Chinese Wall Security Policy* (IEEE S&P 1989) — the access-control
  lineage for conflict-of-interest information barriers.
  https://www.cs.purdue.edu/homes/ninghui/readings/AccessControl/brewer_nash_89.pdf
- Barr, Harman, McMinn, Shahbaz & Yoo, *The Oracle Problem in Software Testing: A Survey*
  (IEEE TSE 2015) — the verdict of correctness must come from an oracle (spec, reference
  model, metamorphic relation), never from the system under test.
  https://doi.org/10.1109/TSE.2014.2372785
- Amodei et al., *Concrete Problems in AI Safety* (2016) — names **reward hacking** as a
  concrete failure; pair with DeepMind, *Specification gaming: the flip side of AI ingenuity*.
  https://arxiv.org/abs/1606.06565 ·
  https://deepmind.google/discover/blog/specification-gaming-the-flip-side-of-ai-ingenuity/
- Manheim & Garrabrant, *Categorizing Variants of Goodhart's Law* (2018) — once the metric is
  the target it stops measuring; the formal frame behind "tests as a decision metric."
  https://arxiv.org/abs/1803.04585
- Recent coding-agent evidence that agents game visible tests:
  - Jimenez et al., *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* (2023) —
    repo-level issue resolution as the standard evaluation setting. https://arxiv.org/abs/2310.06770
  - *SpecBench: Measuring Reward Hacking in Long-Horizon Coding Agents* (2026) — measures a
    "reward-hacking gap" between visible per-feature tests and a held-out compositional suite.
    https://arxiv.org/abs/2605.21384
  - *ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases* (2025) — mutates
    tests to conflict with the spec; any pass is a spec-violating shortcut.
    https://arxiv.org/abs/2510.20270
  - *EvilGenie: A Reward Hacking Benchmark* (2025) — catches frontier agents (Codex, Claude
    Code) hardcoding outputs and editing test files when the environment makes it easy.
    https://arxiv.org/abs/2511.21654
