from __future__ import annotations

from pathlib import Path

from jaunt.validation import (
    validate_generated_import_provenance,
    validate_build_generated_source,
    validate_generated_source,
    validate_test_generated_source,
)


def test_validate_ok_with_expected_names() -> None:
    src = "def foo():\n    return 1\n"
    assert validate_generated_source(src, ["foo"]) == []


def test_validate_missing_name_mentions_symbol() -> None:
    src = "def foo():\n    return 1\n"
    errs = validate_generated_source(src, ["bar"])
    assert errs
    assert any("bar" in e for e in errs)


def test_validate_syntax_error_mentions_syntax() -> None:
    src = "def foo(:\n    pass\n"
    errs = validate_generated_source(src, ["foo"])
    assert errs
    joined = "\n".join(errs).lower()
    assert "syntax" in joined


def test_validate_class_and_assignment_count_as_defined() -> None:
    src = "class A:\n    pass\n\nCONSTANT = 1\n"
    assert validate_generated_source(src, ["A", "CONSTANT"]) == []


def test_validate_empty_expected_names_with_empty_source_ok() -> None:
    assert validate_generated_source("", []) == []


def test_build_validation_rejects_shadowing_handwritten_symbols() -> None:
    src = "class Mark:\n    pass\n\ndef play() -> str:\n    return 'ok'\n"
    errs = validate_build_generated_source(
        src,
        ["play"],
        spec_module="pkg.specs",
        handwritten_names={"Mark", "WIN_LINES"},
    )
    assert any("Mark" in err and "pkg.specs" in err for err in errs)


def test_generated_import_provenance_rejects_undeclared_package(tmp_path: Path) -> None:
    src = "import hallucinated_pkg\n\ndef play():\n    return 1\n"
    errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
        source_roots=[],
        first_party_modules={"pkg"},
        allowlist=[],
    )

    assert any("pkg.__generated__.specs" in err and "hallucinated_pkg" in err for err in errs)
    assert any("[project.dependencies]" in err for err in errs)


def test_generated_import_provenance_rejects_undeclared_dynamic_imports(
    tmp_path: Path,
) -> None:
    src = (
        "import importlib\n\n"
        "def play():\n"
        "    importlib.import_module('hallucinated_pkg')\n"
        "    __import__('other_hallucinated_pkg')\n"
        "    importlib.__import__('third_hallucinated_pkg')\n"
        "    return 1\n"
    )
    errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
        first_party_modules={"pkg"},
    )

    assert any("hallucinated_pkg" in err for err in errs)
    assert any("other_hallucinated_pkg" in err for err in errs)
    assert any("third_hallucinated_pkg" in err for err in errs)


def test_generated_import_provenance_allows_stdlib(tmp_path: Path) -> None:
    src = "import json\nfrom pathlib import Path\n\ndef play():\n    return Path('x')\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
        )
        == []
    )


def test_generated_import_provenance_allows_dynamic_imports_with_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['external-lib>=1,<2']\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    import jaunt.validation as validation

    monkeypatch.setattr(
        validation.metadata,
        "packages_distributions",
        lambda: {"external_lib": ["external-lib"]},
    )

    src = (
        "import importlib\n\n"
        "def play():\n"
        "    importlib.import_module('json')\n"
        "    __import__('external_lib')\n"
        "    importlib.__import__('pkg.helpers')\n"
        "    importlib.import_module('intentional_extra')\n"
        "    return 1\n"
    )

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
            allowlist=["intentional-extra"],
        )
        == []
    )


def test_generated_import_provenance_rejects_aliased_dynamic_imports(
    tmp_path: Path,
) -> None:
    src = (
        "from importlib import import_module\n"
        "from importlib import import_module as im\n"
        "import importlib as il\n\n"
        "def play():\n"
        "    import_module('aliased_a')\n"
        "    im('aliased_b')\n"
        "    il.import_module('aliased_c')\n"
        "    il.__import__('aliased_d')\n"
        "    return 1\n"
    )
    errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
        first_party_modules={"pkg"},
    )
    for name in ("aliased_a", "aliased_b", "aliased_c", "aliased_d"):
        assert any(name in err for err in errs), name


def test_generated_import_provenance_rejects_nonconstant_aliased_dynamic_import(
    tmp_path: Path,
) -> None:
    src = "from importlib import import_module as im\n\ndef play(name):\n    return im(name)\n"
    errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
        first_party_modules={"pkg"},
    )
    assert any("non-constant dynamic import" in err for err in errs)


def test_generated_import_provenance_allows_aliased_dynamic_imports_with_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['external-lib>=1,<2']\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    import jaunt.validation as validation

    monkeypatch.setattr(
        validation.metadata,
        "packages_distributions",
        lambda: {"external_lib": ["external-lib"]},
    )

    src = (
        "from importlib import import_module as im\n"
        "import importlib as il\n\n"
        "def play():\n"
        "    im('json')\n"
        "    il.import_module('external_lib')\n"
        "    im('pkg')\n"
        "    return 1\n"
    )

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
        )
        == []
    )


def test_generated_import_provenance_allows_declared_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['external-lib>=1,<2']\n",
        encoding="utf-8",
    )

    import jaunt.validation as validation

    monkeypatch.setattr(
        validation.metadata,
        "packages_distributions",
        lambda: {"external_lib": ["external-lib"]},
    )

    src = "import external_lib\n\ndef play():\n    return external_lib.VALUE\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
        )
        == []
    )


