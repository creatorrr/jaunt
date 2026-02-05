from __future__ import annotations

import jaunt.cli


def test_parse_build_defaults() -> None:
    ns = jaunt.cli.parse_args(["build"])
    assert ns.command == "build"
    assert ns.root is None
    assert ns.config is None
    assert ns.jobs is None
    assert ns.force is False
    assert ns.target == []
    assert ns.no_infer_deps is False


def test_parse_build_flags() -> None:
    ns = jaunt.cli.parse_args(
        [
            "build",
            "--root",
            "/tmp",
            "--config",
            "/tmp/jaunt.toml",
            "--jobs",
            "3",
            "--force",
            "--target",
            "pkg.mod:foo",
            "--target",
            "pkg.other",
            "--no-infer-deps",
        ]
    )
    assert ns.command == "build"
    assert ns.root == "/tmp"
    assert ns.config == "/tmp/jaunt.toml"
    assert ns.jobs == 3
    assert ns.force is True
    assert ns.target == ["pkg.mod:foo", "pkg.other"]
    assert ns.no_infer_deps is True


def test_parse_test_defaults() -> None:
    ns = jaunt.cli.parse_args(["test"])
    assert ns.command == "test"
    assert ns.no_build is False
    assert ns.no_run is False
    assert ns.pytest_args == []


def test_parse_test_flags() -> None:
    ns = jaunt.cli.parse_args(
        [
            "test",
            "--no-build",
            "--no-run",
            "--pytest-args=-k",
            "--pytest-args",
            "foo",
        ]
    )
    assert ns.command == "test"
    assert ns.no_build is True
    assert ns.no_run is True
    assert ns.pytest_args == ["-k", "foo"]


def test_main_returns_version_exit_code_zero() -> None:
    assert jaunt.cli.main(["--version"]) == 0


def test_main_dispatches_build(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_build", lambda args: 3)
    assert jaunt.cli.main(["build"]) == 3


def test_main_dispatches_test(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_test", lambda args: 4)
    assert jaunt.cli.main(["test"]) == 4
