import type ts from "@typescript/typescript6";
import { dirname, relative, resolve, sep } from "node:path";
import { CONTRACT_IR_VERSION } from "../protocol/messages.js";
import { canonicalJson, digestCanonical } from "./canonical.js";
import { docsForNode } from "./docs.js";
import type {
  DiscoveredModule,
  DiscoveredSymbol,
  ParsedJauntOptions,
} from "./discovery.js";
import {
  collectTypeEnvironment,
  type TypeEnvironmentSnapshot,
} from "./type_environment.js";

export interface TypeIR {
  readonly kind: string;
  readonly name?: string;
  readonly value?: string | number | boolean | null;
  readonly operator?: string;
  readonly element?: TypeIR;
  readonly elements?: readonly TypeIR[];
  readonly types?: readonly TypeIR[];
  readonly object?: TypeIR;
  readonly index?: TypeIR;
  readonly check?: TypeIR;
  readonly extends?: TypeIR;
  readonly trueType?: TypeIR;
  readonly falseType?: TypeIR;
  readonly typeArguments?: readonly TypeIR[];
  readonly parameters?: readonly ParameterIR[];
  readonly typeParameters?: readonly TypeParameterIR[];
  readonly returnType?: TypeIR;
  readonly members?: readonly TypeMemberIR[];
  readonly text?: string;
}

export interface TypeParameterIR {
  readonly name: string;
  readonly constraint?: TypeIR;
  readonly default?: TypeIR;
}

export interface ParameterIR {
  readonly name: string;
  readonly type: TypeIR;
  readonly optional: boolean;
  readonly rest: boolean;
}

export interface SignatureIR {
  readonly typeParameters: readonly TypeParameterIR[];
  readonly parameters: readonly ParameterIR[];
  readonly returnType: TypeIR;
}

export interface TypeMemberIR {
  readonly kind: "property" | "method" | "call" | "construct" | "index";
  readonly name?: string;
  readonly optional?: boolean;
  readonly readonly?: boolean;
  readonly type?: TypeIR;
  readonly signatures?: readonly SignatureIR[];
}

export interface ClassMemberIR {
  readonly kind: "constructor" | "method" | "getter" | "setter" | "property";
  readonly name: string;
  readonly static: boolean;
  readonly readonly: boolean;
  readonly optional: boolean;
  readonly signatures: readonly SignatureIR[];
  readonly type?: TypeIR;
  readonly docs: string;
  readonly preserved: boolean;
  readonly preservedBody?: string;
  readonly preservedBodyDigest?: string;
  readonly synthetic?: boolean;
  readonly inheritedConstructor?: boolean;
}

export interface ClassHeritageIR {
  readonly baseName: string;
  readonly implementationName: string;
  readonly typeArguments: readonly TypeIR[];
  readonly resolvedBaseId?: string;
}

export interface SymbolIR {
  readonly id: string;
  readonly kind: "function" | "class";
  readonly name: string;
  readonly modifiers: readonly string[];
  readonly docs: string;
  readonly typeParameters: readonly TypeParameterIR[];
  readonly heritage?: ClassHeritageIR;
  readonly signatures: readonly SignatureIR[];
  readonly members: readonly ClassMemberIR[];
  readonly options: ParsedJauntOptions;
}

export interface TypeDeclarationIR {
  readonly name: string;
  readonly kind: "interface" | "type";
  readonly typeParameters: readonly TypeParameterIR[];
  readonly source: string;
  readonly docs: string;
  readonly digest: string;
}

export interface TypeImportIR {
  readonly specifier: string;
  readonly typeOnly: boolean;
  readonly runtime: boolean;
  readonly defaultImport?: string;
  readonly namespaceImport?: string;
  readonly namedImports: readonly {
    readonly imported: string;
    readonly local: string;
    readonly typeOnly: boolean;
  }[];
}

interface ImportIdentityContext {
  readonly named: ReadonlyMap<string, string>;
  readonly namespaces: ReadonlyMap<string, string>;
  readonly canonicalSpecifier: (specifier: string) => string;
}

export interface ContractModuleIR {
  readonly schema: typeof CONTRACT_IR_VERSION;
  readonly moduleId: string;
  readonly specPath: string;
  readonly facadePath: string;
  readonly apiMirrorPath: string;
  readonly implementationPath: string;
  readonly contextPath?: string;
  readonly project: string;
  readonly packageOwner: string;
  readonly symbols: readonly SymbolIR[];
  readonly options: ParsedJauntOptions;
  readonly typeDeclarations: readonly TypeDeclarationIR[];
  readonly typeImports: readonly TypeImportIR[];
  /** Canonical imported/context documentation used by the semantic gate. */
  readonly contextDocs: TypeEnvironmentSnapshot["proseRecords"];
  /** Persisted proof for model-free upgrades; excludes only Jaunt tool metadata. */
  readonly semanticEnvironmentDigest?: string;
  readonly dependencies: readonly string[];
  readonly structuralDigest: string;
  readonly proseDigest: string;
  readonly apiDigest: string;
  readonly fingerprint?: {
    readonly toolVersion: string;
    readonly workerVersion: string;
    readonly typescriptVersion: string;
    readonly compilerOptionsHash: string;
    readonly generationFingerprint: string;
    readonly protocol: string;
    readonly ir: string;
  };
}

