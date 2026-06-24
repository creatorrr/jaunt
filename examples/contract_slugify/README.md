# Contract mode example

The committed code is the source of truth; the docstring is the contract.
Jaunt derives a committed pytest battery instead of generating the body.

```bash
# Derive/refresh the committed batteries (deterministic here — structured prose,
# no API key needed):
jaunt reconcile

# Gate on them (deterministic, offline, no API key):
jaunt check

# See per-function drift state + strength score:
jaunt status
```

`slugify` has a strong contract (five pinned cases → high strength). `describe`
is deliberately weak (one example → low strength); `jaunt status` shows the gap,
and `jaunt eject contract_slugify.specs:describe` would freeze its tests as plain
pytest (with a low-strength warning).
