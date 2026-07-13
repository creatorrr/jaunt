import type { ClassMemberIR, ContractModuleIR, SymbolIR } from "./ir.js";
import {
  renderClassTypeAlias,
  relativeModuleSpecifier,
  renderParameterForAdapter,
  renderRuntimeImports,
  renderType,
  renderTypeParametersForAdapter,
} from "./mirror.js";

function throwBody(): string {
  return '{ throw new Error("Jaunt implementation is unbuilt; run `jaunt build`"); }';
}

function renderPlaceholderClassMember(member: ClassMemberIR): string {
  const signature = member.signatures[0] ?? {
    typeParameters: [],
    parameters: [],
    returnType: { kind: "void" },
  };
  const prefix = member.static ? "static " : "";
  if (member.kind === "constructor") {
    return `  constructor(${signature.parameters.map(renderParameterForAdapter).join(", ")}) ${throwBody()}`;
  }
  if (member.kind === "property") {
    const readonly = member.readonly ? "readonly " : "";
    if (member.static && !member.optional) {
      const type = renderType(member.type ?? { kind: "unknown" });
      return [
        `  static get ${member.name}(): ${type} ${throwBody()}`,
        ...(member.readonly
          ? []
          : [`  static set ${member.name}(_value: ${type}) ${throwBody()}`]),
      ].join("\n");
    }
    return `  declare ${prefix}${readonly}${member.name}${member.optional ? "?" : ""}: ${renderType(member.type ?? { kind: "unknown" })};`;
  }
  if (member.kind === "getter") {
    return `  ${prefix}get ${member.name}(): ${renderType(signature.returnType)} ${throwBody()}`;
  }
  if (member.kind === "setter") {
    return `  ${prefix}set ${member.name}(${signature.parameters.map(renderParameterForAdapter).join(", ")}) ${throwBody()}`;
  }
  return `  ${prefix}${member.name}${member.optional ? "?" : ""}${renderTypeParametersForAdapter(signature.typeParameters)}(${signature.parameters.map(renderParameterForAdapter).join(", ")}): ${renderType(signature.returnType)} ${throwBody()}`;
}

function renderOverloadSignature(member: ClassMemberIR): string {
  const signature = member.signatures[0] ?? {
    typeParameters: [],
    parameters: [],
    returnType: { kind: "void" },
  };
  const prefix = member.static ? "static " : "";
  if (member.kind === "constructor") {
    return `  constructor(${signature.parameters.map(renderParameterForAdapter).join(", ")});`;
  }
  return `  ${prefix}${member.name}${member.optional ? "?" : ""}${renderTypeParametersForAdapter(signature.typeParameters)}(${signature.parameters.map(renderParameterForAdapter).join(", ")}): ${renderType(signature.returnType)};`;
}

function renderOverloadImplementation(member: ClassMemberIR): string {
  const prefix = member.static ? "static " : "";
  if (member.kind === "constructor")
    return `  constructor(..._args: unknown[]) ${throwBody()}`;
  return `  ${prefix}${member.name}(..._args: unknown[]): never ${throwBody()}`;
}

function renderClass(symbol: SymbolIR): string {
  const name = `__jaunt_unbuilt_${symbol.name}`;
  const grouped = new Map<string, ClassMemberIR[]>();
  const direct: string[] = [];
  for (const member of symbol.members) {
    if (member.inheritedConstructor) {
      const base = symbol.heritage!.implementationName.replace(
        "__jaunt_impl_",
        "__jaunt_unbuilt_",
      );
      const baseType = `${base}${
        symbol.heritage!.typeArguments.length
          ? `<${symbol.heritage!.typeArguments.map(renderType).join(", ")}>`
          : ""
      }`;
      direct.push(
        `  constructor(..._args: ConstructorParameters<typeof ${baseType}>) { super(..._args); throw new Error("Jaunt implementation is unbuilt; run \`jaunt build\`"); }`,
      );
      continue;
    }
    if (member.kind !== "constructor" && member.kind !== "method") {
      direct.push(renderPlaceholderClassMember(member));
      continue;
    }
    const key = `${member.static ? "static" : "instance"}:${member.kind}:${member.name}`;
    const values = grouped.get(key) ?? [];
    values.push(member);
    grouped.set(key, values);
  }
  const overloads = [...grouped.values()].flatMap((members) => [
    ...members.map(renderOverloadSignature),
    renderOverloadImplementation(members[0]!),
  ]);
  const members = [...overloads, ...direct].join("\n");
  const heritage = symbol.heritage
    ? ` extends ${symbol.heritage.implementationName.replace("__jaunt_impl_", "__jaunt_unbuilt_")}${
        symbol.heritage.typeArguments.length
          ? `<${symbol.heritage.typeArguments.map(renderType).join(", ")}>`
          : ""
      }`
    : "";
  return `class ${name}${renderTypeParametersForAdapter(symbol.typeParameters)}${heritage} {\n${members}\n}\nexport const ${symbol.name}: typeof __JauntApi.${symbol.name} = ${name};\n${renderClassTypeAlias(symbol)}`;
}

function renderFunction(symbol: SymbolIR): string {
  return `export const ${symbol.name}: typeof __JauntApi.${symbol.name} = __jaunt_unbuilt;`;
}

export function renderPlaceholder(ir: ContractModuleIR): string {
  const api = relativeModuleSpecifier(ir.implementationPath, ir.apiMirrorPath);
  const symbols = [...ir.symbols];
  symbols.sort((left, right) => {
    if (
      left.kind === "class" &&
      left.heritage?.implementationName === `__jaunt_impl_${right.name}`
    )
      return 1;
    if (
      right.kind === "class" &&
      right.heritage?.implementationName === `__jaunt_impl_${left.name}`
    )
      return -1;
    return left.name.localeCompare(right.name);
  });
  return [
    "// ⛓️ jaunt:generated — generated; do not edit.",
    "// jaunt:state=unbuilt",
    `// jaunt:module=${ir.moduleId}`,
    `// jaunt:structural=${ir.structuralDigest}`,
    `import type * as __JauntApi from ${JSON.stringify(api)};`,
    ...renderRuntimeImports(ir),
    "",
    `function __jaunt_unbuilt(): never ${throwBody()}`,
    "",
    ...symbols.map((symbol) =>
      symbol.kind === "class" ? renderClass(symbol) : renderFunction(symbol),
    ),
    "",
  ].join("\n");
}
