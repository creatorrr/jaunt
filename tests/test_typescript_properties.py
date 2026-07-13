from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from jaunt.config import JauntConfig, load_config
from jaunt.errors import JauntConfigError
from jaunt.generate.base import GenerationRequest, GeneratorBackend, ModuleSpecContext
from jaunt.typescript.contracts import _battery_path, _battery_request, _generate_battery
from jaunt.typescript.properties import (
    attach_property_block,
    parse_property_cases,
    render_property_block,
)


def _config(root: Path) -> JauntConfig:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "fast-check": "^4.0.0",
                    "vitest": "^4.0.0",
                }
            }
        )
        + "\n"
    )
    (root / "tsconfig.json").write_text("{}\n")
    (root / "tsconfig.test.json").write_text("{}\n")
    (root / "jaunt.toml").write_text(
        """version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]

[codex]
model = "gpt-5.6-sol"
"""
    )
    return load_config(root=root)


def test_round_trip_property_is_parsed_and_rendered_without_model_authorship() -> None:
    source = """/**
 * @prop given bytes: fc.uint8Array() :: decode(encode(bytes)) equals bytes
 */
"""
    cases = parse_property_cases(
        (source,),
        label="round-trip",
        public_symbols=("decode", "encode"),
    )

    assert len(cases) == 1
    case = cases[0]
    assert case.expected_type == "Uint8Array"
    assert case.arbitrary == "fc.uint8Array()"
    assert case.case_id.startswith("prop-")
    block = render_property_block(
        cases,
        symbol_specifiers={"decode": "../codec.js", "encode": "../codec.js"},
        num_runs=50,
    )
    assert "const __jauntPropertyArbitrary_" in block
    assert "fc.Arbitrary<Uint8Array> = fc.uint8Array();" in block
    assert "fc.property(" in block
    assert f"@prop {case.case_id}:" in block
    assert f"{{ seed: {case.seed}, numRuns: 50 }}" in block
    assert (
        "__jauntPropertyExpect("
        "__jauntPropertyTarget0.decode(__jauntPropertyTarget0.encode(bytes))"
        ").toEqual(bytes);"
    ) in block
    attached = attach_property_block(
        'import { test } from "vitest";\ntest("example", () => {});\n', block
    )
    assert attached.index('from "fast-check"') < attached.index('test("example"')


def test_property_case_identity_ignores_nonsemantic_whitespace() -> None:
    compact = parse_property_cases(
        ("/** @prop given value:string::normalize(value) equals value */",),
        label="compact",
        public_symbols=("normalize",),
    )[0]
    spaced = parse_property_cases(
        ("/** @prop given value: string :: normalize( value ) equals value */",),
        label="spaced",
        public_symbols=("normalize",),
    )[0]

    assert compact.case_id == spaced.case_id
    assert compact.seed == spaced.seed


def test_negative_property_uses_a_deterministic_not_equal_predicate() -> None:
    cases = parse_property_cases(
        ('/** @prop given value: string :: normalize(value) does not equal "" */',),
        label="negative",
        public_symbols=("normalize",),
    )
    block = render_property_block(
        cases,
        symbol_specifiers={"normalize": "../normalize.js"},
        num_runs=20,
    )

    assert '.normalize(value)).not.toEqual("");' in block
    assert f"seed: {cases[0].seed}, numRuns: 20" in block


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("every input round-trips", "malformed"),
        ("given value: any :: normalize(value) equals value", "must not contain `any`"),
        (
            "given value: fc.anything() :: normalize(value) equals value",
            "unsupported fast-check arbitrary",
        ),
        (
            "given value: fc.string(); process.exit() :: normalize(value) equals value",
            "unsupported strategy text",
        ),
        (
            'given value: fc.record({"__proto__": fc.string()}) :: normalize(value) equals value',
            "invalid strategy option key",
        ),
        (
            "given value: string :: normalize(value) is stable",
            "exactly one top-level",
        ),
        (
            'given value: string :: Object.constructor("return process")() equals value',
            "invalid property name 'constructor'",
        ),
        (
            "given value: string :: "
            'Object.getOwnPropertyDescriptor(Object,"constructor").value("return process")() '
            "equals value",
            "invalid property name 'getOwnPropertyDescriptor'",
        ),
        (
            'given value: string :: Object["con" + "structor"]() equals value',
            "computed properties require one numeric literal index",
        ),
        (
            "given value: string :: hidden(value) equals value",
            "unsupported identifier 'hidden'",
        ),
    ],
)
def test_malformed_and_unsupported_properties_fail_deterministically(
    body: str, message: str
) -> None:
    with pytest.raises(JauntConfigError, match=message):
        parse_property_cases(
            (f"/** @prop {body} */",),
            label="bad property",
            public_symbols=("normalize",),
        )