function keywordName(
  compiler: typeof import("@typescript/typescript6"),
  kind: ts.SyntaxKind,
): string | undefined {
  if (kind === compiler.SyntaxKind.VoidKeyword) return "void";
  if (kind === compiler.SyntaxKind.UndefinedKeyword) return "undefined";
  if (kind === compiler.SyntaxKind.NeverKeyword) return "never";
  if (kind === compiler.SyntaxKind.UnknownKeyword) return "unknown";
  if (kind === compiler.SyntaxKind.AnyKeyword) return "any";
  if (kind === compiler.SyntaxKind.StringKeyword) return "string";
  if (kind === compiler.SyntaxKind.NumberKeyword) return "number";
  if (kind === compiler.SyntaxKind.BigIntKeyword) return "bigint";
  if (kind === compiler.SyntaxKind.BooleanKeyword) return "boolean";
  if (kind === compiler.SyntaxKind.SymbolKeyword) return "symbol";
  if (kind === compiler.SyntaxKind.ObjectKeyword) return "object";
  if (kind === compiler.SyntaxKind.IntrinsicKeyword) return "intrinsic";
  return undefined;
}

function entityNameText(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.EntityName,
): string {
  return compiler.isIdentifier(node)
    ? node.text
    : `${entityNameText(compiler, node.left)}.${node.right.text}`;
}

function expressionNameText(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Expression,
): string {
  if (compiler.isIdentifier(node)) return node.text;
  if (compiler.isPropertyAccessExpression(node)) {
    return `${expressionNameText(compiler, node.expression)}.${node.name.text}`;
  }
  return node.getText();
}

function propertyNameText(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.PropertyName | undefined,
): string {
  if (!node) return "";
  if (
    compiler.isIdentifier(node) ||
    compiler.isStringLiteral(node) ||
    compiler.isNumericLiteral(node)
  ) {
    return node.text;
  }
  if (compiler.isPrivateIdentifier(node)) return `#${node.text}`;
  return "[computed]";
}

function hasModifier(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  kind: ts.SyntaxKind,
): boolean {
  return compiler.canHaveModifiers(node)
    ? (compiler
        .getModifiers(node)
        ?.some((modifier) => modifier.kind === kind) ?? false)
    : false;
}

function serializeTypeParameters(
  compiler: typeof import("@typescript/typescript6"),
  nodes: ts.NodeArray<ts.TypeParameterDeclaration> | undefined,
): TypeParameterIR[] {
  return (nodes ?? []).map((node) => ({
    name: node.name.text,
    ...(node.constraint
      ? { constraint: serializeType(compiler, node.constraint) }
      : {}),
    ...(node.default ? { default: serializeType(compiler, node.default) } : {}),
  }));
}

function serializeParameters(
  compiler: typeof import("@typescript/typescript6"),
  nodes: ts.NodeArray<ts.ParameterDeclaration>,
): ParameterIR[] {
  return nodes.map((node) => ({
    name: compiler.isIdentifier(node.name)
      ? node.name.text
      : node.name.getText(),
    type: node.type ? serializeType(compiler, node.type) : { kind: "missing" },
    optional: node.questionToken !== undefined,
    rest: node.dotDotDotToken !== undefined,
  }));
}

export function serializeSignature(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.SignatureDeclarationBase,
): SignatureIR {
  return {
    typeParameters: serializeTypeParameters(compiler, node.typeParameters),
    parameters: serializeParameters(compiler, node.parameters),
    returnType:
      "type" in node && node.type && compiler.isTypeNode(node.type)
        ? serializeType(compiler, node.type)
        : { kind: "void" },
  };
}

function serializeTypeMembers(
  compiler: typeof import("@typescript/typescript6"),
  members: ts.NodeArray<ts.TypeElement>,
): TypeMemberIR[] {
  return members.map((member) => {
    if (compiler.isPropertySignature(member)) {
      return {
        kind: "property",
        name: propertyNameText(compiler, member.name),
        optional: member.questionToken !== undefined,
        readonly: hasModifier(
          compiler,
          member,
          compiler.SyntaxKind.ReadonlyKeyword,
        ),
        type: member.type
          ? serializeType(compiler, member.type)
          : { kind: "missing" },
      };
    }
    if (compiler.isMethodSignature(member)) {
      return {
        kind: "method",
        name: propertyNameText(compiler, member.name),
        optional: member.questionToken !== undefined,
        signatures: [serializeSignature(compiler, member)],
      };
    }
    if (compiler.isCallSignatureDeclaration(member)) {
      return {
        kind: "call",
        signatures: [serializeSignature(compiler, member)],
      };
    }
    if (compiler.isConstructSignatureDeclaration(member)) {
      return {
        kind: "construct",
        signatures: [serializeSignature(compiler, member)],
      };
    }
    if (compiler.isIndexSignatureDeclaration(member)) {
      return {
        kind: "index",
        signatures: [serializeSignature(compiler, member)],
      };
    }
    return {
      kind: "property",
      name: "unsupported",
      type: { kind: "unsupported" },
    };
  });
}

