"""Thorough tests for async function support in @magic and @test decorators."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.errors import JauntError, JauntNotBuiltError
from jaunt.registry import clear_registries, get_magic_registry, get_test_registry
from jaunt.runtime import magic
from jaunt.runtime import test as jaunt_test
from jaunt.spec_ref import normalize_spec_ref

# --- Top-level async function specs used by tests ---


async def async_top_level_fn(x: int) -> int:
    """An async spec stub."""
    return x + 1


async def another_async_fn(name: str) -> str:
    """Another async spec stub."""
    return name


def sync_top_level_fn(x: int) -> int:
    """A normal sync stub for comparison."""
    return x + 1


async def async_test_spec() -> None:
    """An async test spec stub."""
    return None


@pytest.fixture(autouse=True)
def _clear_registries() -> Generator[None, None, None]:
    clear_registries()
    yield
    clear_registries()


# ========================= @magic async tests =========================


class TestMagicAsyncRegistration:
    """Tests that @magic properly registers async function specs."""

    def test_registers_async_function_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        reg = get_magic_registry()
        expected_ref = normalize_spec_ref(
            f"{async_top_level_fn.__module__}:{async_top_level_fn.__qualname__}"
        )
        assert expected_ref in reg
        assert reg[expected_ref].kind == "magic"
        assert callable(wrapped)

    def test_async_spec_entry_stores_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        magic()(async_top_level_fn)
        expected_ref = normalize_spec_ref(
            f"{async_top_level_fn.__module__}:{async_top_level_fn.__qualname__}"
        )
        entry = get_magic_registry()[expected_ref]
        assert entry.obj is async_top_level_fn

    def test_decorator_kwargs_stored_for_async(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        magic(deps="pkg.mod:Dep", prompt="implement async", infer_deps=False)(async_top_level_fn)
        expected_ref = normalize_spec_ref(
            f"{async_top_level_fn.__module__}:{async_top_level_fn.__qualname__}"
        )
        got = get_magic_registry()[expected_ref]
        assert got.decorator_kwargs == {
            "deps": "pkg.mod:Dep",
            "prompt": "implement async",
            "infer_deps": False,
        }


class TestMagicAsyncWrapper:
    """Tests that @magic returns async wrappers for async functions."""

    def test_wrapper_is_coroutine_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        assert inspect.iscoroutinefunction(wrapped)

    def test_sync_wrapper_is_not_coroutine_function(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(sync_top_level_fn)
        assert not inspect.iscoroutinefunction(wrapped)

    def test_wrapper_preserves_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        assert wrapped.__name__ == async_top_level_fn.__name__
        assert wrapped.__wrapped__ is async_top_level_fn

    def test_wrapper_returns_coroutine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        result = wrapped(42)
        assert inspect.iscoroutine(result)
        # Clean up the coroutine to avoid RuntimeWarning
        result.close()


class TestMagicAsyncUnbuilt:
    """Tests that unbuilt async @magic specs raise proper errors."""

    def test_unbuilt_async_raises_not_built_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        with pytest.raises(JauntNotBuiltError) as exc:
            asyncio.run(wrapped(1))
        assert "jaunt build" in str(exc.value)

    def test_unbuilt_async_attribute_error_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AttributeError during getattr also raises JauntNotBuiltError."""

        def _import(_name: str) -> Any:
            return SimpleNamespace()  # Module without the expected attribute

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        with pytest.raises(JauntNotBuiltError):
            asyncio.run(wrapped(1))


