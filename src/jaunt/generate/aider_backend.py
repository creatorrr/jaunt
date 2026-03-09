from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from jaunt.agent_runtime import AgentFile, AgentTask, AgentTaskExecutionError
from jaunt.aider_executor import AiderExecutor
from jaunt.config import AiderConfig, LLMConfig, PromptsConfig
from jaunt.generate.aider_contract import (
    aider_contract_addendum,
    aider_generation_fingerprint_parts,
    aider_retry_strategy_addendum,
    aider_runtime_policy,
)
from jaunt.generate.base import GenerationResult, GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.generate.fingerprint import build_generation_fingerprint
from jaunt.generate.shared import async_test_info, fmt_kv_block, load_prompt, render_template
from jaunt.validation import validate_generated_source


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
        "",
        aider_runtime_policy().rstrip(),
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


def _is_typecheck_error(error: str) -> bool:
    return error.startswith("ty check failed for ")


def _is_syntax_error(error: str) -> bool:
    return error.startswith("SyntaxError:")


def _is_narrow_contract_error(error: str) -> bool:
    return error.startswith("Generated source must not redefine handwritten") or (
        "public_api_only tests must not" in error
    )


def _classify_validation_failure(errors: list[str]) -> str:
    if errors and all(_is_typecheck_error(error) for error in errors):
        return "typecheck"
    if errors and all(_is_syntax_error(error) for error in errors):
        return "syntax"
    if errors and all(
        _is_typecheck_error(error) or _is_narrow_contract_error(error) for error in errors
    ):
        return "narrow_contract"
    return "contract"


def _is_edit_application_failure(error_text: str) -> bool:
    return "SEARCH/REPLACE" in error_text and (
        "failed to match" in error_text or "SearchReplaceNoExactMatch" in error_text
    )