export function serializeType(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.TypeNode,
): TypeIR {
  const keyword = keywordName(compiler, node.kind);
  if (keyword) return { kind: keyword };
  if (compiler.isParenthesizedTypeNode(node))
    return serializeType(compiler, node.type);
  if (compiler.isLiteralTypeNode(node)) {
    const literal = node.literal;
    if (literal.kind === compiler.SyntaxKind.NullKeyword)
      return { kind: "literal", value: null };
    if (literal.kind === compiler.SyntaxKind.TrueKeyword)
      return { kind: "literal", value: true };
    if (literal.kind === compiler.SyntaxKind.FalseKeyword)
      return { kind: "literal", value: false };
    if (compiler.isStringLiteral(literal))
      return { kind: "literal", value: literal.text };
    if (compiler.isNumericLiteral(literal))
      return { kind: "literal", value: Number(literal.text) };
    if (compiler.isBigIntLiteral(literal))
      return { kind: "bigint-literal", text: literal.getText() };
    if (
      compiler.isPrefixUnaryExpression(literal) &&
      compiler.isNumericLiteral(literal.operand)
    ) {
      return { kind: "literal", value: -Number(literal.operand.text) };
    }
    if (
      compiler.isPrefixUnaryExpression(literal) &&
      compiler.isBigIntLiteral(literal.operand)
    ) {
      return { kind: "bigint-literal", text: literal.getText() };
    }
    return { kind: "literal", text: literal.getText() };
  }
  if (compiler.isTypeReferenceNode(node)) {
    return {
      kind: "reference",
      name: entityNameText(compiler, node.typeName),
      typeArguments:
        node.typeArguments?.map((argument) =>
          serializeType(compiler, argument),
        ) ?? [],
    };
  }
  if (compiler.isArrayTypeNode(node))
    return {
      kind: "array",
      element: serializeType(compiler, node.elementType),
    };
  if (compiler.isTupleTypeNode(node)) {
    return {
      kind: "tuple",
      elements: node.elements.map((element) =>
        serializeType(compiler, element),
      ),
    };
  }
  if (compiler.isNamedTupleMember(node)) {
    return {
      kind: node.dotDotDotToken
        ? "rest"
        : node.questionToken
          ? "optional"
          : "named-tuple",
      name: node.name.text,
      element: serializeType(compiler, node.type),
    };
  }
  if (compiler.isOptionalTypeNode(node))
    return { kind: "optional", element: serializeType(compiler, node.type) };
  if (compiler.isRestTypeNode(node))
    return { kind: "rest", element: serializeType(compiler, node.type) };
  if (compiler.isUnionTypeNode(node)) {
    return {
      kind: "union",
      types: node.types
        .map((type) => serializeType(compiler, type))
        .sort((a, b) => canonicalJson(a).localeCompare(canonicalJson(b))),
    };
  }
  if (compiler.isIntersectionTypeNode(node)) {
    return {
      kind: "intersection",
      types: node.types.map((type) => serializeType(compiler, type)),
    };
  }
  if (
    compiler.isFunctionTypeNode(node) ||
    compiler.isConstructorTypeNode(node)
  ) {
    const signature = serializeSignature(compiler, node);
    return {
      kind: compiler.isFunctionTypeNode(node) ? "function" : "constructor",
      typeParameters: signature.typeParameters,
      parameters: signature.parameters,
      returnType: signature.returnType,
    };
  }
  if (compiler.isTypeLiteralNode(node)) {
    return {
      kind: "object",
      members: serializeTypeMembers(compiler, node.members),
    };
  }
  if (compiler.isIndexedAccessTypeNode(node)) {
    return {
      kind: "indexed-access",
      object: serializeType(compiler, node.objectType),
      index: serializeType(compiler, node.indexType),
    };
  }
  if (compiler.isConditionalTypeNode(node)) {
    return {
      kind: "conditional",
      check: serializeType(compiler, node.checkType),
      extends: serializeType(compiler, node.extendsType),
      trueType: serializeType(compiler, node.trueType),
      falseType: serializeType(compiler, node.falseType),
    };
  }
  if (compiler.isTypeOperatorNode(node)) {
    return {
      kind: "operator",
      operator: compiler.tokenToString(node.operator) ?? String(node.operator),
      element: serializeType(compiler, node.type),
    };
  }
  if (compiler.isTypeQueryNode(node))
    return {
      kind: "query",
      name: entityNameText(compiler, node.exprName),
    };
  if (compiler.isImportTypeNode(node)) {
    const argument =
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteralLike(node.argument.literal)
        ? JSON.stringify(node.argument.literal.text)
        : node.argument.getText();
    return {
      kind: "import",
      text: argument,
      ...(node.qualifier
        ? { name: entityNameText(compiler, node.qualifier) }
        : {}),
      typeArguments:
        node.typeArguments?.map((argument) =>
          serializeType(compiler, argument),
        ) ?? [],
    };
  }
  if (compiler.isMappedTypeNode(node)) {
    return {
      kind: "mapped",
      name: node.typeParameter.name.text,
      extends: node.typeParameter.constraint
        ? serializeType(compiler, node.typeParameter.constraint)
        : { kind: "unknown" },
      ...(node.type ? { element: serializeType(compiler, node.type) } : {}),
      ...(node.nameType
        ? { index: serializeType(compiler, node.nameType) }
        : {}),
      operator: `${node.readonlyToken?.getText() ?? ""}|${node.questionToken?.getText() ?? ""}`,
    };
  }
  if (compiler.isTemplateLiteralTypeNode(node)) {
    return {
      kind: "template-literal",
      text: node.head.text,
      types: node.templateSpans.map((span) =>
        serializeType(compiler, span.type),
      ),
      elements: node.templateSpans.map((span) => ({
        kind: "literal",
        value: span.literal.text,
      })),
    };
  }
  return { kind: `syntax-${node.kind}` };
}

