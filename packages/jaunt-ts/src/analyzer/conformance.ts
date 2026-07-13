import type { ContractModuleIR } from "./ir.js";
import { renderClassConformance } from "./class_conformance.js";
import { relativeModuleSpecifier, renderType } from "./mirror.js";
import type { TypeParameterIR } from "./ir.js";

function declarationTypeParameters(
  ir: ContractModuleIR,
  parameters: readonly TypeParameterIR[],
  namespace: "__JauntSpec" | "__JauntApi" | "__JauntFacade",
): string {
  if (parameters.length === 0) return "";
  const localNames = [
    ...ir.typeDeclarations.map((declaration) => declaration.name),
    ...ir.symbols
      .filter((symbol) => symbol.kind === "class")
      .map((symbol) => symbol.name),
  ].sort((left, right) => right.length - left.length);
  const qualify = (value: TypeParameterIR["constraint"]): string => {
    let rendered = renderType(value ?? { kind: "unknown" });
    for (const name of localNames) {
      rendered = rendered.replace(
        new RegExp(`(?<![.#\\w])${name}(?![\\w])`, "g"),
        `${namespace}.${name}`,
      );
    }
    return rendered;
  };
  return `<${parameters
    .map(
      (parameter) =>
        `${parameter.name}${parameter.constraint ? ` extends ${qualify(parameter.constraint)}` : ""}${parameter.default ? ` = ${qualify(parameter.default)}` : ""}`,
    )
    .join(", ")}>`;
}

function declarationTypeArguments(
  parameters: readonly TypeParameterIR[],
): string {
  return parameters.length
    ? `<${parameters.map((parameter) => parameter.name).join(", ")}>`
    : "";
}

export function renderConformanceSource(
  ir: ContractModuleIR,
  candidate: string,
): string {
  const spec = relativeModuleSpecifier(ir.implementationPath, ir.specPath);
  const checks = ir.symbols.flatMap((symbol) => {
    if (symbol.kind === "class") return [renderClassConformance(ir, symbol)];
    return [
      `const __jaunt_check_${symbol.name}: typeof __JauntSpec.${symbol.name} = __jaunt_impl_${symbol.name};`,
    ];
  });
  return [
    `import type * as __JauntSpec from ${JSON.stringify(spec)};`,
    candidate.trim(),
    "",
    ...checks,
    "export {};",
    "",
  ].join("\n");
}

export function renderMirrorConformanceSource(ir: ContractModuleIR): string {
  const spec = relativeModuleSpecifier(ir.implementationPath, ir.specPath);
  const api = relativeModuleSpecifier(ir.implementationPath, ir.apiMirrorPath);
  const checks = ir.symbols.flatMap((symbol, index) => [
    `declare const __spec_${index}: typeof __JauntSpec.${symbol.name};`,
    `const __api_from_spec_${index}: typeof __JauntApi.${symbol.name} = __spec_${index};`,
    `declare const __api_${index}: typeof __JauntApi.${symbol.name};`,
    `const __spec_from_api_${index}: typeof __JauntSpec.${symbol.name} = __api_${index};`,
  ]);
  const typeChecks = ir.typeDeclarations.map((declaration, index) => {
    const parameters = declarationTypeParameters(
      ir,
      declaration.typeParameters,
      "__JauntSpec",
    );
    const arguments_ = declarationTypeArguments(declaration.typeParameters);
    return `function __mirror_type_${index}${parameters}(spec: __JauntSpec.${declaration.name}${arguments_}, api: __JauntApi.${declaration.name}${arguments_}): void { const apiFromSpec: __JauntApi.${declaration.name}${arguments_} = spec; const specFromApi: __JauntSpec.${declaration.name}${arguments_} = api; void apiFromSpec; void specFromApi; }`;
  });
  return [
    `import type * as __JauntSpec from ${JSON.stringify(spec)};`,
    `import type * as __JauntApi from ${JSON.stringify(api)};`,
    ...checks,
    ...typeChecks,
    "export {};",
    "",
  ].join("\n");
}

export function renderFacadeConformanceSource(ir: ContractModuleIR): string {
  const facade = relativeModuleSpecifier(ir.implementationPath, ir.facadePath);
  const api = relativeModuleSpecifier(ir.implementationPath, ir.apiMirrorPath);
  const valueChecks = ir.symbols.map(
    (symbol, index) =>
      `const __facade_value_${index}: typeof __JauntApi.${symbol.name} = __JauntFacade.${symbol.name};`,
  );
  const declarationChecks = ir.typeDeclarations.flatMap(
    (declaration, index) => {
      const parameters = declarationTypeParameters(
        ir,
        declaration.typeParameters,
        "__JauntApi",
      );
      const arguments_ = declarationTypeArguments(declaration.typeParameters);
      return [
        `function __facade_type_${index}${parameters}(facade: __JauntFacade.${declaration.name}${arguments_}, api: __JauntApi.${declaration.name}${arguments_}): void { const apiFromFacade: __JauntApi.${declaration.name}${arguments_} = facade; const facadeFromApi: __JauntFacade.${declaration.name}${arguments_} = api; void apiFromFacade; void facadeFromApi; }`,
      ];
    },
  );
  const classChecks = ir.symbols
    .filter((symbol) => symbol.kind === "class")
    .flatMap((symbol, index) => {
      const parameters = declarationTypeParameters(
        ir,
        symbol.typeParameters,
        "__JauntApi",
      );
      const arguments_ = declarationTypeArguments(symbol.typeParameters);
      return [
        `function __facade_class_${index}${parameters}(facade: __JauntFacade.${symbol.name}${arguments_}, api: __JauntApi.${symbol.name}${arguments_}): void { const apiFromFacade: __JauntApi.${symbol.name}${arguments_} = facade; const facadeFromApi: __JauntFacade.${symbol.name}${arguments_} = api; void apiFromFacade; void facadeFromApi; }`,
      ];
    });
  return [
    `import * as __JauntFacade from ${JSON.stringify(facade)};`,
    `import type * as __JauntApi from ${JSON.stringify(api)};`,
    ...valueChecks,
    ...declarationChecks,
    ...classChecks,
    "export {};",
    "",
  ].join("\n");
}