def _compact_lines(text: str, *, max_lines: int = 20) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return lines
    head = lines[: max_lines // 2]
    tail = lines[-(max_lines // 2) :]
    return [*head, "... context truncated ...", *tail]


def _retry_context_lines(*, failure_kind: str, details: list[str]) -> list[str]:
    if failure_kind == "typecheck":
        header = (
            "Retry focus: fix only the reported type-check issue(s) with the smallest "
            "possible source change. Do not redesign unrelated code."
        )
    elif failure_kind == "narrow_contract":
        header = (
            "Retry focus: preserve correct code and make the smallest contract/public-API "
            "fix needed to satisfy the reported error(s)."
        )
    elif failure_kind == "edit_apply":
        header = (
            "Retry focus: a previous diff/search-replace edit failed to apply. Reuse the "
            "current target content and rewrite the file directly instead of repeating the "
            "same exact diff blocks."
        )
    elif failure_kind == "syntax":
        header = "Retry focus: repair the syntax errors while preserving the intended design."
    else:
        header = (
            "Retry focus: satisfy the reported validation errors while preserving correct code."
        )
    return [header, *details]


@dataclass(frozen=True, slots=True)
class _AttemptPlan:
    mode: Literal["architect", "code"]
    edit_format: str
    editor_edit_format: str | None
    target_content: str
    retry_strategy: str | None
    main_reasoning_effort: str | None = None
    editor_reasoning_effort: str | None = None


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

    def _templates_for_ctx(self, ctx: ModuleSpecContext) -> tuple[str, str, str]:
        if ctx.kind == "build":
            return self._aider.build_mode, self._build_system, self._build_module
        return self._aider.test_mode, self._test_system, self._test_module

    def _plan_attempt(
        self,
        *,
        ctx: ModuleSpecContext,
        previous_source: str,
        failure_kind: str | None,
    ) -> _AttemptPlan:
        configured_mode, _system_template, _user_template = self._templates_for_ctx(ctx)
        if failure_kind is None:
            if configured_mode == "architect":
                return _AttemptPlan(
                    mode="architect",
                    edit_format="architect",
                    editor_edit_format="editor-diff",
                    target_content="",
                    retry_strategy=None,
                    editor_reasoning_effort="low",
                )
            return _AttemptPlan(
                mode="code",
                edit_format="diff",
                editor_edit_format=None,
                target_content="",
                retry_strategy=None,
            )

        if failure_kind == "edit_apply" and configured_mode == "architect":
            return _AttemptPlan(
                mode="architect",
                edit_format="architect",
                editor_edit_format="editor-whole",
                target_content=previous_source,
                retry_strategy="edit_apply",
                editor_reasoning_effort="low",
            )

        if failure_kind in {"typecheck", "narrow_contract"}:
            return _AttemptPlan(
                mode="code",
                edit_format="whole",
                editor_edit_format=None,
                target_content=previous_source,
                retry_strategy="minimal_repair",
            )

        if configured_mode == "architect":
            return _AttemptPlan(
                mode="architect",
                edit_format="architect",
                editor_edit_format="editor-diff",
                target_content=previous_source,
                retry_strategy="structural_repair",
                editor_reasoning_effort="low",
            )

        return _AttemptPlan(
            mode="code",
            edit_format="whole",
            editor_edit_format=None,
            target_content=previous_source,
            retry_strategy="structural_repair",
        )

    def _usage_totals(self, prompt_tokens: int, completion_tokens: int) -> TokenUsage | None:
        if prompt_tokens == 0 and completion_tokens == 0:
            return None
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self.model_name,
            provider=self.provider_name,
        )

    async def _run_attempt(
        self,
        ctx: ModuleSpecContext,
        *,
        attempt_plan: _AttemptPlan,
        extra_error_context: list[str] | None,
    ) -> tuple[str, TokenUsage | None]:
        _mode, system_template, user_template = self._templates_for_ctx(ctx)
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
        retry_addendum = aider_retry_strategy_addendum(ctx.kind, attempt_plan.retry_strategy)
        if retry_addendum:
            read_only_files.append(
                AgentFile(relative_path="context/retry_strategy.md", content=retry_addendum)
            )
        if skills_block:
            read_only_files.append(
                AgentFile(
                    relative_path="context/external_skills.md",
                    content=skills_block + "\n",
                )
            )

        instruction_lines = [
            "Edit only the target Python file.",
            "Read and follow `context/contract.md` first.",
        ]
        if retry_addendum:
            instruction_lines.append(
                "Read and follow `context/retry_strategy.md` before making changes."
            )
        instruction_lines.extend(
            [
                "Use the context files as read-only references.",
                "Do not edit files under `context/`.",
                "Return the completed Python source in the target file.",
            ]
        )
        task = AgentTask(
            kind="build_module" if ctx.kind == "build" else "test_module",
            mode=attempt_plan.mode,
            instruction="\n".join(instruction_lines) + "\n",
            target_file=AgentFile(
                relative_path=_module_path(ctx.generated_module),
                content=attempt_plan.target_content,
            ),
            read_only_files=read_only_files,
            edit_format=attempt_plan.edit_format,
            editor_edit_format=attempt_plan.editor_edit_format,
            main_reasoning_effort=attempt_plan.main_reasoning_effort,
            editor_reasoning_effort=attempt_plan.editor_reasoning_effort,
        )
        result = await self._executor.run_task(task)
        return result.output, result.usage

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        attempt_plan = self._plan_attempt(ctx=ctx, previous_source="", failure_kind=None)
        return await self._run_attempt(
            ctx,
            attempt_plan=attempt_plan,
            extra_error_context=extra_error_context,
        )

    async def generate_with_retry(
        self,
        ctx: ModuleSpecContext,
        *,
        max_attempts: int = 2,
        extra_validator: Callable[[str], list[str]] | None = None,
        initial_error_context: list[str] | None = None,
    ) -> GenerationResult:
        attempts = 0
        last_source = ""
        last_errors: list[str] = []
        failure_kind: str | None = None
        base_error_context = list(initial_error_context or [])
        retry_error_context: list[str] = []
        total_prompt = 0
        total_completion = 0

        while attempts < max_attempts:
            attempt_plan = self._plan_attempt(
                ctx=ctx,
                previous_source=last_source,
                failure_kind=failure_kind,
            )
            attempts += 1
            try:
                source, usage = await self._run_attempt(
                    ctx,
                    attempt_plan=attempt_plan,
                    extra_error_context=(base_error_context + retry_error_context) or None,
                )
                last_source = source
                if usage is not None:
                    total_prompt += usage.prompt_tokens
                    total_completion += usage.completion_tokens
            except AgentTaskExecutionError as e:
                last_source = e.output or last_source
                if e.usage is not None:
                    total_prompt += e.usage.prompt_tokens
                    total_completion += e.usage.completion_tokens
                failure_kind = "edit_apply" if _is_edit_application_failure(str(e)) else "contract"
                last_errors = _compact_lines(str(e))
                if attempts >= max_attempts:
                    break
                retry_error_context = _retry_context_lines(
                    failure_kind=failure_kind,
                    details=last_errors,
                )
                continue

            last_errors = validate_generated_source(last_source, ctx.expected_names)
            if not last_errors and extra_validator is not None:
                last_errors = extra_validator(last_source)
            if not last_errors:
                return GenerationResult(
                    attempts=attempts,
                    source=last_source,
                    errors=[],
                    usage=self._usage_totals(total_prompt, total_completion),
                )

            failure_kind = _classify_validation_failure(last_errors)
            if attempts >= max_attempts:
                break
            retry_error_context = _retry_context_lines(
                failure_kind=failure_kind,
                details=last_errors,
            )

        return GenerationResult(
            attempts=attempts,
            source=last_source or None,
            errors=last_errors,
            usage=self._usage_totals(total_prompt, total_completion),
        )


# Backward-compatible alias used by some tests and earlier patches.
AiderBackend = AiderGeneratorBackend
