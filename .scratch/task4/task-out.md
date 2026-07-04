Implemented `jaunt jobs land <id>` and `jaunt jobs discard <id>` in [cli.py](/home/diwank/github.com/creatorrr/jaunt-impl-propose-only/src/jaunt/cli.py), plus the requested coverage in [test_cli_jobs.py](/home/diwank/github.com/creatorrr/jaunt-impl-propose-only/tests/test_cli_jobs.py).

What changed:
- Added parser wiring and dispatch for `jobs land` and `jobs discard`.
- Implemented proposed-job validation, freshness gate, branch/dirty prechecks, journal append/truncate behavior, landing outcomes, and discard cleanup.
- Added tests for happy path, journal commit, stale digest, dirty refusal, conflict truncation, wrong state, missing job, and discard behavior.

Verification:
- `uv run pytest tests/test_cli_jobs.py -q` -> `20 passed`
- `uv run ruff check src/jaunt/cli.py tests/test_cli_jobs.py` -> passed
- `uv run ruff format src/jaunt/cli.py tests/test_cli_jobs.py` -> applied formatting to the test file only