function stableImportedName(specifier: string, name?: string): string {
  return `import(${JSON.stringify(specifier)})${name ? `.${name}` : ""}`;
}

function canonicalModuleSpecifier(
  module: DiscoveredModule,
  root: string,
  specifier: string,
): string {
  let canonical = specifier;
  if (canonical.startsWith(".")) {
    let target = resolve(dirname(module.sourceFile.fileName), canonical);
    target = target.replace(/\.js$/, ".ts").replace(/\.jsx$/, ".tsx");
    target = target.replace(/\.jaunt\.(ts|tsx)$/, ".$1");
    canonical = relative(
      dirname(resolve(root, module.route.apiMirrorPath)),
      target,
    )
      .split(sep)
      .join("/")
      .replace(/\.(?:ts|tsx)$/, ".js");
    if (!canonical.startsWith(".")) canonical = `./${canonical}`;
  }
  // Path aliases can name private inputs without being relative. Generated
  // mirrors/implementations must always target the corresponding facade.
  return canonical.replace(/\.jaunt(?=\.(?:js|jsx|ts|tsx)$)/, "");
}

function importIdentityContext(
  module: DiscoveredModule,
  root: string,
  imports: readonly TypeImportIR[],
): ImportIdentityContext {
  const named = new Map<string, string>();
  const namespaces = new Map<string, string>();
  for (const item of imports) {
    if (item.defaultImport) {
      named.set(
        item.defaultImport,
        stableImportedName(item.specifier, "default"),
      );
    }
    if (item.namespaceImport) {
      namespaces.set(item.namespaceImport, stableImportedName(item.specifier));
    }
    for (const binding of item.namedImports) {
      named.set(
        binding.local,
        stableImportedName(item.specifier, binding.imported),
      );
    }
  }
  return {
    named,
    namespaces,
    canonicalSpecifier: (specifier) =>
      canonicalModuleSpecifier(module, root, specifier),
  };
}

function normalizedReferenceName(
  name: string,
  imports: ImportIdentityContext,
  boundNames: ReadonlySet<string>,
): string {
  const [root, ...rest] = name.split(".");
  if (!root || boundNames.has(root)) return name;
  const direct = imports.named.get(root);
  if (direct) return rest.length > 0 ? `${direct}.${rest.join(".")}` : direct;
  const namespace = imports.namespaces.get(root);
  if (namespace) {
    return rest.length > 0 ? `${namespace}.${rest.join(".")}` : namespace;
  }
  return name;
}

function normalizedTypeParameters(
  parameters: readonly TypeParameterIR[],
  imports: ImportIdentityContext,
  outerNames: ReadonlySet<string>,
): readonly TypeParameterIR[] {
  const boundNames = new Set(outerNames);
  for (const parameter of parameters) boundNames.add(parameter.name);
  return parameters.map((parameter) => ({
    ...parameter,
    ...(parameter.constraint
      ? {
          constraint: normalizedType(parameter.constraint, imports, boundNames),
        }
      : {}),
    ...(parameter.default
      ? { default: normalizedType(parameter.default, imports, boundNames) }
      : {}),
  }));
}

function normalizedSignature(
  signature: SignatureIR,
  imports: ImportIdentityContext,
  outerNames: ReadonlySet<string>,
): SignatureIR {
  const boundNames = new Set(outerNames);
  for (const parameter of signature.typeParameters)
    boundNames.add(parameter.name);
  return {
    typeParameters: normalizedTypeParameters(
      signature.typeParameters,
      imports,
      outerNames,
    ),
    parameters: signature.parameters.map((parameter) => ({
      ...parameter,
      type: normalizedType(parameter.type, imports, boundNames),
    })),
    returnType: normalizedType(signature.returnType, imports, boundNames),
  };
}

function normalizedTypeMember(
  member: TypeMemberIR,
  imports: ImportIdentityContext,
  boundNames: ReadonlySet<string>,
): TypeMemberIR {
  return {
    ...member,
    ...(member.type
      ? { type: normalizedType(member.type, imports, boundNames) }
      : {}),
    ...(member.signatures
      ? {
          signatures: member.signatures.map((signature) =>
            normalizedSignature(signature, imports, boundNames),
          ),
        }
      : {}),
  };
}

function normalizedImportTypeText(
  text: string | undefined,
  imports: ImportIdentityContext,
): string | undefined {
  if (text === undefined) return undefined;
  try {
    const specifier: unknown = JSON.parse(text);
    return typeof specifier === "string"
      ? JSON.stringify(imports.canonicalSpecifier(specifier))
      : text;
  } catch {
    return text;
  }
}

