import { dirname, relative, sep } from "node:path";
import type ts from "@typescript/typescript6";
import type {
  ClassMemberIR,
  ContractModuleIR,
  ParameterIR,
  SignatureIR,
  SymbolIR,
  TypeIR,
  TypeMemberIR,
  TypeParameterIR,
} from "./ir.js";

export function renderDocs(text: string, indent = ""): string {
  if (!text) return "";
  const lines = text.replaceAll("*/", "* /").split("\n");
  return `${indent}/**\n${lines.map((line) => `${indent} * ${line}`.trimEnd()).join("\n")}\n${indent} */\n`;
}

function renderTypeParameters(parameters: readonly TypeParameterIR[]): string {
  if (parameters.length === 0) return "";
  return `<${parameters
    .map(
      (parameter) =>
        `${parameter.name}${parameter.constraint ? ` extends ${renderType(parameter.constraint)}` : ""}${parameter.default ? ` = ${renderType(parameter.default)}` : ""}`,
    )
    .join(", ")}>`;
}

function renderParameter(parameter: ParameterIR): string {
  return `${parameter.rest ? "..." : ""}${parameter.name}${parameter.optional ? "?" : ""}: ${renderType(parameter.type)}`;
}

function renderTypeMember(member: TypeMemberIR): string {
  if (member.kind === "property") {
    return `${member.readonly ? "readonly " : ""}${member.name}${member.optional ? "?" : ""}: ${renderType(member.type ?? { kind: "unknown" })}`;
  }
  const signature = member.signatures?.[0];
  if (!signature) return `${member.name ?? "unknown"}: unknown`;
  const call = `${renderTypeParameters(signature.typeParameters)}(${signature.parameters.map(renderParameter).join(", ")}): ${renderType(signature.returnType)}`;
  if (member.kind === "method")
    return `${member.name}${member.optional ? "?" : ""}${call}`;
  if (member.kind === "construct") return `new ${call}`;
  if (member.kind === "index")
    return `[${signature.parameters.map(renderParameter).join(", ")}]: ${renderType(signature.returnType)}`;
  return call;
}

export function renderType(type: TypeIR): string {
  if (
    [
      "void",
      "undefined",
      "null",
      "never",
      "unknown",
      "any",
      "string",
      "number",
      "bigint",
      "boolean",
      "symbol",
      "intrinsic",
    ].includes(type.kind)
  ) {
    return type.kind;
  }
  if (type.kind === "object" && type.members === undefined) return "object";
  if (type.kind === "literal") return JSON.stringify(type.value ?? null);
  if (type.kind === "bigint-literal") return type.text ?? "0n";
  if (type.kind === "reference") {
    const args = type.typeArguments?.length
      ? `<${type.typeArguments.map((argument) => renderType(argument)).join(", ")}>`
      : "";
    return `${type.name ?? "unknown"}${args}`;
  }
  if (type.kind === "array")
    return `${
      type.element?.kind === "union" || type.element?.kind === "intersection"
        ? `(${renderType(type.element)})`
        : renderType(type.element ?? { kind: "unknown" })
    }[]`;
  if (type.kind === "tuple")
    return `[${(type.elements ?? []).map(renderType).join(", ")}]`;
  if (type.kind === "named-tuple")
    return `${type.name}: ${renderType(type.element ?? { kind: "unknown" })}`;
  if (type.kind === "optional")
    return `${renderType(type.element ?? { kind: "unknown" })}?`;
  if (type.kind === "rest")
    return `...${renderType(type.element ?? { kind: "unknown" })}`;
  if (type.kind === "union")
    return (type.types ?? []).map(renderType).join(" | ");
  if (type.kind === "intersection")
    return (type.types ?? []).map(renderType).join(" & ");
  if (type.kind === "function" || type.kind === "constructor") {
    const prefix = type.kind === "constructor" ? "new " : "";
    return `${prefix}${renderTypeParameters(type.typeParameters ?? [])}(${(type.parameters ?? []).map(renderParameter).join(", ")}) => ${renderType(type.returnType ?? { kind: "unknown" })}`;
  }
  if (type.kind === "object")
    return `{ ${(type.members ?? []).map(renderTypeMember).join("; ")} }`;
  if (type.kind === "indexed-access") {
    return `${renderType(type.object ?? { kind: "unknown" })}[${renderType(type.index ?? { kind: "unknown" })}]`;
  }
  if (type.kind === "conditional") {
    return `${renderType(type.check ?? { kind: "unknown" })} extends ${renderType(type.extends ?? { kind: "unknown" })} ? ${renderType(type.trueType ?? { kind: "unknown" })} : ${renderType(type.falseType ?? { kind: "unknown" })}`;
  }
  if (type.kind === "operator")
    return `${type.operator ?? "keyof"} ${renderType(type.element ?? { kind: "unknown" })}`;
  if (type.kind === "query") return `typeof ${type.name ?? "unknown"}`;
  if (type.kind === "import") {
    const args = type.typeArguments?.length
      ? `<${type.typeArguments.map((argument) => renderType(argument)).join(", ")}>`
      : "";
    return `import(${type.text ?? '""'})${type.name ? `.${type.name}` : ""}${args}`;
  }
  if (type.kind === "mapped") {
    const [readonly = "", optional = ""] = (type.operator ?? "|").split("|");
    const readonlyPrefix =
      readonly === "readonly"
        ? "readonly "
        : readonly === "+" || readonly === "-"
          ? `${readonly}readonly `
          : "";
    const optionalSuffix =
      optional === "?"
        ? "?"
        : optional === "+" || optional === "-"
          ? `${optional}?`
          : "";
    const key = type.name ?? "K";
    const nameType = type.index ? ` as ${renderType(type.index)}` : "";
    return `{ ${readonlyPrefix}[${key} in ${renderType(type.extends ?? { kind: "unknown" })}${nameType}]${optionalSuffix}: ${renderType(type.element ?? { kind: "unknown" })} }`;
  }
  if (type.kind === "template-literal") {
    const expressions = type.types ?? [];
    const tails = type.elements ?? [];
    return `\`${type.text ?? ""}${expressions
      .map(
        (expression, index) =>
          `\${${renderType(expression)}}${String(tails[index]?.value ?? "")}`,
      )
      .join("")}\``;
  }
  return "unknown";
}

