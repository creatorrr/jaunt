"""Codex MCP-backed generation backend."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jaunt.config import CodexConfig, LLMConfig, PromptsConfig
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage


class CodexBackend(GeneratorBackend):
    def __init__(
        self,
        codex: CodexConfig,
        llm: LLMConfig,
        prompts: PromptsConfig | None = None,
        *,
        pool_size: int = 1,
    ) -> None:
        self._codex = codex
        self._llm = llm
        self._prompts = prompts
        self._model = codex.model or llm.model
        self._pool_size = max(1, pool_size)
        self._pool: asyncio.Queue[ClientSession] | None = None
        self._stack: AsyncExitStack | None = None
        self._started = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def provider_name(self) -> str:
        return "codex"

    @property
    def supports_structured_output(self) -> bool:
        return False

    async def _ensure_pool(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("CodexBackend is closed.")
            if self._pool is None:
                self._pool = asyncio.Queue()
                self._stack = AsyncExitStack()

    async def _spawn_slot(self) -> ClientSession:
        if self._stack is None:
            raise RuntimeError("CodexBackend pool was not initialized.")
        params = StdioServerParameters(command="codex", args=["mcp-server"])
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _checkout(self) -> ClientSession:
        await self._ensure_pool()
        pool = self._pool
        if pool is None:
            raise RuntimeError("CodexBackend pool was not initialized.")

        spawn_now = False
        async with self._lock:
            if pool.empty() and self._started < self._pool_size:
                self._started += 1
                spawn_now = True

        if spawn_now:
            try:
                return await self._spawn_slot()
            except Exception:
                async with self._lock:
                    self._started -= 1
                raise

        return await pool.get()

    def _return(self, session: ClientSession) -> None:
        if self._pool is not None and not self._closed:
            self._pool.put_nowait(session)

    async def aclose(self) -> None:
        self._closed = True
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._pool = None
        self._started = 0

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, TokenUsage | None]:
        session = await self._checkout()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / (ctx.generated_module.replace(".", "/") + ".py")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(getattr(ctx, "seed_target_content", "") or "", encoding="utf-8")

                ctx_dir = root / "_context"
                ctx_dir.mkdir()
                for i, (ref, src) in enumerate(
                    sorted(ctx.spec_sources.items(), key=lambda kv: str(kv[0]))
                ):
                    (ctx_dir / f"spec_{i}.py").write_text(f"# {ref}\n{src}", encoding="utf-8")
                for i, (ref, api) in enumerate(
                    sorted(ctx.dependency_apis.items(), key=lambda kv: str(kv[0]))
                ):
                    (ctx_dir / f"dep_{i}.pyi").write_text(f"# {ref}\n{api}", encoding="utf-8")

                contract_block = getattr(ctx, "whole_class_contract_block", "") or ""
                if contract_block.strip():
                    (ctx_dir / "whole_class_contract.md").write_text(
                        contract_block.rstrip() + "\n", encoding="utf-8"
                    )

                prompt = self._build_prompt(ctx, target.relative_to(root), extra_error_context)
                res = await session.call_tool(
                    "codex",
                    {
                        "prompt": prompt,
                        "cwd": str(root),
                        "sandbox": self._codex.sandbox,
                        "approval-policy": "never",
                        "model": self._model,
                        "config": {
                            "model_reasoning_effort": self._codex.reasoning_effort,
                            **(self._codex.config or {}),
                        },
                    },
                )
                source = target.read_text(encoding="utf-8")
                usage = self._extract_usage(res)
                return source, usage
        finally:
            self._return(session)

    def _build_prompt(
        self,
        ctx: ModuleSpecContext,
        target_rel: Path,
        extra_error_context: list[str] | None,
    ) -> str:
        blocks = [
            f"Write a complete Python module to `{target_rel}` that exports: "
            f"{', '.join(ctx.expected_names)}.",
            "The spec stubs and their docstrings in `_context/spec_*.py` are the "
            "behavioral contract. Read `_context/dep_*.pyi` for available APIs.",
        ]
        if (getattr(ctx, "whole_class_contract_block", "") or "").strip():
            blocks.append(
                "Read `_context/whole_class_contract.md`: implement every "
                "`# jaunt:implement` method, keep preserved methods verbatim, and design "
                "the public API the docstring implies."
            )
        blocks += [
            getattr(ctx, "build_instructions_block", "") or "",
            getattr(ctx, "module_contract_block", "") or "",
            getattr(ctx, "base_contract_block", "") or "",
            getattr(ctx, "package_context_block", "") or "",
            getattr(ctx, "skills_block", "") or "",
        ]
        blocks.append(
            "Edit ONLY the target file. Do not create other files, run tests, or modify "
            "anything else. Output the full module - no placeholders."
        )
        if extra_error_context:
            blocks.append("Previous attempt problems:\n" + "\n".join(extra_error_context))
        return "\n\n".join(b for b in blocks if b)

    async def complete_text(self, *, system: str, user: str) -> str:
        session = await self._checkout()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                prompt = "\n\n".join(
                    [
                        system.strip(),
                        user.strip(),
                        "Return ONLY the requested text. Do not run any commands or edit "
                        "any files.",
                    ]
                )
                res = await session.call_tool(
                    "codex",
                    {
                        "prompt": prompt,
                        "cwd": str(tmp),
                        "sandbox": "read-only",
                        "approval-policy": "never",
                        "model": self._model,
                        "config": {"model_reasoning_effort": self._codex.reasoning_effort},
                    },
                )
                return self._final_message(res)
        finally:
            self._return(session)

    def _final_message(self, res: Any) -> str:
        sc = getattr(res, "structuredContent", None) or {}
        for key in ("lastAgentMessage", "agent_message", "message", "result"):
            val = sc.get(key) if isinstance(sc, dict) else None
            if isinstance(val, str) and val:
                return val
        parts = []
        for item in getattr(res, "content", None) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    def _extract_usage(self, res: Any) -> TokenUsage | None:
        sc = getattr(res, "structuredContent", None)
        if not isinstance(sc, dict):
            return None
        usage = sc.get("usage")
        if not isinstance(usage, dict):
            return None
        pin = usage.get("input_tokens", usage.get("prompt_tokens"))
        pout = usage.get("output_tokens", usage.get("completion_tokens"))
        if isinstance(pin, int) and isinstance(pout, int):
            return TokenUsage(
                prompt_tokens=pin,
                completion_tokens=pout,
                model=self._model,
                provider="codex",
            )
        return None
