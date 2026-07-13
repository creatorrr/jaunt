import { defineConfig } from "vitest/config";

// No jaunt plugin, no resolver: the facade architecture means generated code
// is reached through ordinary imports (see ../DESIGN.md, "Substitution").
export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
  },
});