function renderSignature(
  name: string,
  signature: SignatureIR,
  declare = true,
): string {
  return `${declare ? "export declare function " : ""}${name}${renderTypeParameters(signature.typeParameters)}(${signature.parameters.map(renderParameter).join(", ")}): ${renderType(signature.returnType)};`;
}

function renderClassMember(member: ClassMemberIR): string {
  const prefix = `${member.static ? "static " : ""}${member.readonly && member.kind === "property" ? "readonly " : ""}`;
  if (member.kind === "property") {
    return `${prefix}${member.name}${member.optional ? "?" : ""}: ${renderType(member.type ?? { kind: "unknown" })};`;
  }
  const signature = member.signatures[0] ?? {
    typeParameters: [],
    parameters: [],
    returnType: { kind: "void" },
  };
  if (member.kind === "constructor") {
    return `constructor(${signature.parameters.map(renderParameter).join(", ")});`;
  }
  if (member.kind === "getter")
    return `${prefix}get ${member.name}(): ${renderType(signature.returnType)};`;
  if (member.kind === "setter") {
    return `${prefix}set ${member.name}(${signature.parameters.map(renderParameter).join(", ")});`;
  }
  return `${prefix}${member.name}${member.optional ? "?" : ""}${renderTypeParameters(signature.typeParameters)}(${signature.parameters.map(renderParameter).join(", ")}): ${renderType(signature.returnType)};`;
}

function renderSymbol(symbol: SymbolIR): string {
  if (symbol.kind === "function") {
    return `${renderDocs(symbol.docs)}${symbol.signatures.map((signature) => renderSignature(symbol.name, signature)).join("\n")}`;
  }
  const heritage = symbol.heritage
    ? ` extends ${symbol.heritage.baseName}${
        symbol.heritage.typeArguments.length > 0
          ? `<${symbol.heritage.typeArguments.map(renderType).join(", ")}>`
          : ""
      }`
    : "";
  return `${renderDocs(symbol.docs)}export declare class ${symbol.name}${renderTypeParameters(symbol.typeParameters)}${heritage} {\n${symbol.members
    .filter((member) => !member.inheritedConstructor)
    .map(
      (member) =>
        `${renderDocs(member.docs, "  ")}  ${renderClassMember(member)}`,
    )
    .join("\n")}\n}`;
}

