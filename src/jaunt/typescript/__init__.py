"""TypeScript target integration.

The Python package owns orchestration and process lifecycle. TypeScript parsing,
checking, and artifact composition live in the project-local ``@usejaunt/ts``
worker reached through :mod:`jaunt.typescript.worker`.
"""

from jaunt.typescript.config import TypeScriptPromptsConfig, TypeScriptTargetConfig

__all__ = ["TypeScriptPromptsConfig", "TypeScriptTargetConfig"]