function normalizedType(
  type: TypeIR,
  imports: ImportIdentityContext,
  boundNames: ReadonlySet<string>,
): TypeIR {
  const nestedNames = new Set(boundNames);
  for (const parameter of type.typeParameters ?? [])
    nestedNames.add(parameter.name);
  const mappedNames = new Set(nestedNames);
  if (type.kind === "mapped" && type.name) mappedNames.add(type.name);
  const nested = type.kind === "mapped" ? mappedNames : nestedNames;
  const importText =
    type.kind === "import"
      ? normalizedImportTypeText(type.text, imports)
      : undefined;
  return {
    ...type,
    ...(type.name !== undefined &&
    (type.kind === "reference" || type.kind === "query")
      ? {
          name: normalizedReferenceName(type.name, imports, boundNames),
        }
      : {}),
    ...(importText !== undefined ? { text: importText } : {}),
    ...(type.element
      ? { element: normalizedType(type.element, imports, nested) }
      : {}),
    ...(type.elements
      ? {
          elements: type.elements.map((element) =>
            normalizedType(element, imports, nested),
          ),
        }
      : {}),
    ...(type.types
      ? {
          types: type.types.map((item) =>
            normalizedType(item, imports, nested),
          ),
        }
      : {}),
    ...(type.object
      ? { object: normalizedType(type.object, imports, nested) }
      : {}),
    ...(type.index
      ? { index: normalizedType(type.index, imports, nested) }
      : {}),
    ...(type.check
      ? { check: normalizedType(type.check, imports, nested) }
      : {}),
    ...(type.extends
      ? {
          extends: normalizedType(
            type.extends,
            imports,
            type.kind === "mapped" ? nestedNames : nested,
          ),
        }
      : {}),
    ...(type.trueType
      ? { trueType: normalizedType(type.trueType, imports, nested) }
      : {}),
    ...(type.falseType
      ? { falseType: normalizedType(type.falseType, imports, nested) }
      : {}),
    ...(type.typeArguments
      ? {
          typeArguments: type.typeArguments.map((argument) =>
            normalizedType(argument, imports, nested),
          ),
        }
      : {}),
    ...(type.typeParameters
      ? {
          typeParameters: normalizedTypeParameters(
            type.typeParameters,
            imports,
            boundNames,
          ),
        }
      : {}),
    ...(type.parameters
      ? {
          parameters: type.parameters.map((parameter) => ({
            ...parameter,
            type: normalizedType(parameter.type, imports, nested),
          })),
        }
      : {}),
    ...(type.returnType
      ? { returnType: normalizedType(type.returnType, imports, nested) }
      : {}),
    ...(type.members
      ? {
          members: type.members.map((member) =>
            normalizedTypeMember(member, imports, nested),
          ),
        }
      : {}),
  };
}

function normalizedSymbol(
  symbol: SymbolIR,
  imports: ImportIdentityContext,
): SymbolIR {
  const boundNames = new Set(symbol.typeParameters.map(({ name }) => name));
  return {
    ...symbol,
    typeParameters: normalizedTypeParameters(
      symbol.typeParameters,
      imports,
      new Set(),
    ),
    ...(symbol.heritage
      ? {
          heritage: {
            ...symbol.heritage,
            baseName: normalizedReferenceName(
              symbol.heritage.baseName,
              imports,
              boundNames,
            ),
            implementationName: symbol.heritage.implementationName.startsWith(
              "__jaunt_impl_",
            )
              ? symbol.heritage.implementationName
              : normalizedReferenceName(
                  symbol.heritage.implementationName,
                  imports,
                  boundNames,
                ),
            typeArguments: symbol.heritage.typeArguments.map((argument) =>
              normalizedType(argument, imports, boundNames),
            ),
          },
        }
      : {}),
    signatures: symbol.signatures.map((signature) =>
      normalizedSignature(signature, imports, boundNames),
    ),
    members: symbol.members.map((member) => ({
      ...member,
      signatures: member.signatures.map((signature) =>
        normalizedSignature(signature, imports, boundNames),
      ),
      ...(member.type
        ? { type: normalizedType(member.type, imports, boundNames) }
        : {}),
    })),
  };
}

function structuralTypeImports(
  imports: readonly TypeImportIR[],
): readonly unknown[] {
  return imports
    .map((item) => ({
      specifier: item.specifier,
      typeOnly: item.typeOnly,
      runtime: item.runtime,
      ...(item.defaultImport ? { defaultImport: "default" } : {}),
      ...(item.namespaceImport ? { namespaceImport: "*" } : {}),
      namedImports: item.namedImports
        .map(({ imported, typeOnly }) => ({ imported, typeOnly }))
        .sort((left, right) =>
          canonicalJson(left).localeCompare(canonicalJson(right)),
        ),
    }))
    .sort((left, right) =>
      canonicalJson(left).localeCompare(canonicalJson(right)),
    );
}

function typeDeclarationDigest(
  compiler: typeof import("@typescript/typescript6"),
  declaration: ts.InterfaceDeclaration | ts.TypeAliasDeclaration,
  imports: ImportIdentityContext,
): string {
  const typeParameters = serializeTypeParameters(
    compiler,
    declaration.typeParameters,
  );
  const boundNames = new Set(typeParameters.map(({ name }) => name));
  return digestCanonical(
    compiler.isTypeAliasDeclaration(declaration)
      ? normalizedType(
          serializeType(compiler, declaration.type),
          imports,
          boundNames,
        )
      : {
          typeParameters: normalizedTypeParameters(
            typeParameters,
            imports,
            new Set(),
          ),
          heritage: (declaration.heritageClauses ?? []).map((clause) =>
            clause.types.map((type) => ({
              expression: normalizedReferenceName(
                expressionNameText(compiler, type.expression),
                imports,
                boundNames,
              ),
              typeArguments: (type.typeArguments ?? []).map((argument) =>
                normalizedType(
                  serializeType(compiler, argument),
                  imports,
                  boundNames,
                ),
              ),
            })),
          ),
          members: serializeTypeMembers(compiler, declaration.members).map(
            (member) => normalizedTypeMember(member, imports, boundNames),
          ),
        },
  );
}

