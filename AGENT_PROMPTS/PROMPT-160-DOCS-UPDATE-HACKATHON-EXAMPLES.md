# PROMPT-160: Docs Update (Hackathon Examples Story)

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Update the Fumadocs site to tell the hackathon story:
- Point readers to `jaunt-examples/` as the primary demos.
- Make JWT auth the headline “wow gap” example.
- Inline short spec excerpts plus short generated-code excerpts after generation.
- Explicitly mention the `.agents/skills` auto-generation story.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/docs-site/content/docs/guides/examples.mdx`
- `/Users/ishitajindal/Documents/jaunt/docs-site/content/docs/guides/meta.json`
- `/Users/ishitajindal/Documents/jaunt/docs-site/content/docs/guides/toy-example.mdx` (optional)
- `/Users/ishitajindal/Documents/jaunt/docs-site/content/docs/guides/hackathon-demo.mdx` (optional new page)

## Deliverables

### 1) Update examples guide
In `guides/examples.mdx`:
- Introduce `jaunt-examples/` first (hackathon-ready demos).
- Keep older `examples/` section as “older runnable examples” if desired, but de-emphasize.
- Add a JWT auth section:
  - inline a short excerpt from `jaunt-examples/jwt_auth/src/jwt_demo/specs.py`
  - include the exact run commands:
    - `uv run jaunt build --root jaunt-examples/jwt_auth`
    - `PYTHONPATH=jaunt-examples/jwt_auth/src uv run jaunt test --root jaunt-examples/jwt_auth`
  - explain where skills appear:
    - `jaunt-examples/jwt_auth/.agents/skills/pydantic/SKILL.md`

### 2) Inline generated code excerpts (after generation)
After running the example once, inline a short snippet from:
- `src/**/__generated__/...` (implementation)
- `tests/__generated__/...` (generated pytest)

Keep excerpts tight: header + 10-30 lines that clearly show real code exists.

### 3) Navigation
Ensure nav lists the updated guide (and optional new page) in `guides/meta.json`.

## Quality Gates
```bash
cd docs-site && npm run build
cd docs-site && npm run types:check
```

## Constraints
- Keep explanations concise and demo-oriented.
- Do not paste giant generated files; only excerpts.