export function renderTypeImport(
  item: ContractModuleIR["typeImports"][number],
  runtime = item.runtime,
): string {
  const renderAsRuntime = runtime && !item.typeOnly;
  const parts: string[] = [];
  if (item.defaultImport) parts.push(item.defaultImport);
  if (item.namespaceImport) parts.push(`* as ${item.namespaceImport}`);
  if (item.namedImports.length > 0) {
    parts.push(
      `{ ${item.namedImports
        .map(
          (binding) =>
            `${renderAsRuntime && binding.typeOnly ? "type " : ""}${
              binding.imported === binding.local
                ? binding.imported
                : `${binding.imported} as ${binding.local}`
            }`,
        )
        .join(", ")} }`,
    );
  }
  return `import ${renderAsRuntime ? "" : "type "}${parts.join(", ")} from ${JSON.stringify(item.specifier)};`;
}

function isReferenceIdentifier(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Identifier,
): boolean {
  const parent = node.parent;
  if (
    ((compiler.isPropertySignature(parent) ||
      compiler.isMethodSignature(parent) ||
      compiler.isPropertyDeclaration(parent) ||
      compiler.isMethodDeclaration(parent) ||
      compiler.isGetAccessorDeclaration(parent) ||
      compiler.isSetAccessorDeclaration(parent)) &&
      parent.name === node &&
      !compiler.isComputedPropertyName(parent.name)) ||
    ((compiler.isFunctionDeclaration(parent) ||
      compiler.isClassDeclaration(parent) ||
      compiler.isInterfaceDeclaration(parent) ||
      compiler.isTypeAliasDeclaration(parent) ||
      compiler.isParameter(parent) ||
      compiler.isTypeParameterDeclaration(parent)) &&
      parent.name === node) ||
    (compiler.isQualifiedName(parent) && parent.right === node) ||
    (compiler.isPropertyAccessExpression(parent) && parent.name === node)
  ) {
    return false;
  }
  return true;
}

