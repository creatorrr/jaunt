import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule({ inferDeps: true });

export function slugify(title: string): string {
  return jaunt.magic({ deps: [title] });
}

export function testSlugify(): never {
  return jaunt.testSpec({ targets: [slugify] });
}
