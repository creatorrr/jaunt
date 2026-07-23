from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from jaunt.cli import (
    _cmd_mixed_check,
    _cmd_typescript_test_loaded,
    _cmd_mixed_status,
    _aggregate_cost_payloads,
    _capture_python_json,
    _mixed_build_payload,
    _mixed_typescript_targets,
    _target_dispatch_mode,
    main,
    parse_args,
)
from jaunt.config import JauntConfig, load_config
from jaunt.errors import JauntConfigError
from jaunt.targets.base import (
    TargetBuildReport,
    TargetCheckReport,
    TargetDiagnostic,
    TargetTestReport,
)
from jaunt.typescript.cli_bridge import (
    build_payload,
    check_payload,
    human_lines,
    test_payload as _test_payload,
)


def test_mixed_status_preserves_typescript_diagnostics(tmp_path: Path, monkeypatch, capsys) -> None:
    from jaunt.targets.base import TargetStatus
    from jaunt.typescript import status as status_module

    monkeypatch.setattr(
        "jaunt.cli._capture_python_json",
        lambda _command, _args: (
            0,
            {
                "command": "status",
                "ok": True,
                "fresh": [],
                "stale": [],
                "stale_changes": {},
                "digests": {},
                "orphans": [],
            },
        ),
    )

    async def fake_status(*_args, **_kwargs):
        return TargetStatus(
            language="ts",
            diagnostics=(
                TargetDiagnostic(
                    code="JAUNT_TS_WARNING",
                    message="review this warning",
                    severity="warning",
                ),
            ),
        )

    monkeypatch.setattr(status_module, "run_status", fake_status)
    args = argparse.Namespace(target=[], json_output=True)

    assert _cmd_mixed_status(args, tmp_path, cast(JauntConfig, object())) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostics"] == [
        {
            "code": "JAUNT_TS_WARNING",
            "message": "review this warning",
            "severity": "warning",
        }
    ]
    assert payload["targets"]["ts"]["diagnostics"] == payload["diagnostics"]


def test_check_payload_keeps_invalid_blockers_that_differ_only_by_target() -> None:
    shared = TargetDiagnostic(
        code="JAUNT_TS_API_DRIFT",
        message="The generated API mirror drifted.",
    )
    payload = check_payload(
        TargetCheckReport(
            language="ts",
            invalid={
                "ts:src/alpha": (shared,),
                "ts:src/beta": (shared,),
            },
            exit_code=4,
        )
    )

    blockers = [item for item in payload["diagnostics"] if item["code"] == "JAUNT_TS_API_DRIFT"]
    assert [item["data"]["target"] for item in blockers] == [
        "ts:src/alpha",
        "ts:src/beta",
    ]


