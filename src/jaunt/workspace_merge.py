"""Deterministic migration from descendant Jaunt projects to one workspace."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jaunt.config import JauntConfig, PathsConfig, load_config
from jaunt.errors import JauntConfigError
from jaunt.paths import generated_module_to_relpath, spec_module_to_generated_module
from jaunt.workspace import ModuleRoute, ResolvedWorkspace, resolve_workspace


@dataclass(frozen=True, slots=True)
class MergePlan:
    neutral: bool
    configs: tuple[Path, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    actions: tuple[dict[str, str], ...]
    module_routes: tuple[dict[str, str], ...]
    conflicts: tuple[str, ...]

    def to_json(self, root: Path) -> dict[str, object]:
        return {
            "neutral": self.neutral,
            "configs": [_relative(path, root) for path in self.configs],
            "proposed_roots": {
                "source_roots": list(self.source_roots),
                "test_roots": list(self.test_roots),
            },
            "actions": list(self.actions),
            "module_routes": list(self.module_routes),
            "conflicts": list(self.conflicts),
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _tracked_configs(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--", "*/jaunt.toml", "**/jaunt.toml"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        candidates = [root / line for line in proc.stdout.splitlines() if line.strip()]
    else:
        candidates = list(root.rglob("jaunt.toml"))
    root_config = (root / "jaunt.toml").resolve()
    return sorted(
        {path.resolve() for path in candidates if path.resolve() != root_config},
        key=lambda path: path.as_posix(),
    )


def _prompt_content(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise JauntConfigError(f"Could not read prompt override {path}: {exc}") from exc
    return hashlib.sha256(content).hexdigest()


def _policy(cfg: JauntConfig) -> dict[str, object]:
    value = dataclasses.asdict(cfg)
    value.pop("paths", None)
    prompts = value.get("prompts")
    if isinstance(prompts, dict):
        value["prompts"] = {
            key: _prompt_content(raw) if isinstance(raw, str) else raw
            for key, raw in prompts.items()
        }
    return value


def _explicit_roots(
    root: Path, workspaces: list[ResolvedWorkspace], *, tests: bool
) -> tuple[str, ...]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for workspace in workspaces:
        candidates = (
            [route.root for route in workspace.test_roots]
            if tests
            else list(workspace.source_roots)
        )
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            try:
                resolved.relative_to(root.resolve())
            except ValueError as exc:
                raise JauntConfigError(
                    f"Cannot merge source/test root outside the workspace: {resolved}"
                ) from exc
            paths.append(resolved)
            seen.add(resolved)
    return tuple(_relative(path, root) or "." for path in paths)


def _artifact_path(route, generated_dir: str) -> Path:
    generated_module = spec_module_to_generated_module(route.module, generated_dir=generated_dir)
    return route.output_base / generated_module_to_relpath(
        generated_module, generated_dir=generated_dir
    )


def _freshness_conflicts(
    config_root: Path, cfg: JauntConfig, workspace: ResolvedWorkspace
) -> list[str]:
    if not workspace.modules:
        return []
    from jaunt.status_core import compute_magic_status

    status = compute_magic_status(
        root=config_root,
        cfg=cfg,
        source_dirs=list(workspace.source_roots),
        build_instructions=list(cfg.build.instructions),
        include_target_tests=bool(cfg.build.include_target_tests),
        infer_deps=bool(cfg.build.infer_deps),
    )
    return [
        f"{_relative(config_root / 'jaunt.toml', config_root)}: stale module {module}"
        for module in sorted(status.stale)
    ]


def plan_merge(root: Path) -> MergePlan:
    root = root.resolve()
    root_config = root / "jaunt.toml"
    root_cfg = load_config(root=root)
    configs = [root_config, *_tracked_configs(root)]

    loaded: list[tuple[Path, JauntConfig, ResolvedWorkspace]] = []
    conflicts: list[str] = []
    baseline = _policy(root_cfg)
    generated_dir = root_cfg.paths.generated_dir
    for config in configs:
        cfg_root = config.parent
        cfg = load_config(root=cfg_root, config_path=config)
        workspace = resolve_workspace(cfg_root, cfg)
        if config != root_config and not workspace.modules:
            continue
        if cfg.paths.generated_dir != generated_dir:
            conflicts.append(
                f"{_relative(config, root)} uses generated_dir={cfg.paths.generated_dir!r}; "
                f"expected {generated_dir!r}"
            )
        if _policy(cfg) != baseline:
            conflicts.append(f"{_relative(config, root)} has conflicting non-path policy")
        try:
            conflicts.extend(_freshness_conflicts(cfg_root, cfg, workspace))
        except Exception as exc:  # stale/corrupt discovery is never merge-neutral
            conflicts.append(f"{_relative(config, root)} could not prove freshness: {exc}")
        loaded.append((config, cfg, workspace))

    workspaces = [item[2] for item in loaded]
    source_roots = _explicit_roots(root, workspaces, tests=False)
    test_roots = _explicit_roots(root, workspaces, tests=True)
    prospective_cfg = dataclasses.replace(
        root_cfg,
        paths=PathsConfig(
            source_roots=list(source_roots),
            test_roots=list(test_roots),
            generated_dir=generated_dir,
        ),
    )
    try:
        prospective = resolve_workspace(root, prospective_cfg)
    except JauntConfigError as exc:
        conflicts.append(str(exc))
        prospective = ResolvedWorkspace(
            root=root,
            source_roots=(),
            test_roots=(),
            modules=(),
        )

    old_routes: dict[Path, ModuleRoute] = {}
    old_modules: dict[str, Path] = {}
    for _config, _cfg, workspace in loaded:
        for route in workspace.modules:
            source = route.source_file.resolve()
            previous = old_routes.get(source)
            if previous is not None and (
                previous.module != route.module
                or previous.import_root != route.import_root
                or previous.output_base != route.output_base
            ):
                conflicts.append(
                    f"{_relative(source, root)} has conflicting routes across child configs"
                )
            previous_source = old_modules.get(route.module)
            if previous_source is not None and previous_source != source:
                conflicts.append(
                    f"duplicate module {route.module!r} across {_relative(previous_source, root)} "
                    f"and {_relative(source, root)}"
                )
            old_routes[source] = route
            old_modules[route.module] = source
    new_routes: dict[Path, ModuleRoute] = {
        route.source_file.resolve(): route for route in prospective.modules
    }
    if set(old_routes) != set(new_routes):
        missing = sorted(set(old_routes) - set(new_routes))
        added = sorted(set(new_routes) - set(old_routes))
        if missing:
            conflicts.append(
                "prospective config loses modules: " + ", ".join(str(path) for path in missing)
            )
        if added:
            conflicts.append(
                "prospective config newly governs modules: "
                + ", ".join(str(path) for path in added)
            )

    route_rows: list[dict[str, str]] = []
    for source_file in sorted(set(old_routes) & set(new_routes)):
        old = old_routes[source_file]
        new = new_routes[source_file]
        old_artifact = _artifact_path(old, generated_dir)
        new_artifact = _artifact_path(new, generated_dir)
        neutral = (
            old.module == new.module
            and old.import_root == new.import_root
            and old.owner_dir == new.owner_dir
            and old.test_roots == new.test_roots
            and old_artifact == new_artifact
        )
        if not neutral:
            conflicts.append(
                f"route change for {source_file}: {old.module}@{old.import_root} -> "
                f"{new.module}@{new.import_root}"
            )
        route_rows.append(
            {
                "module": old.module,
                "source": _relative(source_file, root),
                "import_root": _relative(old.import_root, root),
                "owner": _relative(old.owner_dir, root),
                "artifact": _relative(old_artifact, root),
                "neutral": str(neutral).lower(),
            }
        )

    participating = tuple(item[0] for item in loaded)
    actions: list[dict[str, str]] = [{"action": "update-paths", "path": "jaunt.toml"}]
    actions.extend(
        {"action": "delete-config", "path": _relative(config, root)}
        for config in participating
        if config != root_config
    )
    return MergePlan(
        neutral=not conflicts,
        configs=participating,
        source_roots=source_roots,
        test_roots=test_roots,
        actions=tuple(actions),
        module_routes=tuple(route_rows),
        conflicts=tuple(conflicts),
    )


def _toml_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def rewrite_paths(text: str, plan: MergePlan, generated_dir: str) -> str:
    replacement = (
        "[paths]\n"
        f"source_roots = {_toml_list(plan.source_roots)}\n"
        f"test_roots = {_toml_list(plan.test_roots)}\n"
        f"generated_dir = {json.dumps(generated_dir)}\n"
    )
    match = re.search(r"(?ms)^\[paths\]\s*\n.*?(?=^\[[^\n]+\]\s*$|\Z)", text)
    if match:
        suffix = "\n" if match.end() < len(text) and not replacement.endswith("\n\n") else ""
        return text[: match.start()] + replacement + suffix + text[match.end() :]
    version = re.search(r"(?m)^version\s*=.*$", text)
    if version:
        at = version.end()
        return text[:at] + "\n\n" + replacement + text[at:]
    return replacement + "\n" + text


def apply_merge(root: Path, plan: MergePlan) -> tuple[bool, str]:
    if not plan.neutral:
        return False, "merge is not neutral"
    root = root.resolve()
    config = root / "jaunt.toml"
    original_root = config.read_bytes()
    deleted = {
        path: path.read_bytes() for path in plan.configs if path.resolve() != config.resolve()
    }
    cfg = load_config(root=root)
    try:
        config.write_text(
            rewrite_paths(original_root.decode("utf-8"), plan, cfg.paths.generated_dir),
            encoding="utf-8",
        )
        for path in deleted:
            path.unlink()
        proc = subprocess.run(
            [sys.executable, "-m", "jaunt", "check", "--root", str(root)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "post-merge check failed").strip()
            raise JauntConfigError(detail)
    except Exception as exc:
        config.write_bytes(original_root)
        for path, content in deleted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return False, str(exc)
    return True, ""
