import type {
  ClassMemberIR,
  ContractModuleIR,
  ParameterIR,
  SymbolIR,
  TypeIR,
  TypeParameterIR,
} from "./ir.js";
import { renderType } from "./mirror.js";

function specType(ir: ContractModuleIR, type: TypeIR): string {
  let rendered = renderType(type);
  const localNames = [
    ...ir.typeDeclarations.map((declaration) => declaration.name),
    ...ir.symbols
      .filter((symbol) => symbol.kind === "class")
      .map((symbol) => symbol.name),
  ];
  for (const name of localNames.sort(
    (left, right) => right.length - left.length,
  )) {
    rendered = rendered.replace(
      new RegExp(`(?<![.#\\w])${name}(?![\\w])`, "g"),
      `__JauntSpec.${name}`,
    );
  }
  return rendered;
}

function typeParameters(
  ir: ContractModuleIR,
  parameters: readonly TypeParameterIR[],
): string {
  if (parameters.length === 0) return "";
  return `<${parameters
    .map(
      (parameter) =>
        `${parameter.name}${parameter.constraint ? ` extends ${specType(ir, parameter.constraint)}` : ""}${parameter.default ? ` = ${specType(ir, parameter.default)}` : ""}`,
    )
    .join(", ")}>`;
}

function typeArguments(parameters: readonly TypeParameterIR[]): string {
  return parameters.length
    ? `<${parameters.map((parameter) => parameter.name).join(", ")}>`
    : "";
}

function renderParameter(ir: ContractModuleIR, parameter: ParameterIR): string {
  return `${parameter.rest ? "..." : ""}${parameter.name}${parameter.optional ? "?" : ""}: ${specType(ir, parameter.type)}`;
}

function parameterList(ir: ContractModuleIR, member: ClassMemberIR): string {
  return (member.signatures[0]?.parameters ?? [])
    .filter((parameter) => parameter.name !== "this")
    .map((parameter) => renderParameter(ir, parameter))
    .join(", ");
}

function argumentsFor(member: ClassMemberIR): string {
  return (member.signatures[0]?.parameters ?? [])
    .filter((parameter) => parameter.name !== "this")
    .map((parameter) => `${parameter.rest ? "..." : ""}${parameter.name}`)
    .join(", ");
}

function memberAdapters(ir: ContractModuleIR, symbol: SymbolIR): string[] {
  const classParameters = symbol.typeParameters;
  const classArguments = typeArguments(classParameters);
  const impl = `__jaunt_impl_${symbol.name}${classArguments}`;
  const authored = `__JauntSpec.${symbol.name}${classArguments}`;
  const lines: string[] = [];
  symbol.members.forEach((member, index) => {
    const signature = member.signatures[0];
    const suffix = `${symbol.name}_${member.name}_${index}`.replace(
      /[^A-Za-z0-9_]/g,
      "_",
    );
    if (member.kind === "constructor") {
      const generics = typeParameters(ir, classParameters);
      if (member.inheritedConstructor) {
        const inherited = `ConstructorParameters<typeof __JauntSpec.${symbol.name}${classArguments}>`;
        lines.push(
          `function __jaunt_check_${suffix}${generics}(...args: ${inherited}): ${authored} { return new __jaunt_impl_${symbol.name}${classArguments}(...args); }`,
        );
        return;
      }
      lines.push(
        `function __jaunt_check_${suffix}${generics}(${parameterList(ir, member)}): ${authored} { return new __jaunt_impl_${symbol.name}${classArguments}(${argumentsFor(member)}); }`,
      );
      return;
    }
    if (member.kind === "property") {
      const baseType = specType(ir, member.type ?? { kind: "unknown" });
      const type = member.optional ? `${baseType} | undefined` : baseType;
      const target = member.static ? `__jaunt_impl_${symbol.name}` : "impl";
      const generics = member.static ? "" : typeParameters(ir, classParameters);
      const receiver = member.static ? "" : `impl: ${impl}`;
      lines.push(
        `function __jaunt_check_read_${suffix}${generics}(${receiver}): ${type} { return ${target}.${member.name}; }`,
      );
      if (!member.readonly) {
        const comma = receiver ? `${receiver}, ` : "";
        lines.push(
          `function __jaunt_check_write_${suffix}${generics}(${comma}value: ${baseType}): void { ${target}.${member.name} = value; }`,
        );
      }
      return;
    }
    const target = member.static ? `__jaunt_impl_${symbol.name}` : "impl";
    const receiver = member.static ? "" : `impl: ${impl}`;
    const classGenerics = member.static ? [] : classParameters;
    if (member.kind === "getter") {
      const returnType = specType(
        ir,
        signature?.returnType ?? { kind: "unknown" },
      );
      lines.push(
        `function __jaunt_check_${suffix}${typeParameters(ir, classGenerics)}(${receiver}): ${returnType} { return ${target}.${member.name}; }`,
      );
      return;
    }
    if (member.kind === "setter") {
      const parameter = signature?.parameters[0];
      const type = specType(ir, parameter?.type ?? { kind: "unknown" });
      const comma = receiver ? `${receiver}, ` : "";
      lines.push(
        `function __jaunt_check_${suffix}${typeParameters(ir, classGenerics)}(${comma}value: ${type}): void { ${target}.${member.name} = value; }`,
      );
      return;
    }
    const returnType = specType(
      ir,
      signature?.returnType ?? { kind: "unknown" },
    );
    const parameters = parameterList(ir, member);
    const separator = receiver && parameters ? ", " : "";
    const memberParameters = signature?.typeParameters ?? [];
    const allParameters = [...classGenerics, ...memberParameters];
    const methodArguments = typeArguments(memberParameters);
    const args = argumentsFor(member);
    const invocation = `${target}.${member.name}${methodArguments}(${args})`;
    const body = member.optional
      ? `if (${target}.${member.name} === undefined) return undefined; return ${invocation};`
      : `return ${invocation};`;
    lines.push(
      `function __jaunt_check_${suffix}${typeParameters(ir, allParameters)}(${receiver}${separator}${parameters}): ${returnType}${member.optional ? " | undefined" : ""} { ${body} }`,
    );
  });
  lines.push(
    `function __jaunt_check_instance_${symbol.name}${typeParameters(ir, classParameters)}(impl: ${impl}): ${authored} { return impl; }`,
  );
  return lines;
}

export function renderClassConformance(
  ir: ContractModuleIR,
  symbol: SymbolIR,
): string {
  return memberAdapters(ir, symbol).join("\n");
}