function classMembers(
  compiler: typeof import("@typescript/typescript6"),
  module: DiscoveredModule,
  symbol: Extract<DiscoveredSymbol, { kind: "class" }>,
): ClassMemberIR[] {
  const output: ClassMemberIR[] = [];
  const overloadKeys = new Set<string>();
  for (const member of symbol.declaration.members) {
    if (
      (!compiler.isConstructorDeclaration(member) &&
        !compiler.isMethodDeclaration(member)) ||
      member.body !== undefined
    )
      continue;
    const name = compiler.isConstructorDeclaration(member)
      ? "constructor"
      : propertyNameText(compiler, member.name);
    overloadKeys.add(
      `${hasModifier(compiler, member, compiler.SyntaxKind.StaticKeyword) ? "static" : "instance"}:${name}`,
    );
  }
  const printer = compiler.createPrinter({
    newLine: compiler.NewLineKind.LineFeed,
    removeComments: false,
  });
  function preservedBody(
    member:
      | ts.MethodDeclaration
      | ts.GetAccessorDeclaration
      | ts.SetAccessorDeclaration,
  ): Pick<
    ClassMemberIR,
    "preserved" | "preservedBody" | "preservedBodyDigest"
  > {
    const preserved =
      docsForNode(compiler, module.sourceFile, member).tags.jauntPreserve !==
      undefined;
    if (!preserved || !member.body) return { preserved };
    const source = printer
      .printNode(compiler.EmitHint.Unspecified, member.body, module.sourceFile)
      .trim();
    const scanner = compiler.createScanner(
      compiler.ScriptTarget.Latest,
      true,
      compiler.LanguageVariant.Standard,
      source,
    );
    const tokens: [number, string][] = [];
    for (
      let token = scanner.scan();
      token !== compiler.SyntaxKind.EndOfFileToken;
      token = scanner.scan()
    ) {
      const value =
        token === compiler.SyntaxKind.StringLiteral ||
        token === compiler.SyntaxKind.NoSubstitutionTemplateLiteral
          ? scanner.getTokenValue()
          : scanner.getTokenText();
      tokens.push([token, value]);
    }
    return {
      preserved: true,
      preservedBody: source,
      preservedBodyDigest: digestCanonical(tokens),
    };
  }
  for (const member of symbol.declaration.members) {
    const staticMember = hasModifier(
      compiler,
      member,
      compiler.SyntaxKind.StaticKeyword,
    );
    const readonly = hasModifier(
      compiler,
      member,
      compiler.SyntaxKind.ReadonlyKeyword,
    );
    const docs = docsForNode(compiler, module.sourceFile, member).text;
    if (
      (compiler.isConstructorDeclaration(member) ||
        compiler.isMethodDeclaration(member)) &&
      member.body !== undefined
    ) {
      const name = compiler.isConstructorDeclaration(member)
        ? "constructor"
        : propertyNameText(compiler, member.name);
      const key = `${staticMember ? "static" : "instance"}:${name}`;
      if (overloadKeys.has(key)) continue;
    }
    if (compiler.isConstructorDeclaration(member)) {
      output.push({
        kind: "constructor",
        name: "constructor",
        static: false,
        readonly: false,
        optional: false,
        signatures: [serializeSignature(compiler, member)],
        docs,
        preserved: false,
      });
    } else if (compiler.isMethodDeclaration(member)) {
      output.push({
        kind: "method",
        name: propertyNameText(compiler, member.name),
        static: staticMember,
        readonly: false,
        optional: member.questionToken !== undefined,
        signatures: [serializeSignature(compiler, member)],
        docs,
        ...preservedBody(member),
      });
    } else if (
      compiler.isGetAccessorDeclaration(member) ||
      compiler.isSetAccessorDeclaration(member)
    ) {
      output.push({
        kind: compiler.isGetAccessorDeclaration(member) ? "getter" : "setter",
        name: propertyNameText(compiler, member.name),
        static: staticMember,
        readonly: compiler.isGetAccessorDeclaration(member),
        optional: false,
        signatures: [serializeSignature(compiler, member)],
        docs,
        ...preservedBody(member),
      });
    } else if (compiler.isPropertyDeclaration(member)) {
      output.push({
        kind: "property",
        name: propertyNameText(compiler, member.name),
        static: staticMember,
        readonly,
        optional: member.questionToken !== undefined,
        signatures: [],
        type: member.type
          ? serializeType(compiler, member.type)
          : { kind: "missing" },
        docs,
        preserved: false,
      });
    }
  }
  if (!output.some((member) => member.kind === "constructor")) {
    output.unshift({
      kind: "constructor",
      name: "constructor",
      static: false,
      readonly: false,
      optional: false,
      signatures: symbol.heritage
        ? []
        : [
            {
              typeParameters: [],
              parameters: [],
              returnType: { kind: "void" },
            },
          ],
      docs: "",
      preserved: false,
      synthetic: true,
      ...(symbol.heritage ? { inheritedConstructor: true } : {}),
    });
  }
  return output;
}

