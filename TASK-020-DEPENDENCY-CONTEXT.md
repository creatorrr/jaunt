---
id: "020"
title: Dependency Context Plumbing
status: done
priority: 1
effort: small
depends: []
---

# TASK-020: Dependency Context Plumbing

## Problem

The builder passed `dependency_apis={}` and `dependency_generated_modules={}`
to every generation call. The dependency graph (ordering, digests, staleness)
worked, but the LLM never saw the code it depended on. It was generating blind.

This was the single biggest quality-of-generation issue.

## Solution (Implemented)

- Added `_collect_dependency_context()` in `builder.py` that:
  - Reads spec API signatures from dependency modules via `extract_source_segment()`
  - Reads already-generated source from disk or from the current build's output
- `build_one()` now passes real `dependency_apis` and `dependency_generated_modules`
- Generated source is cached in `generated_sources` dict so downstream modules
  in the same build get fresh context
- Test added: `test_builder_dep_context.py` verifies context flows through the DAG
