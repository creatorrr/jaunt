import { slugify } from "@jaunt-examples/app/slug/index.js";
import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";

const normalizeContract: (value: string) => string = normalizeSpacing;
const slugContract: (value: string) => string = slugify;

void normalizeContract;
void slugContract;
