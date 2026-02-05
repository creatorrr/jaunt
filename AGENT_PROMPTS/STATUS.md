# Status (Working Tree)

Checked with:
```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ty check
```

## Done
- `010A` (errors + spec_ref): `src/jaunt/errors.py`, `src/jaunt/spec_ref.py`, tests present
- `010B` (registry): `src/jaunt/registry.py`, tests present
- `010C` (runtime decorators): `src/jaunt/runtime.py`, decorator tests present
- `015` (public API exports): `src/jaunt/__init__.py` exports `magic/test` + exceptions (keeps `hello()` for now)
- `020` (config + header + paths): `src/jaunt/config.py`, `src/jaunt/header.py`, `src/jaunt/paths.py`, tests present
- `025` (discovery): `src/jaunt/discovery.py`, tests present
- `030` (deps + digest): `src/jaunt/deps.py`, `src/jaunt/digest.py`, tests present
- `040` (generation core): `src/jaunt/validation.py`, `src/jaunt/generate/base.py`, prompt templates in `src/jaunt/prompts/`, tests present
- `041` (OpenAI backend): `src/jaunt/generate/openai_backend.py`, mocked tests present
- `050` (build/test orchestration): `src/jaunt/builder.py`, `src/jaunt/tester.py`, orchestration + IO + scheduler tests present
- `060` (CLI): `src/jaunt/cli.py` implements `build` + `test`, CLI tests present
- `070` (skill docs): `src/jaunt/skill/` docs + examples present
- `090` (packaging/deps): `pyproject.toml` includes `openai` and includes `py.typed`, prompts, and skill docs in wheel/sdist config

## Not Covered Here
- (next) `070` packaging sanity test: optional `importlib.resources` smoke test not added yet
