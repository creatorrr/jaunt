Implemented Task 3.

Changed:
- [src/jaunt/daemon.py](/home/diwank/github.com/creatorrr/jaunt-impl-propose-only/src/jaunt/daemon.py): propose-only branch writes `.jaunt/jobs/<id>.patch`, supersedes older proposals, marks `PROPOSED`, journals `job-propose`, and leaves the existing auto-commit landing path unchanged.
- [tests/test_daemon.py](/home/diwank/github.com/creatorrr/jaunt-impl-propose-only/tests/test_daemon.py): added propose mode, proposal supersede, and auto-commit unchanged coverage.

Verification passed:
- `uv run pytest tests/test_daemon.py -q` → 42 passed
- `uv run pytest -q` → 1058 passed
- `uv run ruff check .` → passed
- `uv run ruff format .` → completed
- `uv run ty check` → passed

Only `src/jaunt/daemon.py` and `tests/test_daemon.py` are modified. `.scratch/` remains untracked and untouched.