class TestMagicAsyncBuilt:
    """Tests that built async @magic specs correctly forward to generated code."""

    def test_built_async_forwards_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def gen_fn(x: int) -> int:
            return x + 100

        def _import(_name: str) -> Any:
            return SimpleNamespace(**{async_top_level_fn.__qualname__: gen_fn})

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        result = asyncio.run(wrapped(1))
        assert result == 101

    def test_built_async_with_string_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def gen_fn(name: str) -> str:
            return f"hello {name}"

        def _import(_name: str) -> Any:
            return SimpleNamespace(**{another_async_fn.__qualname__: gen_fn})

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(another_async_fn)
        result = asyncio.run(wrapped("world"))
        assert result == "hello world"

    def test_built_async_preserves_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def gen_fn(x: int) -> int:
            raise ValueError("test error")

        def _import(_name: str) -> Any:
            return SimpleNamespace(**{async_top_level_fn.__qualname__: gen_fn})

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        with pytest.raises(ValueError, match="test error"):
            asyncio.run(wrapped(1))

    def test_built_async_passes_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def gen_fn(x: int) -> int:
            return x * 2

        def _import(_name: str) -> Any:
            return SimpleNamespace(**{async_top_level_fn.__qualname__: gen_fn})

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        result = asyncio.run(wrapped(x=21))
        assert result == 42


