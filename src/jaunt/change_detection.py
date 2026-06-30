"""Layer B smart change detection for contract snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef

if TYPE_CHECKING:
    from jaunt.config import SemanticGateConfig

ExecFn = Callable[..., Awaitable[Any]]

_DEFAULT = object()


def sidecar_path(module_file: Path) -> Path:
    """Return the contract sidecar path for a generated module file."""
    return module_file.with_name(module_file.name + ".contract.json")


def read_contract_sidecar(path: Path) -> dict[str, dict]:
    """Read a fail-soft contract sidecar mapping spec refs to snapshot dicts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def write_contract_sidecar(path: Path, snapshots: dict[str, dict]) -> None:
    """Atomically write contract snapshots as pretty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(snapshots, tmp, sort_keys=True, indent=2, ensure_ascii=True)
            tmp.write("\n")

        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                tmp_path = None
        raise


def classify_change(old_snapshot: dict | None, entry: SpecEntry) -> str:
    """Classify a spec change as structural, prose-only, or unchanged."""
    from jaunt.digest import prose_digest, structural_digest

    if not old_snapshot:
        return "structural"

    cur_struct = structural_digest(entry)
    cur_prose = prose_digest(entry)
    old_struct = _snapshot_structural_digest(old_snapshot)
    old_prose = _snapshot_prose_digest(old_snapshot)

    if cur_struct != old_struct:
        return "structural"
    if cur_prose != old_prose:
        return "prose"
    return "none"


async def gate_prose(
    *,
    old_prose: str,
    new_prose: str,
    signature: str,
    cfg: SemanticGateConfig,
    run_exec: ExecFn | object = _DEFAULT,
) -> str:
    """Use a read-only semantic gate to judge whether prose changes behavior."""
    prompt = (
        "A Python symbol's behavioral contract is its docstring. Signature is unchanged: "
        f"`{signature}`. OLD docstring: `{old_prose}`. NEW docstring: `{new_prose}`. "
        "Does the NEW docstring demand any behavior the OLD one did not, or forbid/relax "
        "anything the OLD one required (different result, error, ordering, edge case, "
        "complexity, type)? Reply exactly `EQUIVALENT` or `MEANINGFUL`. If uncertain, "
        "reply `MEANINGFUL`."
    )

    if run_exec is _DEFAULT:
        from jaunt.generate.codex_backend import run_codex_exec

        exec_fn = run_codex_exec
    else:
        exec_fn = cast(ExecFn, run_exec)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = await exec_fn(
                prompt=prompt,
                cwd=tmp,
                sandbox="read-only",
                model=cfg.model,
                reasoning_effort=cfg.reasoning_effort,
                ignore_user_config=True,
            )

        final_message = getattr(result, "final_message", "")
        if not isinstance(final_message, str):
            return "MEANINGFUL"
        if final_message.strip() == "EQUIVALENT":
            return "EQUIVALENT"
        return "MEANINGFUL"
    except Exception:
        return "MEANINGFUL"


async def assess_specs(
    entries: list[SpecEntry],
    old_snapshots: dict[str, dict],
    cfg: SemanticGateConfig,
    *,
    run_exec: ExecFn | object = _DEFAULT,
) -> dict[SpecRef, str]:
    """Assess specs as behaviorally equivalent or meaningful changes."""
    verdicts: dict[SpecRef, str] = {}

    for entry in entries:
        old = old_snapshots.get(str(entry.spec_ref))
        cls = classify_change(old, entry)

        if cls == "structural":
            verdict = "MEANINGFUL"
        elif cls == "none":
            verdict = "EQUIVALENT"
        else:
            from jaunt.digest import contract_snapshot

            snap = contract_snapshot(entry)
            verdict = await gate_prose(
                old_prose=old.get("prose", "") if old else "",
                new_prose=snap.get("prose", ""),
                signature=snap.get("signature", ""),
                cfg=cfg,
                run_exec=run_exec,
            )

        verdicts[entry.spec_ref] = verdict

    return verdicts


def _snapshot_structural_digest(snapshot: dict) -> str:
    digest = snapshot.get("structural_digest")
    if isinstance(digest, str):
        return digest
    payload = {
        "kind": snapshot.get("kind", ""),
        "signature": snapshot.get("signature", ""),
        "decorator_meta": snapshot.get("decorator_meta", ""),
    }
    return _stable_digest(payload)


def _snapshot_prose_digest(snapshot: dict) -> str:
    digest = snapshot.get("prose_digest")
    if isinstance(digest, str):
        return digest
    return _stable_digest({"prose": snapshot.get("prose", "")})


def _stable_digest(payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
