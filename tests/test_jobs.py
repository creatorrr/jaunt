import json
from dataclasses import asdict
from pathlib import Path

from jaunt import jobs


def _mk(
    root: Path, module: str = "recall.rank", digest: str = "abc123", base: str = "deadbeef"
) -> jobs.JobRecord:
    job = jobs.JobRecord.new(module=module, spec_digest=digest, base_commit=base, branch="main")
    jobs.save_job(root, job)
    return job


def test_new_job_id_deterministic_and_short():
    a = jobs.new_job_id("m", "d1", "c1")
    assert a == jobs.new_job_id("m", "d1", "c1")
    assert a != jobs.new_job_id("m", "d2", "c1")
    assert len(a) == 8


def test_save_load_roundtrip(tmp_path: Path):
    job = _mk(tmp_path)
    loaded = jobs.load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded == job
    assert loaded.state == jobs.QUEUED
    assert jobs.load_job(tmp_path, "nope") is None


def test_load_old_format_record_without_phase(tmp_path: Path):
    job = _mk(tmp_path)
    payload = asdict(job)
    del payload["phase"]
    (jobs.jobs_dir(tmp_path) / f"{job.id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    loaded = jobs.load_job(tmp_path, job.id)

    assert loaded is not None
    assert loaded.phase == ""


def test_list_jobs_filters_and_sorts(tmp_path: Path):
    j1 = _mk(tmp_path, module="a")
    j2 = _mk(tmp_path, module="b")
    jobs.mark(tmp_path, j2, jobs.FAILED, error="boom")
    assert [j.module for j in jobs.list_jobs(tmp_path)] == ["a", "b"]
    assert [j.module for j in jobs.list_jobs(tmp_path, states={jobs.QUEUED})] == ["a"]
    assert j1.state == jobs.QUEUED


def test_active_for_module(tmp_path: Path):
    job = _mk(tmp_path)
    active = jobs.active_for_module(tmp_path, "recall.rank")
    assert active is not None
    assert active.id == job.id
    jobs.mark(tmp_path, job, jobs.LANDED, landed_commit="c0ffee")
    assert jobs.active_for_module(tmp_path, "recall.rank") is None


def test_mark_updates_fields_and_persists(tmp_path: Path):
    job = _mk(tmp_path)
    updated = jobs.mark(tmp_path, job, jobs.GREEN, gate="MEANINGFUL", battery="47/47")
    assert updated.state == jobs.GREEN
    loaded = jobs.load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded.battery == "47/47"
    assert updated.updated >= updated.created


def test_terminal_mark_clears_phase(tmp_path: Path):
    job = _mk(tmp_path)
    running = jobs.mark(tmp_path, job, jobs.RUNNING, phase="[build] app: generating")

    failed = jobs.mark(tmp_path, running, jobs.FAILED, error="boom")

    assert failed.phase == ""
    loaded = jobs.load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded.phase == ""


def test_proposed_not_active_and_phase_clearing(tmp_path: Path):
    job = _mk(tmp_path, module="m")
    updated = jobs.mark(tmp_path, job, jobs.PROPOSED, cause="spec change", refrozen="")
    assert updated.state == jobs.PROPOSED
    assert jobs.PROPOSED not in jobs.ACTIVE_STATES
    assert jobs.DISCARDED not in jobs.ACTIVE_STATES
    assert jobs.PROPOSED in jobs.PHASE_CLEAR_STATES
    assert jobs.DISCARDED in jobs.PHASE_CLEAR_STATES
    assert updated.cause == "spec change"
    assert updated.refrozen == ""
    found = jobs.proposed_for_module(tmp_path, "m")
    assert found is not None
    assert found.id == job.id


def test_proposed_for_module_none_when_absent(tmp_path: Path):
    _mk(tmp_path, module="m")
    assert jobs.proposed_for_module(tmp_path, "m") is None
    assert jobs.proposed_for_module(tmp_path, "other") is None


def test_old_job_records_load_without_new_fields(tmp_path: Path):
    job = _mk(tmp_path, module="m")
    raw = json.loads((jobs.jobs_dir(tmp_path) / f"{job.id}.json").read_text(encoding="utf-8"))
    del raw["cause"], raw["refrozen"]
    (jobs.jobs_dir(tmp_path) / f"{job.id}.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = jobs.load_job(tmp_path, job.id)
    assert loaded is not None
    assert loaded.cause == ""
    assert loaded.refrozen == ""


def test_old_job_records_default_to_qualified_python_identity(tmp_path: Path):
    job = _mk(tmp_path, module="m")
    raw = json.loads((jobs.jobs_dir(tmp_path) / f"{job.id}.json").read_text(encoding="utf-8"))
    del raw["language"], raw["artifact_key"]
    (jobs.jobs_dir(tmp_path) / f"{job.id}.json").write_text(json.dumps(raw), encoding="utf-8")

    loaded = jobs.load_job(tmp_path, job.id)

    assert loaded is not None
    assert loaded.language == "py"
    assert loaded.key == "py:m"


def test_artifact_lookups_do_not_collide_across_languages(tmp_path: Path):
    py_job = _mk(tmp_path, module="pkg.token", digest="py")
    ts_job = jobs.JobRecord.new(
        module="pkg.token",
        language="ts",
        artifact_key="ts:pkg.token",
        spec_digest="ts",
        base_commit="deadbeef",
        branch="main",
    )
    jobs.save_job(tmp_path, ts_job)

    assert jobs.active_for_artifact(tmp_path, "py:pkg.token") == py_job
    assert jobs.active_for_artifact(tmp_path, "ts:pkg.token") == ts_job
    assert py_job.id != ts_job.id