def test_mixed_check_reports_language_scoped_magic_diagnostics(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from jaunt.typescript import status as status_module

    monkeypatch.setattr(
        "jaunt.cli._capture_python_json",
        lambda _command, _args: (
            4,
            {
                "command": "check",
                "ok": False,
                "blocked": [],
                "checked": [],
                "orphans": [],
                "magic": {
                    "fresh": [],
                    "stale": {"pkg.specs": "prose"},
                    "unbuilt": ["pkg.new"],
                    "orphans": [],
                },
            },
        ),
    )

    async def fake_check(*_args, **_kwargs):
        return TargetCheckReport(
            language="ts",
            stale={"ts:src/slug": "structural"},
            unbuilt=frozenset({"ts:src/new"}),
            exit_code=4,
        )

    monkeypatch.setattr(status_module, "run_check", fake_check)
    args = argparse.Namespace(
        target=[],
        magic_only=False,
        contracts_only=False,
        json_output=True,
    )

    assert _cmd_mixed_check(args, tmp_path, cast(JauntConfig, object())) == 4
    payload = json.loads(capsys.readouterr().out)

    assert [item["code"] for item in payload["diagnostics"]] == [
        "JAUNT_MAGIC_STALE",
        "JAUNT_MAGIC_UNBUILT",
        "JAUNT_MAGIC_STALE",
        "JAUNT_MAGIC_UNBUILT",
    ]
    assert [item["data"]["language"] for item in payload["diagnostics"]] == [
        "py",
        "py",
        "ts",
        "ts",
    ]
    assert [item["data"]["target"] for item in payload["diagnostics"]] == [
        "py:pkg.specs",
        "py:pkg.new",
        "ts:src/slug",
        "ts:src/new",
    ]
    assert payload["targets"]["py"]["diagnostics"] == payload["diagnostics"][:2]
    assert payload["targets"]["ts"]["diagnostics"] == payload["diagnostics"][2:]
    assert payload["blocked"] == []
    assert payload["magic"]["py"]["stale"] == {"pkg.specs": "prose"}
    assert payload["magic"]["ts"]["stale"] == {"src/slug": "structural"}


def test_init_typescript_scaffolds_v2_without_mutating_package_json(tmp_path: Path, capsys) -> None:
    assert main(["init", "--language", "ts", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["language"] == "ts"
    assert payload["package_init_command"] == "npm init -y && npm pkg set type=module"
    assert payload["install_command"].startswith("npm install -D @usejaunt/ts@^0.1.0 ")
    assert not (tmp_path / "package.json").exists()
    assert (tmp_path / "src" / "index.jaunt.ts").is_file()
    assert (tmp_path / "src" / "index.context.ts").is_file()
    assert (tmp_path / "src" / "index.ts").is_file()
    assert (tmp_path / "tests" / "index.jaunt-test.ts").is_file()
    assert "jaunt.magicModule();" in (tmp_path / "tests" / "index.jaunt-test.ts").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "tsconfig.test.json").is_file()
    assert 'export * from "./index.context.js";' in (tmp_path / "src" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert "one-way leaf" in (tmp_path / "src" / "index.context.ts").read_text(encoding="utf-8")
    assert str(tmp_path / "src" / "index.context.ts") in payload["created"]

    config = load_config(root=tmp_path)
    assert config.version == 2
    assert config.target_languages == ("ts",)
    assert config.typescript_target is not None
    assert config.typescript_target.projects == ["tsconfig.json"]
    assert config.typescript_target.worker_timeout_seconds == 30
    assert config.typescript_target.worker_startup_timeout_seconds == 10


def test_init_typescript_creates_a_missing_root(tmp_path: Path, capsys) -> None:
    root = tmp_path / "new-project"

    assert main(["init", "--language", "ts", "--root", str(root), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(root / "jaunt.toml")
    assert (root / "src" / "index.jaunt.ts").is_file()


def test_init_typescript_existing_untyped_package_prints_esm_command_without_mutation(
    tmp_path: Path, capsys
) -> None:
    package_path = tmp_path / "package.json"
    original = '{\n  "name": "existing-project",\n  "private": true\n}\n'
    package_path.write_text(original, encoding="utf-8")

    assert main(["init", "--language", "ts", "--root", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["package_init_command"] == "npm pkg set type=module"
    assert package_path.read_text(encoding="utf-8") == original
    tsconfig = json.loads((tmp_path / "tsconfig.json").read_text(encoding="utf-8"))
    assert tsconfig["compilerOptions"]["verbatimModuleSyntax"] is True


def test_init_typescript_preserves_explicit_commonjs_package(tmp_path: Path, capsys) -> None:
    package_path = tmp_path / "package.json"
    original = '{"name":"existing-project","type":"commonjs"}\n'
    package_path.write_text(original, encoding="utf-8")

    assert main(["init", "--language", "ts", "--root", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["package_init_command"] is None
    assert package_path.read_text(encoding="utf-8") == original
    tsconfig = json.loads((tmp_path / "tsconfig.json").read_text(encoding="utf-8"))
    assert tsconfig["compilerOptions"]["module"] == "NodeNext"
    assert tsconfig["compilerOptions"]["verbatimModuleSyntax"] is False


def test_init_typescript_rejects_invalid_package_manifest_before_writing(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "package.json").write_text("not json\n", encoding="utf-8")

    assert main(["init", "--language", "ts", "--root", str(tmp_path), "--json"]) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "invalid" in payload["error"]
    assert not (tmp_path / "jaunt.toml").exists()
    assert not (tmp_path / "src").exists()


def test_v2_target_dispatch_defaults_and_explicit_language(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2
[target.py]
source_roots = ["src"]
test_roots = ["tests"]
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
tool_owner = "."
""",
        encoding="utf-8",
    )
    config = load_config(root=tmp_path)

    assert _target_dispatch_mode(parse_args(["status", "--root", str(tmp_path)]), config) == "mixed"
    assert (
        _target_dispatch_mode(
            parse_args(["status", "--root", str(tmp_path), "--language", "ts"]),
            config,
        )
        == "ts"
    )


def test_typescript_clean_forwards_target_selection(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
tool_owner = "."
""",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    async def fake_clean(_root, _config, **kwargs):
        from jaunt.typescript.status import CleanReport

        captured.update(kwargs)
        return CleanReport(would_remove=("src/__generated__/math.ts",))

    monkeypatch.setattr("jaunt.typescript.status.run_clean", fake_clean)

    assert (
        main(
            [
                "clean",
                "--root",
                str(tmp_path),
                "--language",
                "ts",
                "--target",
                "ts:src/math",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert captured["target_ids"] == ("ts:src/math",)
    assert captured["dry_run"] is True
    assert payload["would_remove"] == ["src/__generated__/math.ts"]


def test_typescript_build_payload_is_partitioned_and_structured() -> None:
    report = TargetBuildReport(
        language="ts",
        generated=frozenset({"ts:src/slug/index"}),
        refrozen=frozenset({"ts:src/reused/index"}),
        failed={
            "ts:src/bad/index": (
                TargetDiagnostic(code="JAUNT_TS_BAD_RETURN", message="wrong return type"),
            )
        },
        metadata={
            "recomposed": ("ts:src/reused/index",),
            "candidate_outcomes": {
                "ts:src/slug/index": {
                    "attempts": 2,
                    "retry_count": 1,
                    "retry_reasons": ("TS2322",),
                    "phase": "committed",
                }
            },
        },
        exit_code=3,
    )

    payload = build_payload(report)

    assert payload["schema_version"] == 2
    assert payload["generated"] == ["ts:src/slug/index"]
    assert payload["targets"]["ts"]["generated"] == ["src/slug/index"]
    assert payload["recomposed"] == ["ts:src/reused/index"]
    assert payload["targets"]["ts"]["recomposed"] == ["src/reused/index"]
    assert payload["failed"]["ts:src/bad/index"][0]["code"] == "JAUNT_TS_BAD_RETURN"
    assert payload["candidate_outcomes"]["ts:src/slug/index"]["attempts"] == 2
    assert payload["targets"]["ts"]["candidate_outcomes"]["src/slug/index"]["attempts"] == 2


def test_mixed_build_payload_preserves_typescript_candidate_outcomes() -> None:
    outcomes = {
        "ts:src/slug/index": {
            "attempts": 1,
            "retry_count": 0,
            "retry_reasons": (),
            "phase": "committed",
        }
    }
    payload = _mixed_build_payload(
        "build",
        {"generated": [], "skipped": [], "refrozen": [], "failed": {}},
        build_payload(TargetBuildReport(language="ts", metadata={"candidate_outcomes": outcomes})),
        exit_code=0,
    )

    assert payload["candidate_outcomes"] == outcomes
    targets = cast(dict[str, object], payload["targets"])
    typescript = cast(dict[str, object], targets["ts"])
    partitioned = cast(dict[str, dict[str, object]], typescript["candidate_outcomes"])
    assert partitioned["src/slug/index"]["attempts"] == 1


def test_typescript_test_explicit_plain_progress_stays_on_stderr_with_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        """version = 2
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
""",
        encoding="utf-8",
    )
    config = load_config(root=tmp_path)
    observed: dict[str, object] = {}

    async def fake_test(*_args, **kwargs):
        progress = kwargs["progress"]
        observed["progress"] = progress
        progress.set_total(2)
        progress.phase("tests/math.example.test.ts", "generating", "example")
        progress.advance("tests/math.example.test.ts", ok=True)
        progress.finish()
        return TargetTestReport(language="ts")

    monkeypatch.setattr("jaunt.typescript.tester.run_test", fake_test)
    args = parse_args(["test", "--language", "ts", "--progress", "plain", "--json"])

    assert _cmd_typescript_test_loaded(args, tmp_path, config) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert observed["progress"] is not None
    assert "[ts test] tests/math.example.test.ts: generating (example)" in captured.err
    assert "[ts test] 1/2" in captured.err


def test_mixed_cost_payloads_sum_language_usage() -> None:
    assert _aggregate_cost_payloads(
        {
            "api_calls": 1,
            "cache_hits": 2,
            "prompt_tokens": 10,
            "cached_prompt_tokens": 4,
            "completion_tokens": 3,
            "total_tokens": 13,
            "estimated_cost_usd": 0.125,
        },
        {
            "api_calls": 2,
            "cache_hits": 0,
            "prompt_tokens": 20,
            "cached_prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 27,
            "estimated_cost_usd": 0.25,
        },
    ) == {
        "api_calls": 3,
        "cache_hits": 2,
        "prompt_tokens": 30,
        "cached_prompt_tokens": 9,
        "completion_tokens": 10,
        "total_tokens": 40,
        "estimated_cost_usd": 0.375,
    }


def test_typescript_test_payload_exposes_aggregate_cost() -> None:
    cost = {
        "api_calls": 2,
        "cache_hits": 0,
        "prompt_tokens": 20,
        "cached_prompt_tokens": 0,
        "completion_tokens": 5,
        "total_tokens": 25,
        "estimated_cost_usd": 0.1,
    }
    payload = _test_payload(TargetTestReport(language="ts", runner={"cost": cost}))

    assert payload["cost"] == cost
    assert payload["targets"]["ts"]["cost"] == cost


def test_typescript_human_output_surfaces_npm_skill_warnings_without_mutating_payloads() -> None:
    warning = "optional npm skill 'npm-demo' not written: filesystem error"
    npm_skills = {"generated": (), "skipped": (), "removed": (), "warnings": (warning,)}
    build = build_payload(TargetBuildReport(language="ts", metadata={"npm_skills": npm_skills}))
    build_before = json.dumps(build, sort_keys=True)

    assert human_lines(build).count(f"  warning: {warning}") == 1
    assert json.dumps(build, sort_keys=True) == build_before
    assert build["npm_skills"] == npm_skills

    test = _test_payload(TargetTestReport(language="ts", runner={"npm_skills": npm_skills}))
    test_before = json.dumps(test, sort_keys=True)

    assert human_lines(test).count(f"  warning: {warning}") == 1
    assert json.dumps(test, sort_keys=True) == test_before
    assert "npm_skills" not in test
    assert test["vitest"]["npm_skills"] == npm_skills


def test_mixed_build_preflights_typescript_before_mutating_python(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2
[target.py]
source_roots = ["src"]
test_roots = ["tests"]
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
""",
        encoding="utf-8",
    )

    def fail(*args, **kwargs):
        raise JauntConfigError("install project-local @usejaunt/ts")

    python_ran = False

    def capture_python(*args, **kwargs):
        nonlocal python_ran
        python_ran = True
        return 0, {"ok": True}

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", fail)
    monkeypatch.setattr("jaunt.cli._capture_python_json", capture_python)

    assert main(["build", "--root", str(tmp_path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["targets"]["py"]["skipped"] is True
    assert payload["targets"]["ts"]["ok"] is False
    assert "@usejaunt/ts" in payload["error"]["message"]
    assert python_ran is False


def test_mixed_target_partition_does_not_expand_one_language_to_all() -> None:
    ts_args = parse_args(["build", "--target", "ts:src/token"])
    called = False

    def python_command(args):
        nonlocal called
        called = True
        return 99

    code, payload = _capture_python_json(python_command, ts_args)
    assert code == 0
    assert payload["generated"] == []
    assert called is False

    py_args = parse_args(["build", "--target", "package.module"])
    assert _mixed_typescript_targets(py_args) is None
    assert _mixed_typescript_targets(ts_args) == ("ts:src/token",)


def test_explicit_typescript_on_v1_is_a_structured_config_error(tmp_path: Path, capsys) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")

    assert main(["build", "--language", "ts", "--root", str(tmp_path), "--json"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "build"
    assert payload["ok"] is False
    assert "version-2" in payload["error"]["message"]
