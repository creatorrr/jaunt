from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import jaunt.skills_npm as skills_npm
from jaunt.cache import CacheEntry, ResponseCache
from jaunt.cost import CostTracker
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import (
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
    generation_request_cache_key,
)
from jaunt.generate.request_cache import generate_request_cached
from jaunt.skills_npm import ensure_npm_skills, plan_npm_skills
from jaunt.typescript.builder import _build_request, _generation_fingerprint, _model_contract
from jaunt.typescript.contracts import _contract_generation_fingerprint


class _RequestBackend(GeneratorBackend):
    def __init__(self, source: str = "valid") -> None:
        self.source = source
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "gpt-5.6-sol"

    @property
    def provider_name(self) -> str:
        return "codex"

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> Any:
        raise AssertionError((ctx, extra_error_context))

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any) -> Any:
        self.calls += 1
        return (
            self.source,
            TokenUsage(4, 2, self.model_name, self.provider_name),
        )


def _request(root: Path) -> GenerationRequest:
    return GenerationRequest(
        language="ts",
        kind="build",
        target_path="src/out.ts",
        context_files={"_context/spec.ts": "export declare function value(): number;"},
        prompt="implement value",
        cache_payload={"moduleId": "ts:src/value"},
        validator=lambda source: [] if source == "valid" else ["invalid overlay"],
        project_root=root,
    )


