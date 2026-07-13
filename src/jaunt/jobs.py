"""Persisted job records for the jaunt daemon (.jaunt/jobs/*.json)."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
GREEN = "green"
LANDED = "landed"
PARKED = "parked"
FAILED = "failed"
SUPERSEDED = "superseded"
PROPOSED = "proposed"
DISCARDED = "discarded"

ACTIVE_STATES = frozenset({QUEUED, RUNNING, GREEN})
PHASE_CLEAR_STATES = frozenset({GREEN, LANDED, PARKED, FAILED, SUPERSEDED, PROPOSED, DISCARDED})


def qualified_artifact_key(language: str, module: str) -> str:
    """Return the stable cross-language identity used by daemon scheduling.

    Persisted pre-TypeScript jobs have only ``module``.  Treat those records as
    Python while keeping the qualified key separate from the legacy display
    field so existing notification and JSON consumers continue to see the same
    module name.
    """

    normalized_language = language or "py"
    prefix = f"{normalized_language}:"
    return module if module.startswith(prefix) else f"{prefix}{module}"


def split_artifact_key(value: str) -> tuple[str, str, str]:
    """Return ``(language, legacy module, qualified key)`` for a probe ID."""

    language, separator, module = value.partition(":")
    if separator and language in {"py", "ts"} and module:
        return language, module, value
    return "py", value, qualified_artifact_key("py", value)


def new_job_id(
    module: str,
    spec_digest: str,
    base_commit: str,
    *,
    language: str = "py",
    artifact_key: str = "",
) -> str:
    key = artifact_key or qualified_artifact_key(language, module)
    return hashlib.sha256(f"{key}\x00{spec_digest}\x00{base_commit}".encode()).hexdigest()[:8]


@dataclass(frozen=True)
class JobRecord:
    id: str
    module: str
    spec_digest: str
    base_commit: str
    branch: str
    state: str
    created: float
    updated: float
    language: str = "py"
    artifact_key: str = ""
    phase: str = ""
    gate: str = ""
    battery: str = ""
    landed_commit: str = ""
    error: str = ""
    detail_log: str = ""
    patch_paths: str = ""  # JSON-encoded list; set when a job parks so retry can re-land
    cause: str = ""  # human cause recorded at green time; reused for the landing commit
    refrozen: str = ""  # "1" when the green result was a re-freeze (journal action "refreeze")
    advisories: str = ""  # JSON-encoded list; set at green time
    newly_governed: str = ""  # JSON-encoded list; set at green time

    @classmethod
    def new(
        cls,
        *,
        module: str,
        spec_digest: str,
        base_commit: str,
        branch: str,
        language: str = "py",
        artifact_key: str = "",
    ) -> JobRecord:
        now = time.time()
        key = artifact_key or qualified_artifact_key(language, module)
        return cls(
            id=new_job_id(
                module,
                spec_digest,
                base_commit,
                language=language,
                artifact_key=key,
            ),
            module=module,
            spec_digest=spec_digest,
            base_commit=base_commit,
            branch=branch,
            state=QUEUED,
            created=now,
            updated=now,
            language=language,
            artifact_key=key,
        )

    @property
    def key(self) -> str:
        """Qualified identity, including for records written before this field."""

        return self.artifact_key or qualified_artifact_key(self.language, self.module)


def jobs_dir(root: Path) -> Path:
    return root / ".jaunt" / "jobs"


def _path(root: Path, job_id: str) -> Path:
    return jobs_dir(root) / f"{job_id}.json"


def save_job(root: Path, job: JobRecord) -> None:
    path = _path(root, job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(job), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_job(root: Path, job_id: str) -> JobRecord | None:
    path = _path(root, job_id)
    if not path.exists():
        return None
    try:
        return JobRecord(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def list_jobs(root: Path, states: frozenset[str] | set[str] | None = None) -> list[JobRecord]:
    directory = jobs_dir(root)
    if not directory.exists():
        return []
    records = []
    for path in directory.glob("*.json"):
        job = load_job(root, path.stem)
        if job is not None and (states is None or job.state in states):
            records.append(job)
    return sorted(records, key=lambda j: (j.created, j.id))


def active_for_module(root: Path, module: str) -> JobRecord | None:
    key = qualified_artifact_key("py", module)
    for job in list_jobs(root, states=ACTIVE_STATES):
        if job.key == key:
            return job
    return None


def active_for_artifact(root: Path, artifact_key: str) -> JobRecord | None:
    for job in list_jobs(root, states=ACTIVE_STATES):
        if job.key == artifact_key:
            return job
    return None


def parked_for_module(root: Path, module: str) -> JobRecord | None:
    key = qualified_artifact_key("py", module)
    for job in list_jobs(root, states={PARKED}):
        if job.key == key:
            return job
    return None


def parked_for_artifact(root: Path, artifact_key: str) -> JobRecord | None:
    for job in list_jobs(root, states={PARKED}):
        if job.key == artifact_key:
            return job
    return None


def proposed_for_module(root: Path, module: str) -> JobRecord | None:
    key = qualified_artifact_key("py", module)
    for job in list_jobs(root, states={PROPOSED}):
        if job.key == key:
            return job
    return None


def proposed_for_artifact(root: Path, artifact_key: str) -> JobRecord | None:
    for job in list_jobs(root, states={PROPOSED}):
        if job.key == artifact_key:
            return job
    return None


def mark(root: Path, job: JobRecord, state: str, **updates: str) -> JobRecord:
    if state in PHASE_CLEAR_STATES and "phase" not in updates:
        updates["phase"] = ""
    updated = replace(job, state=state, updated=time.time(), **updates)
    save_job(root, updated)
    return updated
