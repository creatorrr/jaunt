from __future__ import annotations

from collections.abc import Mapping

from jaunt.targets.base import TargetWorkspace
from jaunt.typescript.cli_bridge import specs_payload
from jaunt.typescript.status import _spec_dependency_graph


def test_typescript_specs_dependency_graph_keeps_symbol_edges() -> None:
    core = "ts:packages/core/src/normalize/index#normalizeSpacing"
    base = "ts:packages/core/src/store/index#BaseStore"
    app = "ts:packages/app/src/slug/index#slugify"
    store = "ts:packages/app/src/store/index#TokenStore"
    modules: list[Mapping[str, object]] = [
        {
            "symbols": [
                {"id": app, "options": {"deps": [core]}},
                {
                    "id": store,
                    "options": {"deps": [core]},
                    "heritage": {"resolvedBaseId": base},
                },
            ]
        },
        {"symbols": [{"id": core, "options": {}}, {"id": base, "options": {}}]},
    ]

    assert _spec_dependency_graph(modules) == {
        app: [core],
        store: [core, base],
        core: [],
        base: [],
    }


def test_typescript_specs_payload_repeats_graph_under_target() -> None:
    graph = {"ts:src/app#index": ["ts:src/core#normalize"]}
    payload = specs_payload(TargetWorkspace(language="ts", metadata={"dependency_graph": graph}))

    assert payload["dependency_graph"] == graph
    assert payload["targets"]["ts"]["dependency_graph"] == graph