def test_composite_fast_check_strategies_are_canonical_and_inferred() -> None:
    cases = parse_property_cases(
        (
            "/** @prop given value: fc.integer({ min: -10, max: 10 }) "
            ":: normalize(value) equals value */",
            "/** @prop given pair: fc.tuple(fc.string(), fc.integer({max: 4})) "
            ":: pair[0] equals pair[0] */",
            "/** @prop given item: fc.record({ name: fc.string(), flags: fc.array(fc.boolean()) }) "
            ":: item.name equals item.name */",
        ),
        label="composite properties",
        public_symbols=("normalize",),
    )

    assert cases[0].expected_type == "number"
    assert cases[0].arbitrary == "fc.integer({min:-10,max:10})"
    assert cases[1].expected_type == "inferred"
    assert cases[1].arbitrary == "fc.tuple(fc.string(),fc.integer({max:4}))"
    assert cases[2].expected_type == "inferred"
    block = render_property_block(
        cases,
        symbol_specifiers={"normalize": "../normalize.js"},
        num_runs=12,
    )
    assert ": fc.Arbitrary<number> = fc.integer({min:-10,max:10});" in block
    assert " = fc.tuple(fc.string(),fc.integer({max:4})) satisfies fc.Arbitrary<unknown>;" in block
    assert (
        " = fc.record({name:fc.string(),flags:fc.array(fc.boolean())}) "
        "satisfies fc.Arbitrary<unknown>;" in block
    )


def test_property_delimiters_named_equals_and_adjacent_operators_are_preserved() -> None:
    cases = parse_property_cases(
        (
            '/** @prop given value: fc.constant("::") :: value equals "::" */',
            "/** @prop given value: string :: equals(value) equals true */",
            '/** @prop given value: string :: value.equals("") equals false */',
            "/** @prop given value: number :: value - -1 equals value + +1 */",
        ),
        label="token-aware properties",
        public_symbols=("equals",),
    )

    assert cases[0].arbitrary == 'fc.constant("::")'
    assert cases[1].left.canonical == "equals(value)"
    assert cases[2].left.canonical == 'value.equals("")'
    assert cases[3].left.canonical == "value- -1"
    assert cases[3].right.canonical == "value+ +1"


def test_property_input_and_fixtures_shadow_same_named_public_symbols() -> None:
    cases = parse_property_cases(
        (
            '/** @prop given normalize: string :: normalize equals "" */',
            "/** @prop given value: string :: db(value) equals db(value) */",
        ),
        label="shadowing property",
        public_symbols=("db", "normalize"),
        fixture_names=("db",),
    )
    block = render_property_block(
        cases,
        symbol_specifiers={"db": "../db.js", "normalize": "../normalize.js"},
        fixture_specifier="../fixtures.js",
        fixture_names=("db",),
        num_runs=5,
    )

    assert "__jauntPropertyExpect(normalize).toEqual" in block
    assert "__jauntPropertyTarget" not in block
    assert "__jauntPropertyExpect(db(value)).toEqual(db(value));" in block


def test_async_properties_are_typed_and_support_async_fixtures() -> None:
    source = "/** @prop given value: string :: await load(value) equals value */"
    cases = parse_property_cases(
        (source,),
        label="async property",
        public_symbols=("load",),
        async_symbols=("load",),
    )
    block = render_property_block(
        cases,
        symbol_specifiers={"load": "../load.js"},
        num_runs=12,
    )
    assert "fc.asyncProperty(" in block
    assert "async () =>" in block
    assert "await fc.assert(" in block
    assert "async (value) =>" in block

    fixture_cases = parse_property_cases(
        ("/** @prop given value: string :: await load(db, value) equals value */",),
        label="fixture property",
        public_symbols=("load",),
        fixture_names=("db",),
        async_symbols=("load",),
    )
    fixture_block = render_property_block(
        fixture_cases,
        symbol_specifiers={"load": "../load.js"},
        num_runs=12,
        fixture_specifier="../fixtures.js",
        fixture_names=("db",),
    )
    assert 'from "../fixtures.js"' in fixture_block
    assert "async ({ db }) =>" in fixture_block
    assert "await __jauntPropertyTarget0.load(db,value)" in fixture_block


