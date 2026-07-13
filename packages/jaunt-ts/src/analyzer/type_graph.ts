import { digestCanonical } from "./canonical.js";

export interface TypeGraphNode {
  readonly id: string;
  readonly payload: unknown;
  readonly references: readonly string[];
}

export interface TypeGraphComponent {
  readonly id: string;
  readonly members: readonly string[];
  readonly outgoing: readonly string[];
  readonly digest: string;
}

export function merkleTypeGraph(
  nodes: readonly TypeGraphNode[],
): readonly TypeGraphComponent[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let index = 0;
  const stack: string[] = [];
  const onStack = new Set<string>();
  const indices = new Map<string, number>();
  const low = new Map<string, number>();
  const groups: string[][] = [];

  function visit(id: string): void {
    indices.set(id, index);
    low.set(id, index);
    index += 1;
    stack.push(id);
    onStack.add(id);
    for (const reference of byId.get(id)?.references ?? []) {
      if (!byId.has(reference)) continue;
      if (!indices.has(reference)) {
        visit(reference);
        low.set(id, Math.min(low.get(id) ?? 0, low.get(reference) ?? 0));
      } else if (onStack.has(reference)) {
        low.set(id, Math.min(low.get(id) ?? 0, indices.get(reference) ?? 0));
      }
    }
    if (low.get(id) === indices.get(id)) {
      const component: string[] = [];
      while (stack.length > 0) {
        const member = stack.pop();
        if (member === undefined) break;
        onStack.delete(member);
        component.push(member);
        if (member === id) break;
      }
      groups.push(component.sort());
    }
  }

  for (const id of [...byId.keys()].sort()) if (!indices.has(id)) visit(id);
  const componentFor = new Map<string, string>();
  groups.forEach((members, position) =>
    members.forEach((member) => componentFor.set(member, `scc:${position}`)),
  );
  const pending = groups.map((members, position) => ({
    id: `scc:${position}`,
    members,
  }));
  const digests = new Map<string, string>();
  const output: TypeGraphComponent[] = [];

  function digestComponent(id: string): string {
    const existing = digests.get(id);
    if (existing) return existing;
    const component = pending.find((item) => item.id === id);
    if (!component) return digestCanonical({ external: id });
    const outgoing = new Set<string>();
    for (const member of component.members) {
      for (const reference of byId.get(member)?.references ?? []) {
        const target = componentFor.get(reference) ?? `external:${reference}`;
        if (target !== id) outgoing.add(target);
      }
    }
    const outbound = [...outgoing]
      .sort()
      .map((target) => [target, digestComponent(target)]);
    const digest = digestCanonical({
      members: component.members.map((member) => ({
        id: member,
        payload: byId.get(member)?.payload,
      })),
      outbound,
    });
    digests.set(id, digest);
    return digest;
  }

  for (const component of pending) {
    const outgoing = new Set<string>();
    for (const member of component.members) {
      for (const reference of byId.get(member)?.references ?? []) {
        const target = componentFor.get(reference) ?? `external:${reference}`;
        if (target !== component.id) outgoing.add(target);
      }
    }
    output.push({
      id: component.id,
      members: component.members,
      outgoing: [...outgoing].sort(),
      digest: digestComponent(component.id),
    });
  }
  return output.sort((left, right) => left.id.localeCompare(right.id));
}
