"""Deterministic drift state machine for contract functions (no model)."""

from __future__ import annotations

import enum


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
    """Resolve state in precedence order (§5 of the design).

    Steps 1-3 short-circuit before the battery runs, so `battery_passed` may be
    None when an earlier hashing check already determined the state.
    """

    if not has_battery:
        return DriftState.UNBUILT
    if not prose_match:
        return DriftState.STALE_PROSE
    if not signature_match:
        return DriftState.SIGNATURE_DRIFT
    if battery_passed is False:
        return DriftState.BEHAVIOR_DRIFT
    if not body_match:
        return DriftState.REFACTORED
    return DriftState.IN_SYNC