class TestMagicAsyncEdgeCases:
    """Edge case tests for async @magic decorator."""

    def test_rejects_nested_async_objects(self) -> None:
        """Nested async functions should be rejected."""

        async def inner() -> None:
            return None

        with pytest.raises(JauntError):
            magic()(inner)

    def test_async_and_sync_can_coexist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both async and sync specs can be registered in the same session."""

        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        async_wrapped = magic()(async_top_level_fn)
        sync_wrapped = magic()(sync_top_level_fn)

        assert inspect.iscoroutinefunction(async_wrapped)
        assert not inspect.iscoroutinefunction(sync_wrapped)

        reg = get_magic_registry()
        assert len(reg) == 2

    def test_runtime_respects_generated_dir_env_var_for_async(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAUNT_GENERATED_DIR", "__custom_gen__")

        import_calls: list[str] = []

        def _import(name: str) -> Any:
            import_calls.append(name)
            raise ModuleNotFoundError(name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(async_top_level_fn)
        with pytest.raises(JauntNotBuiltError):
            asyncio.run(wrapped(1))

        assert any("__custom_gen__" in c for c in import_calls), (
            f"Expected import to use __custom_gen__, got: {import_calls}"
        )


# ========================= @test async tests =========================


class TestTestAsyncRegistration:
    """Tests that @test properly handles async test specs."""

    def test_registers_async_test_spec(self) -> None:
        fn = jaunt_test()(async_test_spec)
        assert fn is async_test_spec
        assert callable(fn)
        assert fn.__test__ is False

        expected_ref = normalize_spec_ref(f"{fn.__module__}:{fn.__qualname__}")
        reg = get_test_registry()
        assert expected_ref in reg
        assert reg[expected_ref].kind == "test"

    def test_async_test_spec_preserves_coroutine_nature(self) -> None:
        fn = jaunt_test()(async_test_spec)
        assert inspect.iscoroutinefunction(fn)

    def test_async_test_specs_do_not_leak_into_magic_registry(self) -> None:
        jaunt_test()(async_test_spec)
        assert get_magic_registry() == {}

    def test_stores_deps_in_decorator_kwargs_for_async(self) -> None:
        jaunt_test(deps=["a.b:One", "a.b:Two"])(async_test_spec)
        expected_ref = normalize_spec_ref(
            f"{async_test_spec.__module__}:{async_test_spec.__qualname__}"
        )
        got = get_test_registry()[expected_ref]
        assert got.decorator_kwargs == {"deps": ["a.b:One", "a.b:Two"]}

    def test_async_test_spec_object_is_coroutine_function(self) -> None:
        jaunt_test()(async_test_spec)
        expected_ref = normalize_spec_ref(
            f"{async_test_spec.__module__}:{async_test_spec.__qualname__}"
        )
        entry = get_test_registry()[expected_ref]
        assert inspect.iscoroutinefunction(entry.obj)


# ========================= Config tests =========================


class TestAsyncRunnerConfig:
    """Tests for async_runner configuration."""

    def test_default_async_runner_is_asyncio(self, tmp_path: Any) -> None:
        from jaunt.config import load_config

        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
        cfg = load_config(root=tmp_path)
        assert cfg.build.async_runner == "asyncio"

    def test_async_runner_asyncio_explicit(self, tmp_path: Any) -> None:
        from jaunt.config import load_config

        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            'version = 1\n[build]\nasync_runner = "asyncio"\n', encoding="utf-8"
        )
        cfg = load_config(root=tmp_path)
        assert cfg.build.async_runner == "asyncio"

    def test_async_runner_anyio(self, tmp_path: Any) -> None:
        from jaunt.config import load_config

        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            'version = 1\n[build]\nasync_runner = "anyio"\n', encoding="utf-8"
        )
        cfg = load_config(root=tmp_path)
        assert cfg.build.async_runner == "anyio"

    def test_async_runner_invalid_raises(self, tmp_path: Any) -> None:
        from jaunt.config import load_config
        from jaunt.errors import JauntConfigError

        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            'version = 1\n[build]\nasync_runner = "trio"\n', encoding="utf-8"
        )
        with pytest.raises(JauntConfigError, match="async_runner"):
            load_config(root=tmp_path)

    def test_async_runner_invalid_type_raises(self, tmp_path: Any) -> None:
        from jaunt.config import load_config
        from jaunt.errors import JauntConfigError

        (tmp_path / "src").mkdir()
        (tmp_path / "jaunt.toml").write_text(
            "version = 1\n[build]\nasync_runner = 42\n", encoding="utf-8"
        )
        with pytest.raises(JauntConfigError, match="string"):
            load_config(root=tmp_path)


# ========================= Validation tests =========================


class TestAsyncValidation:
    """Tests that validation correctly handles async function definitions."""

    def test_validate_async_function_def(self) -> None:
        from jaunt.validation import validate_generated_source

        src = "async def fetch_data():\n    return []\n"
        assert validate_generated_source(src, ["fetch_data"]) == []

    def test_validate_multiple_async_and_sync(self) -> None:
        from jaunt.validation import validate_generated_source

        src = (
            "async def fetch_data():\n    return []\n\n"
            "def process_data(data):\n    return data\n\n"
            "async def save_data(data):\n    pass\n"
        )
        assert validate_generated_source(src, ["fetch_data", "process_data", "save_data"]) == []

    def test_validate_missing_async_name(self) -> None:
        from jaunt.validation import validate_generated_source

        src = "async def foo():\n    pass\n"
        errs = validate_generated_source(src, ["bar"])
        assert errs
        assert any("bar" in e for e in errs)


# ========================= ModuleSpecContext tests =========================


class TestModuleSpecContextAsyncRunner:
    """Tests that ModuleSpecContext carries async_runner."""

    def test_default_async_runner(self) -> None:
        from jaunt.generate.base import ModuleSpecContext

        ctx = ModuleSpecContext(
            kind="build",
            spec_module="pkg.specs",
            generated_module="pkg.__generated__.specs",
            expected_names=["foo"],
            spec_sources={},
            decorator_prompts={},
            dependency_apis={},
            dependency_generated_modules={},
        )
        assert ctx.async_runner == "asyncio"

    def test_custom_async_runner(self) -> None:
        from jaunt.generate.base import ModuleSpecContext

        ctx = ModuleSpecContext(
            kind="test",
            spec_module="pkg.specs",
            generated_module="pkg.__generated__.specs",
            expected_names=["foo"],
            spec_sources={},
            decorator_prompts={},
            dependency_apis={},
            dependency_generated_modules={},
            async_runner="anyio",
        )
        assert ctx.async_runner == "anyio"


# ========================= Prompt rendering tests =========================


class TestAsyncTestInfo:
    """Tests that async_test_info helper returns correct guidance."""

    def test_asyncio_info(self) -> None:
        from jaunt.generate.shared import async_test_info

        info = async_test_info("asyncio")
        assert "pytest.mark.asyncio" in info
        assert "pytest-asyncio" in info

    def test_anyio_info(self) -> None:
        from jaunt.generate.shared import async_test_info

        info = async_test_info("anyio")
        assert "pytest.mark.anyio" in info
        assert "anyio" in info

    def test_default_falls_back_to_asyncio(self) -> None:
        from jaunt.generate.shared import async_test_info

        info = async_test_info("unknown_runner")
        assert "pytest.mark.asyncio" in info


class TestPromptRendering:
    """Tests that prompts correctly include async test info."""

    def test_build_system_mentions_async(self) -> None:
        from jaunt.generate.shared import load_prompt

        prompt = load_prompt("build_system.md", None)
        assert "async def" in prompt

    def test_build_module_mentions_async(self) -> None:
        from jaunt.generate.shared import load_prompt

        prompt = load_prompt("build_module.md", None)
        assert "async def" in prompt

    def test_test_system_has_async_test_info_placeholder(self) -> None:
        from jaunt.generate.shared import load_prompt

        prompt = load_prompt("test_system.md", None)
        assert "{{async_test_info}}" in prompt

    def test_test_module_has_async_test_info_placeholder(self) -> None:
        from jaunt.generate.shared import load_prompt

        prompt = load_prompt("test_module.md", None)
        assert "{{async_test_info}}" in prompt

    def test_rendered_test_prompt_includes_asyncio_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaunt.config import LLMConfig
        from jaunt.generate.base import ModuleSpecContext
        from jaunt.generate.openai_backend import OpenAIBackend
        from jaunt.spec_ref import normalize_spec_ref

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        backend = OpenAIBackend(
            LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
        )
        foo_ref = normalize_spec_ref("pkg.specs:foo")
        ctx = ModuleSpecContext(
            kind="test",
            spec_module="pkg.specs",
            generated_module="pkg.__generated__.specs",
            expected_names=["foo"],
            spec_sources={foo_ref: "async def foo() -> int:\n    pass\n"},
            decorator_prompts={},
            dependency_apis={},
            dependency_generated_modules={},
            async_runner="asyncio",
        )
        messages = backend._render_messages(ctx, extra_error_context=None)
        rendered = "\n".join(m["content"] for m in messages)
        assert "pytest.mark.asyncio" in rendered

    def test_rendered_test_prompt_includes_anyio_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaunt.config import LLMConfig
        from jaunt.generate.base import ModuleSpecContext
        from jaunt.generate.openai_backend import OpenAIBackend
        from jaunt.spec_ref import normalize_spec_ref

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        backend = OpenAIBackend(
            LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
        )
        foo_ref = normalize_spec_ref("pkg.specs:foo")
        ctx = ModuleSpecContext(
            kind="test",
            spec_module="pkg.specs",
            generated_module="pkg.__generated__.specs",
            expected_names=["foo"],
            spec_sources={foo_ref: "async def foo() -> int:\n    pass\n"},
            decorator_prompts={},
            dependency_apis={},
            dependency_generated_modules={},
            async_runner="anyio",
        )
        messages = backend._render_messages(ctx, extra_error_context=None)
        rendered = "\n".join(m["content"] for m in messages)
        assert "pytest.mark.anyio" in rendered


# ========================= Digest / dependency tests =========================


class TestAsyncDigest:
    """Tests that digest extraction works for async functions."""

    def test_extract_source_segment_for_async(self, tmp_path: Any) -> None:
        from jaunt.digest import extract_source_segment
        from jaunt.registry import SpecEntry
        from jaunt.spec_ref import normalize_spec_ref

        src = 'async def fetch(url: str) -> str:\n    """Fetch a URL."""\n    ...\n'
        f = tmp_path / "specs.py"
        f.write_text(src, encoding="utf-8")

        entry = SpecEntry(
            kind="magic",
            spec_ref=normalize_spec_ref("specs:fetch"),
            module="specs",
            qualname="fetch",
            source_file=str(f),
            obj=None,
            decorator_kwargs={},
        )
        segment = extract_source_segment(entry)
        assert "async def fetch" in segment
        assert "Fetch a URL" in segment
