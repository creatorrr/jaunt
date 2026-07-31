#!/usr/bin/env python3
"""Verify model-free repair from the last published Jaunt/worker pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EXPECTED_MODULES = {
    "ts:packages/app/src/slug/index",
    "ts:packages/core/src/normalize/index",
}
EXPECTED_BATTERIES = {
    "tests/__generated__/workspace.derived.test.ts",
    "tests/__generated__/workspace.example.test.ts",
}
MUTABLE_HISTORICAL_PREFIXES = (
    "packages/app/src/slug/__generated__/",
    "packages/core/src/normalize/__generated__/",
    "tests/__generated__/",
)
IGNORED_RUNTIME_PARTS = {".jaunt", ".jaunt-vitest-cache", "dist", "node_modules"}


class VerificationError(RuntimeError):
    """A release candidate failed the historical upgrade contract."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(text: str, *, phase: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{phase} did not emit JSON: {text[-2000:]}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{phase} emitted a non-object JSON payload")
    return value


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    expected_codes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(command)
    print(f"+ {rendered}", file=sys.stderr, flush=True)
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode not in expected_codes:
        raise VerificationError(
            f"command exited {result.returncode}, expected {sorted(expected_codes)}: {rendered}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def _record_phase(
    report: dict[str, Any],
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None = None,
    expected_codes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        result = _run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            expected_codes=expected_codes,
        )
    finally:
        report["phases"][name] = {"duration_seconds": round(time.monotonic() - started, 3)}
    report["phases"][name]["exit_code"] = result.returncode
    return result