function resolvedOptions(
  module: DiscoveredModule,
  options: ParsedJauntOptions,
  resolvedDependencies: readonly string[],
): ParsedJauntOptions {
  if (options.deps === undefined) return options;
  const dependencies = module.dependencyResolutionComplete
    ? resolvedDependencies
    : options.deps.map((name) => `${module.route.moduleId}#${name}`);
  return { ...options, deps: dependencies };
}

function symbolIr(
  compiler: typeof import("@typescript/typescript6"),
  module: DiscoveredModule,
  symbol: DiscoveredSymbol,
): SymbolIR {
  if (symbol.kind === "function") {
    const publicDeclarations = symbol.declarations.some(
      (declaration) => declaration.body === undefined,
    )
      ? symbol.declarations.filter(
          (declaration) => declaration.body === undefined,
        )
      : symbol.declarations;
    return {
      id: `${module.route.moduleId}#${symbol.name}`,
      kind: "function",
      name: symbol.name,
      modifiers: ["export"],
      docs: symbol.docs.text,
      typeParameters: [],
      signatures: publicDeclarations.map((declaration) =>
        serializeSignature(compiler, declaration),
      ),
      members: [],
      options: resolvedOptions(
        module,
        symbol.options,
        symbol.resolvedDependencies,
      ),
    };
  }
  return {
    id: `${module.route.moduleId}#${symbol.name}`,
    kind: "class",
    name: symbol.name,
    modifiers: ["export"],
    docs: symbol.docs.text,
    typeParameters: serializeTypeParameters(
      compiler,
      symbol.declaration.typeParameters,
    ),
    ...(symbol.heritage
      ? {
          heritage: {
            baseName: symbol.heritage.baseName,
            implementationName:
              symbol.heritage.resolvedBaseIds.some(
                (id) =>
                  id ===
                  `${module.route.moduleId}#${symbol.heritage!.baseName}`,
              ) ||
              module.symbols.some(
                (candidate) =>
                  candidate.kind === "class" &&
                  candidate.name === symbol.heritage!.baseName,
              )
                ? `__jaunt_impl_${symbol.heritage.baseName}`
                : symbol.heritage.baseName,
            typeArguments: symbol.heritage.typeArguments.map((argument) =>
              serializeType(compiler, argument),
            ),
            ...(symbol.heritage.resolvedBaseIds[0]
              ? { resolvedBaseId: symbol.heritage.resolvedBaseIds[0] }
              : {}),
          },
        }
      : {}),
    signatures: [],
    members: classMembers(compiler, module, symbol),
    options: resolvedOptions(
      module,
      symbol.options,
      symbol.resolvedDependencies,
    ),
  };
}

interface ContractBuildContext {
  readonly apiDigests: Map<string, string>;
  readonly visiting: Set<string>;
}

function workspaceRoot(module: DiscoveredModule): string {
  return resolve(
    dirname(module.sourceFile.fileName),
    relative(dirname(module.route.specPath), "."),
  );
}

