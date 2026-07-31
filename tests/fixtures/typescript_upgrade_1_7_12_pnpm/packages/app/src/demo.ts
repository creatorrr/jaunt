import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";
import { slugify } from "./slug/index.js";

const title = "  Project\tReferences!  ";
const normalized = normalizeSpacing(title);
const slug = slugify(title);

if (normalized !== "Project References!") {
  throw new Error(`unexpected normalized title: ${normalized}`);
}
if (slug !== "project-references") {
  throw new Error(`unexpected slug: ${slug}`);
}

process.stdout.write(`${normalized} -> ${slug}\n`);
