"""Tests for the MCP server (TASK-110).

Tests the core MCP tool functions that power the jaunt MCP server.
These functions are tested directly (without fastmcp) to keep tests fast
and dependency-free.
"""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_min_project(root: Path, *, generated_dir: str = "__generated__") -> None:
    """Create a minimal jaunt project with source roots."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "jaunt.toml").write_text(
        f'version = 1\n\n[paths]\ngenerated_dir = "{generated_dir}"\n',
        encoding="utf-8",
    )


def _make_project_with_generated(root: Path) -> None:
    """Create a project with some __generated__ dirs."""
    _make_min_project(root)
    gen = root / "src" / "pkg" / "__generated__"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "__init__.py").write_text("", encoding="utf-8")
    (gen / "specs.py").write_text("# gen\ndef Foo(): pass\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI integration: `jaunt mcp serve` parsing
# ---------------------------------------------------------------------------


class TestMCPCLIParsing:
    def test_parse_mcp_serve(self) -> None:
        ns = jaunt.cli.parse_args(["mcp", "serve"])
        assert ns.command == "mcp"
        assert ns.mcp_command == "serve"

    def test_parse_mcp_serve_with_root(self) -> None:
        ns = jaunt.cli.parse_args(["mcp", "serve", "--root", "/tmp/proj"])
        assert ns.command == "mcp"
        assert ns.mcp_command == "serve"
        assert ns.root == "/tmp/proj"

    def test_main_dispatches_mcp(self, monkeypatch) -> None:
        monkeypatch.setattr(jaunt.cli, "cmd_mcp", lambda args: 0)
        assert jaunt.cli.main(["mcp", "serve"]) == 0


# ---------------------------------------------------------------------------
# Core MCP tool functions
# ---------------------------------------------------------------------------


class TestMCPToolBuild:
    """Test jaunt_build MCP tool."""

    def test_build_returns_structured_result(self, tmp_path: Path, monkeypatch) -> None:
        """Build with no specs should return ok with empty lists."""
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_build

        result = tool_build(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "build"
        assert data["ok"] is True
        assert data["generated"] == []
        assert data["skipped"] == []
        assert data["failed"] == {}

    def test_build_with_force_flag(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_build

        result = tool_build(root=str(tmp_path), force=True)
        data = json.loads(result)
        assert data["command"] == "build"
        assert data["ok"] is True

    def test_build_missing_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        # No jaunt.toml

        from jaunt.mcp_server import tool_build

        result = tool_build(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "build"
        assert data["ok"] is False
        assert "error" in data


class TestMCPToolTest:
    """Test jaunt_test MCP tool."""

    def test_test_returns_structured_result(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_test

        result = tool_test(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "test"
        assert data["ok"] is True

    def test_test_missing_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        from jaunt.mcp_server import tool_test

        result = tool_test(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "test"
        assert data["ok"] is False
        assert "error" in data


class TestMCPToolStatus:
    """Test jaunt_status MCP tool."""

    def test_status_returns_structured_result(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_status

        result = tool_status(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "status"
        assert data["ok"] is True
        assert "stale" in data
        assert "fresh" in data

    def test_status_missing_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        from jaunt.mcp_server import tool_status

        result = tool_status(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "status"
        assert data["ok"] is False
        assert "error" in data


class TestMCPToolClean:
    """Test jaunt_clean MCP tool."""

    def test_clean_removes_generated_dirs(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project_with_generated(tmp_path)

        from jaunt.mcp_server import tool_clean

        result = tool_clean(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "clean"
        assert data["ok"] is True
        assert len(data["removed"]) >= 1
        # The generated dir should actually be gone
        assert not (tmp_path / "src" / "pkg" / "__generated__").exists()

    def test_clean_dry_run(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project_with_generated(tmp_path)

        from jaunt.mcp_server import tool_clean

        result = tool_clean(root=str(tmp_path), dry_run=True)
        data = json.loads(result)
        assert data["command"] == "clean"
        assert data["ok"] is True
        assert data["dry_run"] is True
        assert len(data["would_remove"]) >= 1
        # Files should still exist
        assert (tmp_path / "src" / "pkg" / "__generated__").exists()

    def test_clean_missing_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        from jaunt.mcp_server import tool_clean

        result = tool_clean(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "clean"
        assert data["ok"] is False
        assert "error" in data


class TestMCPToolSpecInfo:
    """Test jaunt_spec_info MCP tool."""

    def test_spec_info_no_specs(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_spec_info

        result = tool_spec_info(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "spec_info"
        assert data["ok"] is True
        assert data["specs"] == []
        assert data["dependency_graph"] == {}

    def test_spec_info_missing_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        from jaunt.mcp_server import tool_spec_info

        result = tool_spec_info(root=str(tmp_path))
        data = json.loads(result)
        assert data["command"] == "spec_info"
        assert data["ok"] is False
        assert "error" in data

    def test_spec_info_with_module_filter(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_spec_info

        result = tool_spec_info(root=str(tmp_path), module="nonexistent")
        data = json.loads(result)
        assert data["command"] == "spec_info"
        assert data["ok"] is True
        assert data["specs"] == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestRunCliJsonEdgeCases:
    """Test _run_cli_json handles non-JSON and malformed output gracefully."""

    def test_non_json_output_returns_error_envelope(self) -> None:
        from jaunt.mcp_server import _run_cli_json

        # Passing an invalid subcommand that argparse rejects — the CLI returns
        # an exit code but no JSON.  _run_cli_json should wrap it.
        result = _run_cli_json(["build", "--root", "/nonexistent/path/unlikely"])
        data = json.loads(result)
        assert data["ok"] is False
        assert "error" in data


class TestSpecInfoSysPathCleanup:
    """Verify tool_spec_info restores sys.path after execution."""

    def test_sys_path_restored(self, tmp_path: Path, monkeypatch) -> None:
        import sys

        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        from jaunt.mcp_server import tool_spec_info

        original_path = sys.path[:]
        tool_spec_info(root=str(tmp_path))
        assert sys.path == original_path


# ---------------------------------------------------------------------------
# MCP server creation
# ---------------------------------------------------------------------------


class TestMCPServerCreation:
    """Test that the MCP server object is created with correct tools."""

    def test_create_server_returns_object(self) -> None:
        from jaunt.mcp_server import create_mcp_server

        server = create_mcp_server()
        assert server is not None

    def test_server_has_expected_tool_names(self) -> None:
        import asyncio

        from jaunt.mcp_server import create_mcp_server

        server = create_mcp_server()
        # The server should expose these tools — get_tools() returns a dict keyed by name
        tools = asyncio.run(server.get_tools())
        tool_names = set(tools.keys())
        expected = {"jaunt_build", "jaunt_test", "jaunt_status", "jaunt_spec_info", "jaunt_clean"}
        assert expected <= tool_names


# ---------------------------------------------------------------------------
# Config [mcp] section
# ---------------------------------------------------------------------------


class TestMCPConfig:
    """Test that jaunt.toml supports [mcp] section."""

    def test_config_loads_mcp_enabled(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            "version = 1\n\n[mcp]\nenabled = true\n",
            encoding="utf-8",
        )
        from jaunt.config import load_config

        cfg = load_config(root=tmp_path)
        assert cfg.mcp.enabled is True

    def test_config_mcp_defaults_to_enabled(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
        from jaunt.config import load_config

        cfg = load_config(root=tmp_path)
        assert cfg.mcp.enabled is True

    def test_config_mcp_disabled(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            "version = 1\n\n[mcp]\nenabled = false\n",
            encoding="utf-8",
        )
        from jaunt.config import load_config

        cfg = load_config(root=tmp_path)
        assert cfg.mcp.enabled is False
