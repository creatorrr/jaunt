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