function surfaceUsesBinding(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  binding: string,
): boolean {
  let found = false;
  const bindingNameDeclares = (name: ts.BindingName): boolean => {
    if (compiler.isIdentifier(name)) return name.text === binding;
    return name.elements.some(
      (element) =>
        !compiler.isOmittedExpression(element) &&
        bindingNameDeclares(element.name),
    );
  };
  const isInside = (node: ts.Node, ancestor: ts.Node): boolean => {
    let current: ts.Node | undefined = node;
    while (current) {
      if (current === ancestor) return true;
      current = current.parent;
    }
    return false;
  };
  const isValueReference = (node: ts.Node): boolean => {
    let current: ts.Node | undefined = node.parent;
    while (current) {
      if (compiler.isTypeQueryNode(current))
        return isInside(node, current.exprName);
      current = current.parent;
    }
    return false;
  };
  const isShadowed = (node: ts.Node): boolean => {
    const valueReference = isValueReference(node);
    let current: ts.Node | undefined = node.parent;
    while (current) {
      const typeParameters = (
        current as ts.Node & {
          readonly typeParameters?: readonly ts.TypeParameterDeclaration[];
        }
      ).typeParameters;
      if (typeParameters?.some((parameter) => parameter.name.text === binding))
        return true;
      const parameters = (
        current as ts.Node & {
          readonly parameters?: readonly ts.ParameterDeclaration[];
        }
      ).parameters;
      if (
        valueReference &&
        parameters?.some((parameter) => bindingNameDeclares(parameter.name))
      )
        return true;
      current = current.parent;
    }
    return false;
  };
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (
      compiler.isIdentifier(node) &&
      node.text === binding &&
      isReferenceIdentifier(compiler, node) &&
      !isShadowed(node)
    ) {
      found = true;
      return;
    }
    compiler.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

function publicSurfaceImport(
  compiler: typeof import("@typescript/typescript6"),
  item: ContractModuleIR["typeImports"][number],
  sourceFile: ts.SourceFile,
): ContractModuleIR["typeImports"][number] | undefined {
  const defaultImport =
    item.defaultImport &&
    surfaceUsesBinding(compiler, sourceFile, item.defaultImport)
      ? item.defaultImport
      : undefined;
  const namespaceImport =
    item.namespaceImport &&
    surfaceUsesBinding(compiler, sourceFile, item.namespaceImport)
      ? item.namespaceImport
      : undefined;
  const namedImports = item.namedImports.filter((binding) =>
    surfaceUsesBinding(compiler, sourceFile, binding.local),
  );
  if (!defaultImport && !namespaceImport && namedImports.length === 0)
    return undefined;
  return {
    specifier: item.specifier,
    typeOnly: item.typeOnly,
    runtime: item.runtime,
    ...(defaultImport === undefined ? {} : { defaultImport }),
    ...(namespaceImport === undefined ? {} : { namespaceImport }),
    namedImports,
  };
}

export function renderApiMirror(
  compiler: typeof import("@typescript/typescript6"),
  ir: ContractModuleIR,
): string {
  const header = [
    "// ⛓️ jaunt:api-mirror — generated; do not edit.",
    `// jaunt:module=${ir.moduleId}`,
    `// jaunt:structural=${ir.structuralDigest}`,
    `// jaunt:prose=${ir.proseDigest}`,
    `// jaunt:api=${ir.apiDigest}`,
    "",
  ].join("\n");
  const types = ir.typeDeclarations
    .map(
      (declaration) =>
        `${renderDocs(declaration.docs)}${declaration.source.trim()}`,
    )
    .join("\n\n");
  const symbols = ir.symbols.map(renderSymbol).join("\n\n");
  // Dependency imports remain in contract IR for graph/prompt purposes, but an API
  // mirror is only the public declaration surface. Carrying implementation-only
  // imports into strict projects produces TS6133/TS6196 before a module is built.
  const publicSurface = [types, symbols].filter(Boolean).join("\n\n");
  const sourceFile = compiler.createSourceFile(
    ir.apiMirrorPath,
    publicSurface,
    compiler.ScriptTarget.Latest,
    true,
    ir.apiMirrorPath.endsWith(".tsx")
      ? compiler.ScriptKind.TSX
      : compiler.ScriptKind.TS,
  );
  const imports = ir.typeImports
    .map((item) => publicSurfaceImport(compiler, item, sourceFile))
    .filter(
      (item): item is ContractModuleIR["typeImports"][number] =>
        item !== undefined,
    )
    .map((item) => renderTypeImport(item))
    .join("\n");
  const body = [imports, types, symbols].filter(Boolean).join("\n\n");
  return `${header}${body}\n`;
}

export function relativeModuleSpecifier(
  fromPath: string,
  toPath: string,
): string {
  let value = relative(dirname(fromPath), toPath)
    .split(sep)
    .join("/")
    .replace(/\.(?:ts|tsx)$/, ".js");
  if (!value.startsWith(".")) value = `./${value}`;
  return value;
}

export function canonicalFacadeSource(ir: ContractModuleIR): string {
  const api = relativeModuleSpecifier(ir.facadePath, ir.apiMirrorPath);
  const implementation = relativeModuleSpecifier(
    ir.facadePath,
    ir.implementationPath,
  );
  const publicTypes = ir.typeDeclarations
    .map((declaration) => declaration.name)
    .sort();
  const lines = publicTypes.length
    ? [`export type { ${publicTypes.join(", ")} } from ${JSON.stringify(api)};`]
    : [];
  if (ir.contextPath)
    lines.push(
      `export * from ${JSON.stringify(relativeModuleSpecifier(ir.facadePath, ir.contextPath))};`,
    );
  lines.push(`export * from ${JSON.stringify(implementation)};`);
  return `${lines.join("\n")}\n`;
}

export function renderClassMemberForPlaceholder(member: ClassMemberIR): string {
  return renderClassMember(member);
}

export function renderParameterForAdapter(parameter: ParameterIR): string {
  return renderParameter(parameter);
}

export function renderTypeParametersForAdapter(
  parameters: readonly TypeParameterIR[],
): string {
  return renderTypeParameters(parameters);
}

export function renderRuntimeImports(ir: ContractModuleIR): readonly string[] {
  return ir.typeImports
    .filter((item) => item.runtime)
    .map((item) => renderTypeImport(item, true));
}

export function renderClassTypeAlias(symbol: SymbolIR): string {
  const parameters = renderTypeParameters(symbol.typeParameters);
  const arguments_ = symbol.typeParameters.length
    ? `<${symbol.typeParameters.map((parameter) => parameter.name).join(", ")}>`
    : "";
  return `export type ${symbol.name}${parameters} = __JauntApi.${symbol.name}${arguments_};`;
}