@pytest.mark.asyncio
async def test_invalid_cached_request_is_rejected_and_valid_final_bytes_replace_it(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    backend = _RequestBackend()
    cache = ResponseCache(tmp_path / "cache")
    fingerprint = "runner-v1"
    key = generation_request_cache_key(
        request,
        model=backend.model_name,
        provider=backend.provider_name,
        generation_fingerprint=fingerprint,
    )
    cache.put(key, CacheEntry("bad", 1, 1, "old", "old", 0.0))
    cost = CostTracker()
    phases: list[tuple[str, str]] = []

    result = await generate_request_cached(
        backend,
        request,
        max_attempts=1,
        generation_fingerprint=fingerprint,
        response_cache=cache,
        cost_tracker=cost,
        progress=lambda stage, detail: phases.append((stage, detail)),
    )

    assert result.source == "valid"
    assert backend.calls == 1
    assert cost.api_calls == 1
    assert cost.cache_hits == 0
    assert any(stage == "cache rejected" for stage, _detail in phases)
    assert cache.get(key).source == "valid"  # type: ignore[union-attr]

    cached_cost = CostTracker()
    second = await generate_request_cached(
        _RequestBackend("should-not-run"),
        request,
        max_attempts=1,
        generation_fingerprint=fingerprint,
        response_cache=cache,
        cost_tracker=cached_cost,
    )
    assert second.source == "valid"
    assert second.attempts == 0
    assert cached_cost.cache_hits == 1


@pytest.mark.asyncio
async def test_invalid_cached_request_is_evicted_when_replacement_generation_fails(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    backend = _RequestBackend("bad")
    cache = ResponseCache(tmp_path / "cache")
    fingerprint = "runner-v1"
    key = generation_request_cache_key(
        request,
        model=backend.model_name,
        provider=backend.provider_name,
        generation_fingerprint=fingerprint,
    )
    cache.put(key, CacheEntry("bad", 1, 1, "old", "old", 0.0))

    result = await generate_request_cached(
        backend,
        request,
        max_attempts=1,
        generation_fingerprint=fingerprint,
        response_cache=cache,
    )

    assert result.errors == ["invalid overlay"]
    assert cache.info()["entries"] == 0
    assert ResponseCache(tmp_path / "cache").get(key) is None


@pytest.mark.asyncio
async def test_request_retries_charge_each_attempt_and_stop_at_budget(
    tmp_path: Path,
) -> None:
    class RetryBackend(_RequestBackend):
        async def generate_request(self, request: GenerationRequest, **_kwargs: Any) -> Any:
            self.calls += 1
            return (
                "bad" if self.calls == 1 else "valid",
                TokenUsage(4, 2, self.model_name, self.provider_name),
            )

    request = _request(tmp_path)
    backend = RetryBackend()
    cost = CostTracker()
    result = await generate_request_cached(
        backend,
        request,
        max_attempts=3,
        generation_fingerprint="runner-v1",
        cost_tracker=cost,
        usage_label="ts:src/value",
    )
    assert result.source == "valid"
    assert backend.calls == 2
    assert cost.api_calls == 2

    budgeted_backend = RetryBackend()
    budgeted = CostTracker(max_cost=0.000001)
    with pytest.raises(JauntGenerationError, match="exceeds budget"):
        await generate_request_cached(
            budgeted_backend,
            request,
            max_attempts=3,
            generation_fingerprint="runner-v2",
            cost_tracker=budgeted,
            usage_label="ts:src/value",
        )
    assert budgeted_backend.calls == 1
    assert budgeted.api_calls == 1


def test_build_request_and_fingerprint_include_effective_context_and_skills(
    tmp_path: Path,
) -> None:
    from jaunt.config import load_config

    (tmp_path / "jaunt.toml").write_text(
        """version = 2
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
"""
    )
    config = load_config(root=tmp_path)
    module = {
        "moduleId": "ts:src/value",
        "implementationPath": "src/__generated__/value.ts",
        "symbols": [{"name": "value"}],
        "toolingProvenanceRecords": [
            {"id": "tooling:packageManager:package.json", "digest": "sha256:pnpm"}
        ],
        "sidecar": json.dumps(
            {
                "moduleId": "ts:src/value",
                "toolingProvenanceRecords": [
                    {
                        "id": "tooling:packageManager:package.json",
                        "digest": "sha256:pnpm",
                    }
                ],
            }
        ),
        "specSource": "export declare function value(): number;",
        "apiSource": "export declare function value(): number;",
    }
    request = _build_request(
        tmp_path,
        config,
        module,
        {"ts:src/value": module},
        lambda _source: pytest.fail("not called"),
        repo_map_block="## Repository map\nsrc/value.jaunt.ts",
        project_overview_block="A small value library.",
        builtin_skill_names=(),
    )
    assert request.builtin_skill_names == ()
    assert "_context/repository-map.md" in request.context_files
    assert "_context/project-overview.md" in request.context_files
    assert "toolingProvenanceRecords" not in request.context_files["_context/contract.json"]
    enabled = _generation_fingerprint(
        config,
        root=tmp_path,
        builtin_skill_names=(),
        repo_map_enabled=True,
        project_overview_enabled=True,
    )
    disabled = _generation_fingerprint(
        config,
        root=tmp_path,
        builtin_skill_names=(),
        repo_map_enabled=False,
        project_overview_enabled=False,
    )
    assert enabled != disabled


@pytest.mark.parametrize(
    "sidecar",
    [
        "{malformed toolingProvenanceRecords",
        json.dumps(["toolingProvenanceRecords"]),
        json.dumps(
            {
                "moduleId": "ts:src/value",
                "toolingProvenanceRecords": [
                    {
                        "id": "tooling:packageManager:package.json",
                        "digest": "sha256:pnpm",
                    }
                ],
            }
        ),
    ],
)
def test_model_contract_fails_closed_for_tooling_sidecar_provenance(sidecar: str) -> None:
    rendered = json.dumps(
        _model_contract(
            {
                "moduleId": "ts:src/value",
                "toolingProvenanceRecords": [],
                "sidecar": sidecar,
            }
        ),
        sort_keys=True,
    )

    assert "toolingProvenanceRecords" not in rendered


def test_npm_skill_names_disambiguate_scoped_collision_and_write_atomically(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"@foo/bar":"1.0.0","foo-bar":"1.0.0"}}\n')
    for package, relative in (("@foo/bar", "@foo/bar"), ("foo-bar", "foo-bar")):
        package_root = owner / "node_modules" / relative
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_text(
            f'{{"name":"{package}","version":"1.0.0","description":"demo"}}\n'
        )
        (package_root / "README.md").write_text(f"# {package}\n")

    result = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))

    assert len(result.generated) == 2
    assert len(set(result.generated)) == 2
    assert all(name.startswith("npm-foo-bar-") for name in result.generated)
    assert not list((tmp_path / ".agents" / "skills").rglob(".jaunt-tmp-*"))