def _remaining(deadline: float, *, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VerificationError(f"repair budget exhausted before {phase}")
    return remaining


def _extract_npm_package(tarball: Path, destination: Path) -> None:
    staging = destination.with_name(f"{destination.name}.extracting")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as archive:
        members = archive.getmembers()
        if not members or any(
            member.name != "package" and not member.name.startswith("package/")
            for member in members
        ):
            raise VerificationError(f"unsafe npm tarball layout: {tarball}")
        archive.extractall(staging, filter="data")
    package = staging / "package"
    if not (package / "package.json").is_file():
        raise VerificationError(f"npm tarball has no package/package.json: {tarball}")
    shutil.rmtree(destination, ignore_errors=True)
    package.replace(destination)
    shutil.rmtree(staging, ignore_errors=True)


def _npm_pack_old_worker(download_dir: Path, *, cwd: Path) -> Path:
    result = _run(
        [
            "npm",
            "pack",
            "@usejaunt/ts@0.1.2",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            str(download_dir),
        ],
        cwd=cwd,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise VerificationError(f"unexpected npm pack response: {result.stdout[-2000:]}")
    filename = payload[0].get("filename")
    if not isinstance(filename, str):
        raise VerificationError(f"npm pack response has no filename: {payload}")
    packed = download_dir / Path(filename).name
    if not packed.is_file():
        raise VerificationError(f"npm pack did not create {packed}")
    return packed


def _verify_fixture(fixture: Path) -> dict[str, Any]:
    manifest_path = fixture / "fixture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise VerificationError("unsupported fixture manifest schema")
    if manifest.get("versions") != {"jaunt": "1.7.12", "typescript_worker": "0.1.2"}:
        raise VerificationError("fixture versions are not the expected published baseline")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise VerificationError("fixture manifest has no file hashes")
    actual_paths = {
        path.relative_to(fixture).as_posix()
        for path in fixture.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_paths != set(files):
        raise VerificationError(
            f"fixture file set differs from manifest; missing={sorted(set(files) - actual_paths)}, "
            f"unexpected={sorted(actual_paths - set(files))}"
        )
    for relative, expected in files.items():
        actual = _sha256(fixture / relative)
        if actual != expected:
            raise VerificationError(f"fixture hash differs for {relative}: {actual} != {expected}")
    return manifest


def _assert_historical_files_protected(project: Path, manifest: Mapping[str, Any]) -> None:
    files = manifest["files"]
    assert isinstance(files, dict)
    for relative, expected in files.items():
        if relative.startswith(MUTABLE_HISTORICAL_PREFIXES):
            continue
        path = project / relative
        if not path.is_file() or _sha256(path) != expected:
            raise VerificationError(f"upgrade modified protected historical file: {relative}")

    historical = set(files)
    unexpected: list[str] = []
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(project)
        relative_text = relative.as_posix()
        if relative_text in historical or relative_text == "JAUNT_LOG":
            continue
        if any(part in IGNORED_RUNTIME_PARTS for part in relative.parts):
            continue
        if relative.name.endswith(".tsbuildinfo"):
            continue
        unexpected.append(relative_text)
    if unexpected:
        raise VerificationError(f"upgrade wrote unexpected project files: {sorted(unexpected)}")


def _collect_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str):
            codes.add(code)
        for item in value.values():
            codes.update(_collect_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.update(_collect_codes(item))
    return codes


def _installed_version(python: Path) -> str:
    result = _run(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('jaunt'))",
        ],
        cwd=python.parent,
    )
    return result.stdout.strip()


def _worker_version(package_root: Path) -> str:
    payload = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    value = payload.get("version")
    if not isinstance(value, str):
        raise VerificationError(f"worker package has no version: {package_root}")
    return value


def verify(args: argparse.Namespace, report: dict[str, Any]) -> None:
    fixture = args.fixture.resolve()
    wheel = args.wheel.resolve()
    candidate_tarball = args.npm_tarball.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise VerificationError(f"candidate wheel does not exist: {wheel}")
    if not candidate_tarball.is_file() or not candidate_tarball.name.endswith(".tgz"):
        raise VerificationError(f"candidate npm tarball does not exist: {candidate_tarball}")
    manifest = _verify_fixture(fixture)

    with tempfile.TemporaryDirectory(prefix="jaunt-typescript-upgrade-") as temporary:
        sandbox = Path(temporary)
        repo_root = sandbox / "repo"
        project = repo_root / "examples" / "typescript_project_references"
        worker_root = repo_root / "packages" / "jaunt-ts"
        project.parent.mkdir(parents=True)
        shutil.copytree(fixture, project, ignore=shutil.ignore_patterns("fixture-manifest.json"))

        old_tarball = _npm_pack_old_worker(sandbox, cwd=sandbox)
        _extract_npm_package(old_tarball, worker_root)
        if _worker_version(worker_root) != "0.1.2":
            raise VerificationError("npm baseline did not install @usejaunt/ts 0.1.2")

        venv = sandbox / "venv"
        _run(["uv", "venv", "--python", "3.12", str(venv)], cwd=sandbox)
        python = venv / "bin" / "python"
        jaunt = venv / "bin" / "jaunt"
        _run(
            ["uv", "pip", "install", "--python", str(python), "jaunt==1.7.12"],
            cwd=sandbox,
        )
        if _installed_version(python) != "1.7.12":
            raise VerificationError("Python baseline did not install Jaunt 1.7.12")
        _run(["pnpm", "--dir", str(project), "install", "--frozen-lockfile"], cwd=repo_root)

        base_env = dict(os.environ)
        base_env["PATH"] = os.pathsep.join(
            [str(venv / "bin"), str(project / "node_modules" / ".bin"), base_env["PATH"]]
        )
        shim_dir = sandbox / "no-model-bin"
        shim_dir.mkdir()
        marker = sandbox / "codex-was-called"
        codex = shim_dir / "codex"
        codex.write_text(
            f"#!/bin/sh\nprintf called > {marker!s}\nexit 97\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        no_model_env = dict(base_env)
        no_model_env["PATH"] = os.pathsep.join([str(shim_dir), no_model_env["PATH"]])

        initial_baseline = _record_phase(
            report,
            "baseline_initial_check",
            [str(jaunt), "check", "--language", "ts", "--root", str(project), "--json"],
            cwd=repo_root,
            env=no_model_env,
            expected_codes=frozenset({0, 4}),
        )
        initial_payload = _load_json(initial_baseline.stdout, phase="baseline_initial_check")
        if initial_baseline.returncode == 4:
            if "JAUNT_MAGIC_STALE" not in _collect_codes(initial_payload):
                raise VerificationError(
                    f"published fixture has unexpected initial drift: {initial_payload}"
                )
            _record_phase(
                report,
                "baseline_migrate",
                [
                    str(jaunt),
                    "migrate",
                    "--language",
                    "ts",
                    "--apply",
                    "--root",
                    str(project),
                    "--json",
                ],
                cwd=repo_root,
                env=no_model_env,
                timeout=300,
            )
            _record_phase(
                report,
                "baseline_battery_refreeze",
                [
                    str(jaunt),
                    "test",
                    "--language",
                    "ts",
                    "--no-build",
                    "--root",
                    str(project),
                    "--json",
                ],
                cwd=repo_root,
                env=no_model_env,
                timeout=300,
            )

        baseline = _record_phase(
            report,
            "baseline_check",
            [str(jaunt), "check", "--language", "ts", "--root", str(project), "--json"],
            cwd=repo_root,
            env=no_model_env,
        )
        baseline_payload = _load_json(baseline.stdout, phase="baseline_check")
        if baseline_payload.get("ok") is not True:
            raise VerificationError(f"published baseline is not fresh: {baseline_payload}")
        if marker.exists():
            raise VerificationError("published baseline normalization invoked codex")

        _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--reinstall",
                "--no-deps",
                str(wheel),
            ],
            cwd=sandbox,
        )
        candidate_python_version = _installed_version(python)
        if candidate_python_version == "1.7.12":
            raise VerificationError("candidate wheel did not replace Jaunt 1.7.12")
        _extract_npm_package(candidate_tarball, worker_root)
        candidate_worker_version = _worker_version(worker_root)
        if candidate_worker_version == "0.1.2":
            raise VerificationError("candidate tarball did not replace worker 0.1.2")
        report["candidate_versions"] = {
            "jaunt": candidate_python_version,
            "typescript_worker": candidate_worker_version,
        }

        candidate_env = no_model_env

        precheck = _record_phase(
            report,
            "candidate_precheck",
            [str(jaunt), "check", "--language", "ts", "--root", str(project), "--json"],
            cwd=repo_root,
            env=candidate_env,
            expected_codes=frozenset({4}),
        )
        precheck_payload = _load_json(precheck.stdout, phase="candidate_precheck")
        codes = _collect_codes(precheck_payload)
        if "JAUNT_MAGIC_STALE" not in codes:
            raise VerificationError(
                f"candidate precheck did not expose module drift; codes={sorted(codes)}"
            )

        deadline = time.monotonic() + args.budget_seconds
        migrate = _record_phase(
            report,
            "migrate",
            [
                str(jaunt),
                "migrate",
                "--language",
                "ts",
                "--apply",
                "--root",
                str(project),
                "--json",
            ],
            cwd=repo_root,
            env=candidate_env,
            timeout=_remaining(deadline, phase="migrate"),
        )
        migrate_payload = _load_json(migrate.stdout, phase="migrate")
        free_recompose = {
            action.get("module_id")
            for action in migrate_payload.get("actions", [])
            if isinstance(action, dict) and action.get("classification") == "free-recompose"
        }
        if (
            migrate_payload.get("ok") is not True
            or migrate_payload.get("applied") is not True
            or migrate_payload.get("blocked") is not False
            or migrate_payload.get("requires_rebuild") != []
            or free_recompose != EXPECTED_MODULES
        ):
            raise VerificationError(
                f"candidate migration was not a free two-module repair: {migrate_payload}"
            )

        battery_precheck = _record_phase(
            report,
            "battery_precheck",
            [str(jaunt), "check", "--language", "ts", "--root", str(project), "--json"],
            cwd=repo_root,
            env=candidate_env,
            timeout=_remaining(deadline, phase="battery_precheck"),
            expected_codes=frozenset({0, 4}),
        )
        battery_precheck_payload = _load_json(battery_precheck.stdout, phase="battery_precheck")
        battery_codes = _collect_codes(battery_precheck_payload)
        battery_drift = "JAUNT_TS_TEST_BATTERY_STALE" in battery_codes
        if battery_precheck.returncode == 4 and not battery_drift:
            raise VerificationError(
                "candidate post-migration check did not expose battery drift; "
                f"codes={sorted(battery_codes)}"
            )
        if battery_precheck.returncode == 0 and battery_precheck_payload.get("ok") is not True:
            raise VerificationError(
                "candidate post-migration check returned inconsistent success: "
                f"{battery_precheck_payload}"
            )

        refreeze = _record_phase(
            report,
            "battery_refreeze",
            [
                str(jaunt),
                "test",
                "--language",
                "ts",
                "--no-build",
                "--root",
                str(project),
                "--json",
            ],
            cwd=repo_root,
            env=candidate_env,
            timeout=_remaining(deadline, phase="battery_refreeze"),
        )
        refreeze_payload = _load_json(refreeze.stdout, phase="battery_refreeze")
        expected_refrozen = EXPECTED_BATTERIES if battery_drift else set()
        expected_skipped = set() if battery_drift else EXPECTED_BATTERIES
        if (
            refreeze_payload.get("ok") is not True
            or refreeze_payload.get("generated") != []
            or set(refreeze_payload.get("skipped", [])) != expected_skipped
            or refreeze_payload.get("failed") != {}
            or set(refreeze_payload.get("refrozen", [])) != expected_refrozen
        ):
            raise VerificationError(
                f"candidate battery refreeze was not model-free: {refreeze_payload}"
            )

        final_check = _record_phase(
            report,
            "final_check",
            [str(jaunt), "check", "--language", "ts", "--root", str(project), "--json"],
            cwd=repo_root,
            env=candidate_env,
            timeout=_remaining(deadline, phase="final_check"),
        )
        if _load_json(final_check.stdout, phase="final_check").get("ok") is not True:
            raise VerificationError("candidate final Jaunt check did not pass")
        report["repair_duration_seconds"] = round(
            sum(
                report["phases"][name]["duration_seconds"]
                for name in ("migrate", "battery_precheck", "battery_refreeze", "final_check")
            ),
            3,
        )
        if report["repair_duration_seconds"] > args.budget_seconds:
            raise VerificationError(
                "repair took "
                f"{report['repair_duration_seconds']}s, budget is {args.budget_seconds}s"
            )

        _record_phase(
            report,
            "typecheck",
            ["pnpm", "--dir", str(project), "run", "typecheck"],
            cwd=repo_root,
            env=candidate_env,
            timeout=300,
        )
        _record_phase(
            report,
            "vitest",
            ["pnpm", "--dir", str(project), "test"],
            cwd=repo_root,
            env=candidate_env,
            timeout=300,
        )
        if marker.exists():
            raise VerificationError("model-free upgrade invoked codex")
        _assert_historical_files_protected(project, manifest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--npm-tarball", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.budget_seconds <= 0:
        raise SystemExit("--budget-seconds must be positive")
    report: dict[str, Any] = {
        "schema_version": 1,
        "ok": False,
        "budget_seconds": args.budget_seconds,
        "baseline_versions": {"jaunt": "1.7.12", "typescript_worker": "0.1.2"},
        "phases": {},
    }
    started = time.monotonic()
    try:
        verify(args, report)
        report["ok"] = True
        return_code = 0
    except (OSError, subprocess.SubprocessError, VerificationError) as exc:
        report["error"] = str(exc)
        print(f"upgrade verification failed: {exc}", file=sys.stderr)
        return_code = 1
    finally:
        report["total_duration_seconds"] = round(time.monotonic() - started, 3)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
