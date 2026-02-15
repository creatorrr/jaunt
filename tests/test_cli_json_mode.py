"""Tests for CLI --json flag."""

from __future__ import annotations

import jaunt.cli


def test_parse_build_json_flag() -> None:
    ns = jaunt.cli.parse_args(["build", "--json"])
    assert ns.json_output is True


def test_parse_build_no_json_default() -> None:
    ns = jaunt.cli.parse_args(["build"])
    assert ns.json_output is False


def test_parse_test_json_flag() -> None:
    ns = jaunt.cli.parse_args(["test", "--json"])
    assert ns.json_output is True


def test_parse_eval_json_flag() -> None:
    ns = jaunt.cli.parse_args(["eval", "--json"])
    assert ns.json_output is True


def test_is_json_mode_helper() -> None:
    ns = jaunt.cli.parse_args(["build", "--json"])
    assert jaunt.cli._is_json_mode(ns) is True

    ns2 = jaunt.cli.parse_args(["build"])
    assert jaunt.cli._is_json_mode(ns2) is False