def _install_npm_fixture(owner: Path, package: str) -> None:
    package_root = owner / "node_modules" / Path(*package.split("/"))
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "package.json").write_text(
        f'{{"name":"{package}","version":"1.0.0","description":"demo"}}\n'
    )
    (package_root / "README.md").write_text(f"# {package}\n")


def test_npm_skill_plan_reports_files_and_bytes_without_writing(tmp_path: Path) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"one":"1.0.0"}}\n')
    _install_npm_fixture(owner, "one")

    plan = plan_npm_skills(project_root=tmp_path, package_owners=(owner,))

    assert plan.file_count == 1
    assert plan.total_bytes > 0
    assert plan.packages == ("one",)
    assert not (tmp_path / ".agents").exists()

    result = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert result.metadata()["plan"] == {
        "file_count": plan.file_count,
        "total_bytes": plan.total_bytes,
    }


def test_npm_skill_reconciliation_removes_only_stale_managed_skills(tmp_path: Path) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"keep":"1.0.0","remove":"1.0.0"}}\n')
    _install_npm_fixture(owner, "keep")
    _install_npm_fixture(owner, "remove")
    first = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert set(first.generated) == {"npm-keep", "npm-remove"}

    user_skill = tmp_path / ".agents/skills/user/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user\n---\nuser owned\n")
    pypi_skill = tmp_path / ".agents/skills/requests/SKILL.md"
    pypi_skill.parent.mkdir(parents=True)
    pypi_skill.write_text(
        "---\nname: requests\nx-jaunt-dist: requests\nx-jaunt-version: 1.0\n---\n"
    )

    (owner / "package.json").write_text('{"dependencies":{"keep":"1.0.0"}}\n')
    second = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))

    assert second.removed == ("npm-remove",)
    assert not (tmp_path / ".agents/skills/npm-remove").exists()
    assert (tmp_path / ".agents/skills/npm-keep/SKILL.md").is_file()
    assert user_skill.is_file()
    assert pypi_skill.is_file()


def test_npm_skill_reconciliation_handles_collision_transitions(tmp_path: Path) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    _install_npm_fixture(owner, "foo-bar")
    _install_npm_fixture(owner, "@foo/bar")
    (owner / "package.json").write_text('{"dependencies":{"foo-bar":"1.0.0"}}\n')

    initial = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert initial.generated == ("npm-foo-bar",)

    (owner / "package.json").write_text('{"dependencies":{"foo-bar":"1.0.0","@foo/bar":"1.0.0"}}\n')
    collided = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    collided_names = set(collided.generated)
    assert collided.removed == ("npm-foo-bar",)
    assert len(collided_names) == 2
    assert all(name.startswith("npm-foo-bar-") for name in collided_names)
    assert not (tmp_path / ".agents/skills/npm-foo-bar/SKILL.md").exists()

    (owner / "package.json").write_text('{"dependencies":{"foo-bar":"1.0.0"}}\n')
    uncollided = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert set(uncollided.removed) == collided_names
    assert uncollided.generated == ("npm-foo-bar",)
    assert (tmp_path / ".agents/skills/npm-foo-bar/SKILL.md").is_file()
    assert all(not (tmp_path / ".agents/skills" / name).exists() for name in collided_names)


