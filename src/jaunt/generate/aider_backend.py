from __future__ import annotations

from pathlib import Path
from typing import Literal

from jaunt.agent_runtime import AgentFile, AgentTask
from jaunt.aider_executor import AiderExecutor
from jaunt.config import AiderConfig, LLMConfig, PromptsConfig
from jaunt.generate.aider_contract import (
    aider_contract_addendum,
    aider_generation_fingerprint_parts,
)
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.generate.fingerprint import build_generation_fingerprint
from jaunt.generate.shared import async_test_info, fmt_kv_block, load_prompt, render_template


def _module_path(module_name: str) -> str:
    return str(Path(*module_name.split("."))).replace("\\", "/") + ".py"


def _build_contract(
    *,
    kind: Literal["build", "test"],
    system: str,
    user: str,
) -> str:
    sections = [
        "# Contract",
        "Edit the target Python module in place.",
        "## System",
        system,
        "## Task",
        user,
    ]
    addendum = aider_contract_addendum(kind)
    if addendum:
        sections.extend(["", addendum.rstrip()])
    return "\n\n".join(sections).strip() + "\n"


def _render_prompt_sections(
    *,
    ctx: ModuleSpecContext,
    system_template: str,
    user_template: str,
    extra_error_context: list[str] | None,
) -> tuple[str, str, str, str, str]:
    expected = ", ".join(ctx.expected_names)

    spec_items: list[tuple[str, str]] = []
    for ref, source in sorted(ctx.spec_sources.items(), key=lambda kv: str(kv[0])):
        label = str(ref)
        prompt = ctx.decorator_prompts.get(ref)
        if prompt:
            source = f"{source.rstrip()}\n\n# Decorator prompt\n{prompt.rstrip()}\n"
        spec_items.append((label, source))

    deps_api_items = [
        (str(ref), api)
        for ref, api in sorted(ctx.dependency_apis.items(), key=lambda kv: str(kv[0]))
    ]
    deps_generated_items = sorted(ctx.dependency_generated_modules.items(), key=lambda kv: kv[0])
    decorator_api_items = [
        (str(ref), api)
        for ref, api in sorted(ctx.decorator_apis.items(), key=lambda kv: str(kv[0]))
    ]

    err_items: list[tuple[str, str]] = []
    if extra_error_context:
        for idx, line in enumerate(extra_error_context, start=1):
            err_items.append((f"error_context[{idx}]", line))

    mapping = {
        "spec_module": ctx.spec_module,
        "generated_module": ctx.generated_module,
        "expected_names": expected,
        "specs_block": fmt_kv_block(spec_items),
        "deps_api_block": fmt_kv_block(deps_api_items),
        "deps_generated_block": fmt_kv_block(deps_generated_items),
        "decorator_apis_block": fmt_kv_block(decorator_api_items),
        "module_contract_block": ctx.module_contract_block or "(none)\n",
        "error_context_block": fmt_kv_block(err_items),
        "async_test_info": async_test_info(ctx.async_runner),
    }

    system = render_template(system_template, mapping).strip()
    user = render_template(user_template, mapping).strip()
    deps_generated = fmt_kv_block(deps_generated_items)
    error_context = fmt_kv_block(err_items)
    return system, user, deps_generated, error_context, (ctx.skills_block or "").strip()


class AiderGeneratorBackend(GeneratorBackend):
    def __init__(
        self,
        llm: LLMConfig,
        aider: AiderConfig,
        prompts: PromptsConfig | None = None,
    ) -> None:
        self._llm = llm
        self._aider = aider
        self._model = llm.model
        self._executor = AiderExecutor(llm, aider)
        self._build_system = load_prompt(
            "build_system.md",
            prompts.build_system if prompts else None,
        )
        self._build_module = load_prompt(
            "build_module.md",
            prompts.build_module if prompts else None,
        )
        self._test_system = load_prompt(
            "test_system.md",
            prompts.test_system if prompts else None,
        )
        self._test_module = load_prompt(
            "test_module.md",
            prompts.test_module if prompts else None,
        )

    @property
    def provider_name(self) -> str:
        return "aider"

    def generation_fingerprint(self, ctx: ModuleSpecContext) -> str:
        if ctx.kind == "build":
            prompt_parts = [self._build_system, self._build_module]
            mode = self._aider.build_mode
        else:
            prompt_parts = [self._test_system, self._test_module]
            mode = self._aider.test_mode
        return build_generation_fingerprint(
            engine="aider",
            kind=ctx.kind,
            mode=mode,
            prompt_parts=prompt_parts,
            editor_model=self._aider.editor_model,
            reasoning_effort=self._llm.reasoning_effort or "",
            runtime_parts=aider_generation_fingerprint_parts(ctx.kind),
        )

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        if ctx.kind == "build":
            mode = self._aider.build_mode
            system_template = self._build_system
            user_template = self._build_module
        else:
            mode = self._aider.test_mode
            system_template = self._test_system
            user_template = self._test_module

        system, user, deps_generated, error_context, skills_block = _render_prompt_sections(
            ctx=ctx,
            system_template=system_template,
            user_template=user_template,
            extra_error_context=extra_error_context,
        )
        contract = _build_contract(kind=ctx.kind, system=system, user=user)

        read_only_files = [
            AgentFile(relative_path="context/contract.md", content=contract),
            AgentFile(
                relative_path="context/dependency_generated_modules.md",
                content=deps_generated,
            ),
            AgentFile(relative_path="context/error_context.md", content=error_context),
        ]
        if skills_block:
            read_only_files.append(
                AgentFile(
                    relative_path="context/external_skills.md",
                    content=skills_block + "\n",
                )
            )

        task = AgentTask(
            kind="build_module" if ctx.kind == "build" else "test_module",
            mode=mode,  # type: ignore[arg-type]
            instruction=(
                "Edit only the target Python file.\n"
                "Read and follow `context/contract.md` first.\n"
                "Use the context files as read-only references.\n"
                "Do not edit files under `context/`.\n"
                "Return the completed Python source in the target file.\n"
            ),
            target_file=AgentFile(relative_path=_module_path(ctx.generated_module), content=""),
            read_only_files=read_only_files,
        )
        result = await self._executor.run_task(task)
        return result.output, result.usage


# Backward-compatible alias used by some tests and earlier patches.
AiderBackend = AiderGeneratorBackend
