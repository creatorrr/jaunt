Implemented `jaunt jobs land --all` in `src/jaunt/cli.py`.

What changed:
- Extracted single-proposal landing into `_land_one_proposal(...) -> tuple[int, bool]`.
- Added `_land_all_proposals(...)` that lands proposed jobs in `jobs_mod.list_jobs(...)` order.
- Preserved single-id behavior and stdout/stderr semantics.
- Hard git errors now return `aborted=True` so `--all` stops immediately.

Verification passed:
- `uv run pytest tests/test_cli_jobs.py tests/test_cli_jobs_wait.py -q` → 42 passed
- `uv run ruff check src/jaunt/cli.py` → passed
- `uv run ruff format src/jaunt/cli.py` → unchanged
- `uv run ty check` → passed

Note: the worktree also has pre-existing changes/untracked paths outside my edit scope (`tests/test_cli_jobs.py`, `tests/test_cli_jobs_wait.py`, `.scratch-task5/`, `.scratch/`).