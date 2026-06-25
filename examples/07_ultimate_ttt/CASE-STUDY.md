# Case Study: building this example blind

This example was produced as a **black-box dogfood test of Jaunt**. An autonomous agent
was dropped into an empty directory with nothing but a virtualenv (Jaunt installed), the
public `README.md`, and a one-line note that the Codex engine was already authenticated. It
was given no access to Jaunt's source, design docs, or internal conventions, and told:
*"build a working Ultimate Tic-Tac-Toe using Jaunt, and journal your experience honestly."*

This is a curated version of that journal — what worked, what didn't, and the real numbers.

## Outcome

**It worked.** Jaunt generated a correct Ultimate Tic-Tac-Toe — including the subtle
forced-redirect edge cases — and **all 10 generated tests passed on the first run, with
zero hand-edits.** Manual verification confirmed the core "send to board" mechanic, forced
redirects when a target board is won or full, local- and meta-board win detection, draws,
and descriptive `ValueError`s. The minimax AI plays a correct game and opens with the
known-strong centre-of-centre move `(4, 4)`.

Total: **~10 minutes** from empty directory to working, tested implementation.

| Build | Target | API calls | Tokens | Cost |
|-------|--------|-----------|--------|------|
| 1 | `game.py` (whole class) | 1 | 138,864 | $0.32 |
| 2 | tests (`jaunt test`) | — | — | — |
| 3 | `ai.py` (minimax) | 3 | 336,330 | $0.75 |
| 4 | `cli_player.py` (`@preserve`) | 1 | 126,494 | $0.27 |
| 5 | game+ai rebuild (stale cascade) | 4 | 524,336 | $1.20 |
| 6 | `game.py` after a docstring edit | 3 | 367,478 | $0.83 |
| **Total** | | **~12** | **~1.5M** | **~$3.37** |

## What impressed a skeptical first-time user

- **Correct on the first try** — not "close." The generated `move()` implemented
  `active_board = cell_idx`, the won/full redirect, local→meta promotion, meta-win, draw
  detection, and player switching, all correctly.
- **The generated tests were genuinely good** — realistic game sequences with setup
  helpers, not trivial assertions.
- **`@jaunt.preserve` worked exactly as documented** — `CLIPlayer.run()` was kept verbatim.
- **Incremental staleness was accurate** — editing a method's docstring correctly marked
  the module stale and only that module rebuilt.

## Rough edges it hit (verbatim findings)

1. **`@jaunt.magic` without parentheses crashes opaquely** — `TypeError: magic() takes 0
   positional arguments but 1 was given`. The error never says "add `()`." Cost ~5 minutes.
2. **`pytest` isn't in the venv and there's no helpful error** — `jaunt test` just fails
   with `No module named pytest`. Had to `ensurepip` + `pip install pytest` manually.
3. **`jaunt build` / `jaunt test` are silent on success** — no "built N modules" or
   "10 tests passed" summary; you have to open the generated files to know what happened.
4. **The stale cascade is aggressive** — building a *downstream* module marked already-built
   upstream modules stale, triggering rebuilds that cost real money (~$1.20 here).
5. **`jaunt init` scaffolds no example stub** — a new user has no template for what a
   `@jaunt.magic` class actually looks like, and the README had no inline code example.
6. **Cost is real** — ~120K prompt tokens *per build* (large system prompt) dominate; a toy
   project ran ~$3.37.

> Several documentation/scaffolding issues this run surfaced (a stale `jaunt init` config
> template and out-of-date engine references in the docs) have since been fixed. The DX
> items above (1–5) remain open and are good first issues.

## Verdict (from the evaluator)

> *Rating: 7.5/10. For "spec → working code" on self-contained logic, **yes,
> enthusiastically** — writing a correct Ultimate TTT plus tests in 10 minutes is
> remarkable. For production use on a real codebase, the stale-cascade cost model and the
> silent, example-less DX need work first. Best moment: watching 10/10 tests pass on the
> first run — tests the model wrote against code the model wrote, with the forced-board
> mechanic correctly implemented.*
