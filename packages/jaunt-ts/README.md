# @usejaunt/ts

Early marker API for the TypeScript target of
[Jaunt](https://github.com/creatorrr/jaunt), a spec-driven code-generation
framework.

This alpha provides the statically typed `magicModule`, `magic`, and `testSpec`
markers used by Jaunt's TypeScript analyzer. Spec modules are private analysis
inputs: importing one at runtime throws `JauntNotBuiltError` with an actionable
message.

```ts
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Convert a title to a stable URL slug. */
export function slugify(title: string): string {
  return jaunt.magic();
}
```

The analyzer and build integration are under active development. Use the
`next` dist-tag until the TypeScript target reaches a stable release.