def test_generated_import_provenance_rejects_nonconstant_dynamic_import(
    tmp_path: Path,
) -> None:
    src = "import importlib\n\ndef play(name):\n    return importlib.import_module(name)\n"
    errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
    )

    assert any("non-constant dynamic import" in err for err in errs)
    assert any("provenance cannot be checked" in err for err in errs)


def test_generated_import_provenance_allows_first_party_module(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    src = "from pkg.helpers import value\n\ndef play():\n    return value\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
        )
        == []
    )


def test_build_validation_can_disable_generated_import_check(tmp_path: Path) -> None:
    src = (
        "import hallucinated_pkg\n"
        "import importlib\n\n"
        "def play(name):\n"
        "    importlib.import_module('other_hallucinated_pkg')\n"
        "    __import__(name)\n"
        "    return 1\n"
    )
    errs = validate_build_generated_source(
        src,
        ["play"],
        spec_module="pkg.specs",
        handwritten_names=(),
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
        check_imports=False,
    )

    assert errs == []


def test_generated_import_provenance_refreshes_declared_dependencies_after_pyproject_change(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\ndependencies = []\n", encoding="utf-8")
    src = "import newly_declared_pkg\n\ndef play():\n    return 1\n"

    initial_errs = validate_generated_import_provenance(
        src,
        generated_module="pkg.__generated__.specs",
        project_dir=tmp_path,
    )
    assert any("newly_declared_pkg" in err for err in initial_errs)

    pyproject.write_text(
        "[project]\ndependencies = ['newly-declared-pkg']\n",
        encoding="utf-8",
    )

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
        )
        == []
    )


def _make_uv_workspace(tmp_path: Path) -> Path:
    """Repo-root pyproject declares only ``openai``; ``pkg`` declares ``whenever``.

    Returns the spec source file under ``pkg/src/...``.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['openai']\n",
        encoding="utf-8",
    )
    pkg = tmp_path / "pkg"
    (pkg / "src" / "thing").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        "[project]\ndependencies = ['whenever']\n",
        encoding="utf-8",
    )
    spec_file = pkg / "src" / "thing" / "specs.py"
    spec_file.write_text("", encoding="utf-8")
    return spec_file


def test_generated_import_provenance_resolves_dep_from_owning_pyproject(
    tmp_path: Path,
) -> None:
    spec_file = _make_uv_workspace(tmp_path)
    src = "import whenever\n\ndef play():\n    return whenever.Instant\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="thing.__generated__.specs",
            project_dir=tmp_path,
            spec_source_file=spec_file,
        )
        == []
    )


def test_generated_import_provenance_owning_dep_rejected_without_spec_file(
    tmp_path: Path,
) -> None:
    # Regression pin: without the spec source file, the config-root pyproject
    # (declaring only openai) is consulted and the owning-package dep is rejected.
    spec_file = _make_uv_workspace(tmp_path)
    src = "import whenever\n\ndef play():\n    return whenever.Instant\n"

    errs = validate_generated_import_provenance(
        src,
        generated_module="thing.__generated__.specs",
        project_dir=tmp_path,
    )
    assert any("whenever" in err for err in errs)
    assert spec_file.exists()


def test_generated_import_provenance_root_dep_still_passes_with_spec_file(
    tmp_path: Path,
) -> None:
    spec_file = _make_uv_workspace(tmp_path)
    src = "import openai\n\ndef play():\n    return openai\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="thing.__generated__.specs",
            project_dir=tmp_path,
            spec_source_file=spec_file,
        )
        == []
    )


def test_generated_import_provenance_undeclared_still_fails_with_spec_file(
    tmp_path: Path,
) -> None:
    spec_file = _make_uv_workspace(tmp_path)
    src = "import hallucinated_pkg\n\ndef play():\n    return 1\n"

    errs = validate_generated_import_provenance(
        src,
        generated_module="thing.__generated__.specs",
        project_dir=tmp_path,
        spec_source_file=spec_file,
    )
    assert any("hallucinated_pkg" in err for err in errs)
    assert any("generated_import_allowlist" in err for err in errs)


def test_generated_import_provenance_allows_configured_allowlist(tmp_path: Path) -> None:
    src = "import intentional_extra\n\ndef play():\n    return 1\n"

    assert (
        validate_generated_import_provenance(
            src,
            generated_module="pkg.__generated__.specs",
            project_dir=tmp_path,
            allowlist=["intentional-extra"],
        )
        == []
    )


def test_test_validation_rejects_wrapper_introspection_by_default() -> None:
    src = (
        "def test_game_flow() -> None:\n"
        "    value = target.__globals__['Mark']\n"
        "    assert value is not None\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": True},
        target_modules_by_name={},
    )
    assert any("__globals__" in err for err in errs)


def test_test_validation_allows_white_box_opt_out() -> None:
    src = (
        "def test_game_flow() -> None:\n"
        "    value = target.__globals__['Mark']\n"
        "    assert value is not None\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": False},
        target_modules_by_name={},
    )
    assert errs == []


def test_test_validation_rejects_monkeypatching_target_module_attributes() -> None:
    src = (
        "import pkg.feature as feature\n\n"
        "def test_game_flow(monkeypatch) -> None:\n"
        "    monkeypatch.setattr(feature, 'helper', lambda: 1)\n"
        "    assert True\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": True},
        target_modules_by_name={"test_game_flow": ("pkg.feature",)},
    )
    assert any("monkeypatch target-module attribute" in err for err in errs)
