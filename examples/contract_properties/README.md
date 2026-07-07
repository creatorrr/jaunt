# Contract properties example

Contract mode with the `properties` case kind: `Properties:` docstring bullets
become committed Hypothesis tests alongside the pinned examples. Everything in
this example is **Tier 1** — the `given <bindings> :: <invariant>` bullets parse
deterministically, so deriving needs no model and no API key.

```bash
# Derive/refresh the committed batteries (deterministic here):
jaunt reconcile

# Gate on them (deterministic, offline):
jaunt check
```

## Why properties, in one bug

`allocate` splits cents into near-equal shares. Its two pinned examples pass
with this plausible-looking body:

```python
q = int(total / parts)        # bug: truncates toward zero
r = total - q * parts
return [q + 1] * r + [q] * (parts - r)
```

`int(total / parts)` truncates toward zero while `divmod` floors, so the bug
only misallocates **negative** totals — a region no pinned example covers. The
conservation property

```
- given t: int, n: st.integers(min_value=1, max_value=50) :: sum(allocate(t, n)) == t
```

falsifies it instantly (Hypothesis shrinks to `t=-1, n=2`) and `jaunt check`
blocks with `behavior-drift`. Examples pin points; properties pin the space
between them.

## The three property idioms shown here

- **Conservation** (`allocate`): an aggregate of the output equals an aggregate
  of the input — nothing lost, nothing created.
- **Round-trip** (`chunked`, `rle_encode`/`rle_decode`): compose the function
  with its inverse (or its flatten) and land where you started. A codec's
  invariant may call its partner function — the battery imports it
  automatically.
- **Bounds** (`chunked`, `rle_encode`): a comprehension over the output holds
  everywhere (`all(len(c) <= n ...)`, counts positive, no adjacent equal runs).

Bindings map plain types to `st.from_type(...)`; an `st.`-rooted expression
(like `st.integers(min_value=1, max_value=50)` above) passes through verbatim
when the full type domain is not what the contract means.

Prose bullets (Tier 2) also work — e.g. `- No two shares ever differ by more
than one cent.` — but they call the model at `reconcile`, which transcribes the
stated invariant into the same `given … :: …` form. Review those diffs like any
model output: the oracle it writes is checked by the grammar round-trip, but
the generator ranges it picks are its own judgment.

Rendered properties always carry `derandomize=True, database=None,
deadline=None`, so `jaunt check` stays a pure function of the committed code.
