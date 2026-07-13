import { slugify } from "./index.js";

const actual = slugify(" Hello, Jaunt TS! ");
if (actual !== "hello-jaunt-ts") {
  throw new Error(`unexpected slug: ${actual}`);
}

process.stdout.write(`${actual}\n`);