function buildContractIRInternal(
  compiler: typeof import("@typescript/typescript6"),
  module: DiscoveredModule,
  typeEnvironment: TypeEnvironmentSnapshot | undefined,
  context: ContractBuildContext,
): ContractModuleIR {
  context.visiting.add(module.route.moduleId);
  const symbols = module.symbols.map((symbol) =>
    symbolIr(compiler, module, symbol),
  );
  const typeDeclarationRecords = module.typeDeclarations.map((declaration) => {
    const source = declaration
      .getText(module.sourceFile)
      .replace(/\{[\s\S]*$/, (tail) => tail);
    return {
      name: declaration.name.text,
      kind: compiler.isInterfaceDeclaration(declaration)
        ? ("interface" as const)
        : ("type" as const),
      typeParameters: serializeTypeParameters(
        compiler,
        declaration.typeParameters,
      ),
      source,
      docs: docsForNode(compiler, module.sourceFile, declaration).text,
    };
  });
  const root = workspaceRoot(module);
  const runtimeImportNames = new Set(
    module.symbols.flatMap((symbol) =>
      symbol.kind === "class" ? symbol.runtimeImportNames : [],
    ),
  );
  const typeImports: TypeImportIR[] = [];
  for (const statement of module.sourceFile.statements) {
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier) ||
      !statement.importClause ||
      /^@usejaunt\/ts(?:\/spec)?$/.test(statement.moduleSpecifier.text)
    ) {
      continue;
    }
    const specifier = canonicalModuleSpecifier(
      module,
      root,
      statement.moduleSpecifier.text,
    );
    const bindings = statement.importClause.namedBindings;
    const importedLocalNames = [
      ...(statement.importClause.name
        ? [statement.importClause.name.text]
        : []),
      ...(bindings && compiler.isNamespaceImport(bindings)
        ? [bindings.name.text]
        : []),
      ...(bindings && compiler.isNamedImports(bindings)
        ? bindings.elements.map((element) => element.name.text)
        : []),
    ];
    typeImports.push({
      specifier,
      typeOnly: statement.importClause.isTypeOnly,
      runtime: importedLocalNames.some((name) => runtimeImportNames.has(name)),
      ...(statement.importClause.name
        ? { defaultImport: statement.importClause.name.text }
        : {}),
      ...(bindings && compiler.isNamespaceImport(bindings)
        ? { namespaceImport: bindings.name.text }
        : {}),
      namedImports:
        bindings && compiler.isNamedImports(bindings)
          ? bindings.elements.map((element) => ({
              imported: element.propertyName?.text ?? element.name.text,
              local: element.name.text,
              typeOnly:
                statement.importClause!.isTypeOnly || element.isTypeOnly,
            }))
          : [],
    });
  }
  const importIdentities = importIdentityContext(module, root, typeImports);
  const typeDeclarations: TypeDeclarationIR[] = typeDeclarationRecords.map(
    (record, index) => ({
      ...record,
      digest: typeDeclarationDigest(
        compiler,
        module.typeDeclarations[index]!,
        importIdentities,
      ),
    }),
  );
  const dependencies = module.symbols
    .flatMap((symbol) =>
      module.dependencyResolutionComplete
        ? symbol.resolvedDependencies
        : symbol.dependencies.map((name) => `${module.route.moduleId}#${name}`),
    )
    .filter((value, index, array) => array.indexOf(value) === index)
    .sort();
  const dependencyApiDigests = module.dependencyModules.map((dependency) => {
    let apiDigest = context.apiDigests.get(dependency.route.moduleId);
    if (apiDigest === undefined) {
      if (context.visiting.has(dependency.route.moduleId)) {
        // Discovery reports cycles as fatal. Keep direct IR construction total
        // and deterministic so diagnostics can still be inspected.
        apiDigest = digestCanonical({
          dependencyCycle: dependency.route.moduleId,
        });
      } else {
        const dependencyEnvironment = collectTypeEnvironment(
          compiler,
          workspaceRoot(dependency),
          dependency,
          dependency.compilerOptions,
        );
        apiDigest = buildContractIRInternal(
          compiler,
          dependency,
          dependencyEnvironment,
          context,
        ).apiDigest;
      }
      context.apiDigests.set(dependency.route.moduleId, apiDigest);
    }
    return { moduleId: dependency.route.moduleId, apiDigest };
  });
  const structuralSymbols = symbols.map((authoredSymbol) => {
    const { docs: _docs, ...symbol } = normalizedSymbol(
      authoredSymbol,
      importIdentities,
    );
    return {
      ...symbol,
      members: symbol.members.map(
        ({ preservedBody: _body, docs: _memberDocs, ...member }) => member,
      ),
    };
  });
  const structuralPayload = {
    symbols: structuralSymbols,
    typeDeclarations: typeDeclarations.map(
      ({ source: _source, docs: _docs, ...declaration }) => ({
        ...declaration,
        typeParameters: normalizedTypeParameters(
          declaration.typeParameters,
          importIdentities,
          new Set(),
        ),
      }),
    ),
    typeImports: structuralTypeImports(typeImports),
    typeEnvironmentDigest: typeEnvironment?.digest ?? digestCanonical([]),
    dependencyApiDigests,
  };
  const prosePayload = {
    symbols: symbols.map((symbol) => ({
      id: symbol.id,
      docs: symbol.docs,
      members: symbol.members.map((member) => ({
        kind: member.kind,
        name: member.name,
        static: member.static,
        docs: member.docs,
      })),
    })),
    typeEnvironmentProseDigest:
      typeEnvironment?.proseDigest ?? digestCanonical([]),
    typeDeclarations: typeDeclarations.map(({ name, docs }) => ({
      name,
      docs,
    })),
  };
  const structuralDigest = digestCanonical(structuralPayload);
  const proseDigest = digestCanonical(prosePayload);
  const result: ContractModuleIR = {
    schema: CONTRACT_IR_VERSION,
    moduleId: module.route.moduleId,
    specPath: module.route.specPath,
    facadePath: module.route.facadePath,
    apiMirrorPath: module.route.apiMirrorPath,
    implementationPath: module.route.implementationPath,
    ...(module.route.contextPath
      ? { contextPath: module.route.contextPath }
      : {}),
    project: module.route.project,
    packageOwner: module.route.packageOwner,
    symbols,
    options: resolvedOptions(
      module,
      module.moduleOptions,
      module.resolvedModuleDependencies,
    ),
    typeDeclarations,
    typeImports,
    contextDocs: typeEnvironment?.proseRecords ?? [],
    semanticEnvironmentDigest:
      typeEnvironment?.compatibilityDigest ?? digestCanonical([]),
    dependencies,
    structuralDigest,
    proseDigest,
    apiDigest: digestCanonical({ structuralPayload, prosePayload }),
  };
  context.visiting.delete(module.route.moduleId);
  context.apiDigests.set(module.route.moduleId, result.apiDigest);
  return result;
}

export function buildContractIR(
  compiler: typeof import("@typescript/typescript6"),
  module: DiscoveredModule,
  typeEnvironment?: TypeEnvironmentSnapshot,
): ContractModuleIR {
  return buildContractIRInternal(compiler, module, typeEnvironment, {
    apiDigests: new Map(),
    visiting: new Set(),
  });
}

export interface SidecarArtifacts {
  readonly state: "built" | "unbuilt";
  readonly artifactHashes: Readonly<Record<string, string>>;
}

export function renderSidecar(
  ir: ContractModuleIR,
  artifacts?: SidecarArtifacts,
): string {
  const payload = artifacts ? { ...ir, ...artifacts } : ir;
  return `${JSON.stringify(JSON.parse(canonicalJson(payload)), null, 2)}\n`;
}