def test_npm_skill_reconciliation_warns_when_stale_skill_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"keep":"1.0.0","remove":"1.0.0"}}\n')
    _install_npm_fixture(owner, "keep")
    _install_npm_fixture(owner, "remove")
    initial = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert set(initial.generated) == {"npm-keep", "npm-remove"}

    user_skill = tmp_path / ".agents/skills/user/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user\n---\nuser owned\n")
    stale_skill = tmp_path / ".agents/skills/npm-remove/SKILL.md"
    stale_bytes = stale_skill.read_bytes()
    original_unlink = Path.unlink

    def deny_stale_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == stale_skill:
            raise PermissionError("read-only Codex skills workspace")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_stale_unlink)
    (owner / "package.json").write_text('{"dependencies":{"keep":"1.0.0"}}\n')

    result = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))

    warning = "optional npm skill 'npm-remove' not removed: filesystem error"
    assert result.removed == ()
    assert result.skipped == ("npm-keep",)
    assert result.warnings == (warning,)
    assert result.metadata()["warnings"] == (warning,)
    assert stale_skill.read_bytes() == stale_bytes
    assert user_skill.read_text() == "---\nname: user\n---\nuser owned\n"


def test_npm_skill_reconciliation_warns_per_failed_write_and_keeps_other_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"beta":"1.0.0"}}\n')
    _install_npm_fixture(owner, "beta")
    initial = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))
    assert initial.generated == ("npm-beta",)
    beta_skill = tmp_path / ".agents/skills/npm-beta/SKILL.md"
    beta_bytes = beta_skill.read_bytes()

    _install_npm_fixture(owner, "alpha")
    (owner / "package.json").write_text('{"dependencies":{"alpha":"1.0.0","beta":"2.0.0"}}\n')
    (owner / "node_modules/beta/package.json").write_text(
        '{"name":"beta","version":"2.0.0","description":"demo"}\n'
    )
    user_skill = tmp_path / ".agents/skills/user/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user\n---\nuser owned\n")
    original_atomic_write = skills_npm._atomic_write_text

    def fail_beta_write(path: Path, content: str) -> None:
        if path == beta_skill:
            raise OSError("simulated read-only skills workspace")
        original_atomic_write(path, content)

    monkeypatch.setattr(skills_npm, "_atomic_write_text", fail_beta_write)

    result = ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))

    warning = "optional npm skill 'npm-beta' not written: filesystem error"
    assert result.generated == ("npm-alpha",)
    assert result.warnings == (warning,)
    assert (tmp_path / ".agents/skills/npm-alpha/SKILL.md").is_file()
    assert beta_skill.read_bytes() == beta_bytes
    assert user_skill.read_text() == "---\nname: user\n---\nuser owned\n"


def test_npm_skill_reconciliation_does_not_mask_programming_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path / "app"
    owner.mkdir()
    (owner / "package.json").write_text('{"dependencies":{"demo":"1.0.0"}}\n')
    _install_npm_fixture(owner, "demo")

    def fail_with_programming_error(_path: Path, _content: str) -> None:
        raise RuntimeError("bug in skill rendering")

    monkeypatch.setattr(skills_npm, "_atomic_write_text", fail_with_programming_error)

    with pytest.raises(RuntimeError, match="bug in skill rendering"):
        ensure_npm_skills(project_root=tmp_path, package_owners=(owner,))


def test_contract_cache_fingerprint_changes_with_project_skill_bytes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    skill = tmp_path / ".agents/skills/npm-demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: npm-demo\n---\nfirst\n")
    first = _contract_generation_fingerprint(tmp_path, request, "runner")
    skill.write_text("---\nname: npm-demo\n---\nsecond\n")
    second = _contract_generation_fingerprint(tmp_path, request, "runner")
    assert first != second


def test_contract_cache_fingerprint_changes_with_rendered_property_bytes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    first = _contract_generation_fingerprint(tmp_path, request, "runner")
    changed = replace(
        request,
        cache_payload={**request.cache_payload, "propertyBlock": "// renderer changed\n"},
    )
    second = _contract_generation_fingerprint(tmp_path, changed, "runner")

    assert first != second
