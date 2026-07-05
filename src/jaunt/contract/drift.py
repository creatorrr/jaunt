"""Deterministic drift state machine for contract functions (no model)."""

from __future__ import annotations

import enum

import jaunt

jaunt.magic_module(__name__)


class DriftState(enum.Enum):
    UNBUILT = "unbuilt"
    STALE_PROSE = "stale-prose"
    SIGNATURE_DRIFT = "signature-drift"
    BEHAVIOR_DRIFT = "behavior-drift"
    REFACTORED = "refactored"
    IN_SYNC = "in-sync"


_BLOCKING = frozenset(
    {
        DriftState.UNBUILT,
        DriftState.STALE_PROSE,
        DriftState.SIGNATURE_DRIFT,
        DriftState.BEHAVIOR_DRIFT,
    }
)

BLOCKING_MESSAGE: dict[DriftState, str] = {
    DriftState.UNBUILT: "no contract battery; run `jaunt reconcile`.",
    DriftState.STALE_PROSE: "contract prose changed; run `jaunt reconcile`.",
    DriftState.SIGNATURE_DRIFT: "signature changed; run `jaunt reconcile`.",
    DriftState.BEHAVIOR_DRIFT: "body no longer satisfies the contract; fix the body or reconcile.",
}


def is_blocking(state: DriftState) -> bool:
    return state in _BLOCKING


def compute_drift_state(
    *,
    has_battery: bool,
    prose_match: bool,
    signature_match: bool,
    body_match: bool,
    battery_passed: bool | None,
) -> DriftState:
    """Resolve the drift state of one contract function from five boolean signals.

    This is a pure, deterministic classifier (no model, no I/O): given the
    already-computed comparison signals for a single contract-tracked function,
    it returns exactly one ``DriftState`` (the enum defined above in this
    module). All arguments are keyword-only; the signature is fixed as
    ``compute_drift_state(*, has_battery: bool, prose_match: bool,
    signature_match: bool, body_match: bool, battery_passed: bool | None)
    -> DriftState``.

    Argument meaning:

    - ``has_battery`` — a committed contract battery exists for this function.
    - ``prose_match`` — the current docstring/contract prose matches what the
      battery was derived from.
    - ``signature_match`` — the current function signature matches the battery's.
    - ``body_match`` — the current implementation body matches the one recorded
      when the battery was last in sync.
    - ``battery_passed`` — tri-state result of actually running the battery:
      ``True`` (passed), ``False`` (failed), or ``None`` (not run because an
      earlier hashing check already resolved the state, so it short-circuited
      before the battery ran).

    Evaluate the following checks strictly in order and return at the FIRST one
    that matches (later signals are irrelevant once an earlier one fires):

    1. If ``not has_battery`` → ``DriftState.UNBUILT``.
    2. Else if ``not prose_match`` → ``DriftState.STALE_PROSE``.
    3. Else if ``not signature_match`` → ``DriftState.SIGNATURE_DRIFT``.
    4. Else if ``battery_passed is False`` → ``DriftState.BEHAVIOR_DRIFT``.
       (Use an identity check against ``False``; ``None`` must NOT trigger this
       branch.)
    5. Else if ``not body_match`` → ``DriftState.REFACTORED`` (the behavior still
       satisfies the contract but the implementation body changed).
    6. Otherwise → ``DriftState.IN_SYNC``.

    The precedence above means, for example, a stale-prose function whose
    signature also drifted still reports ``STALE_PROSE`` (step 2 wins over step
    3). ``battery_passed`` being ``None`` never selects ``BEHAVIOR_DRIFT`` (step
    4 requires it to be exactly ``False``); a ``None`` value falls through to the
    ``body_match`` / ``IN_SYNC`` decision.

    Examples:
    - ``compute_drift_state(has_battery=False, prose_match=True,
      signature_match=True, body_match=True, battery_passed=None)`` returns
      ``DriftState.UNBUILT``.
    - ``compute_drift_state(has_battery=True, prose_match=False,
      signature_match=False, body_match=True, battery_passed=None)`` returns
      ``DriftState.STALE_PROSE``.
    - ``compute_drift_state(has_battery=True, prose_match=True,
      signature_match=False, body_match=True, battery_passed=None)`` returns
      ``DriftState.SIGNATURE_DRIFT``.
    - ``compute_drift_state(has_battery=True, prose_match=True,
      signature_match=True, body_match=True, battery_passed=False)`` returns
      ``DriftState.BEHAVIOR_DRIFT``.
    - ``compute_drift_state(has_battery=True, prose_match=True,
      signature_match=True, body_match=False, battery_passed=True)`` returns
      ``DriftState.REFACTORED``.
    - ``compute_drift_state(has_battery=True, prose_match=True,
      signature_match=True, body_match=True, battery_passed=True)`` returns
      ``DriftState.IN_SYNC``.
    - ``compute_drift_state(has_battery=True, prose_match=True,
      signature_match=True, body_match=True, battery_passed=None)`` returns
      ``DriftState.IN_SYNC``.
    """

    raise NotImplementedError