def test_contract_properties_are_scoped_to_the_adopted_symbol_before_generation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "src/codec.ts"
    source_text = """/**
 * Encode a value.
 * @prop given value: string :: decode(encode(value)) equals value
 * @jauntContract
 */
export function encode(value: string): string { return value; }

/** Decode one value. @jauntContract */
export function decode(value: string): string { return value; }
"""
    source.write_text(source_text)

    encode = _battery_request(
        tmp_path,
        config,
        source,
        "encode",
        _battery_path(tmp_path, config, source, "encode"),
        source_text,
        declaration_context="export function encode(value: string): string;\n",
    )
    decode = _battery_request(
        tmp_path,
        config,
        source,
        "decode",
        _battery_path(tmp_path, config, source, "decode"),
        source_text,
        declaration_context="export function decode(value: string): string;\n",
    )

    assert encode.cache_payload["propertyCount"] == 1
    assert decode.cache_payload["propertyCount"] == 0
    assert "fc.Arbitrary<string>" in str(encode.cache_payload["propertyBlock"])
    assert decode.cache_payload["propertyBlock"] == ""


class _NeverGenerator(GeneratorBackend):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "never"

    @property
    def model_name(self) -> str:
        return "never"

    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: object) -> tuple[str, None]:
        raise AssertionError(ctx)

    async def generate_request(
        self, request: GenerationRequest, **_kwargs: object
    ) -> tuple[str, None, tuple[str, ...]]:
        self.calls += 1
        raise AssertionError(request)


@pytest.mark.asyncio
async def test_invalid_contract_property_is_rejected_before_the_backend_call(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "src/invalid.ts"
    source_text = """/**
 * @prop given value: any :: normalize(value) equals value
 * @jauntContract
 */
export function normalize(value: string): string { return value; }
"""
    source.write_text(source_text)
    generator = _NeverGenerator()

    class ProjectionClient:
        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "projectContract"
            return {
                "source": "export function normalize(value: string): string;\n",
                "sourceDigest": "sha256:"
                + hashlib.sha256(str(params["source"]).encode()).hexdigest(),
                "symbol": params["symbol"],
                "kind": "function",
            }

    with pytest.raises(JauntConfigError, match="must not contain `any`"):
        await _generate_battery(
            tmp_path,
            config,
            ProjectionClient(),
            source,
            "normalize",
            source_text,
            generator,
            max_attempts=2,
        )

    assert generator.calls == 0


@pytest.mark.asyncio
async def test_contract_property_requires_fast_check_before_the_backend_call(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^4.0.0"}}) + "\n",
        encoding="utf-8",
    )
    vitest = tmp_path / "node_modules" / "vitest"
    vitest.mkdir(parents=True)
    (vitest / "package.json").write_text(
        json.dumps({"name": "vitest", "version": "4.1.10"}) + "\n",
        encoding="utf-8",
    )
    source = tmp_path / "src/contract.ts"
    source_text = """/**
 * @prop given value: string :: normalize(value) equals value
 * @jauntContract
 */
export function normalize(value: string): string { return value; }
"""
    source.write_text(source_text, encoding="utf-8")
    generator = _NeverGenerator()

    class ProjectionClient:
        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "projectContract"
            return {
                "source": "export function normalize(value: string): string;\n",
                "sourceDigest": "sha256:"
                + hashlib.sha256(str(params["source"]).encode()).hexdigest(),
                "symbol": params["symbol"],
                "kind": "function",
            }

    workspace = {
        "projects": [
            {
                "id": "tsconfig.test.json",
                "configPath": "tsconfig.test.json",
                "role": "test",
                "packageOwner": ".",
            }
        ]
    }
    with pytest.raises(JauntConfigError, match="directly declare devDependencies: fast-check"):
        await _generate_battery(
            tmp_path,
            config,
            ProjectionClient(),
            source,
            "normalize",
            source_text,
            generator,
            max_attempts=2,
            workspace=workspace,
        )

    assert generator.calls == 0
