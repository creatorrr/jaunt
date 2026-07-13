import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import ts from "@typescript/typescript6";

const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "..");
const repoRoot = resolve(packageRoot, "../..");
const dist = resolve(packageRoot, "dist");

await rm(dist, { recursive: true, force: true });

const configPath = resolve(packageRoot, "tsconfig.json");
const configFile = ts.readConfigFile(configPath, ts.sys.readFile);
if (configFile.error) {
  throw new Error(
    ts.flattenDiagnosticMessageText(configFile.error.messageText, "\n"),
  );
}
const parsed = ts.parseJsonConfigFileContent(
  configFile.config,
  ts.sys,
  packageRoot,
  {},
  configPath,
);
const program = ts.createProgram({
  rootNames: parsed.fileNames,
  options: parsed.options,
});
const result = program.emit();
const diagnostics = ts
  .getPreEmitDiagnostics(program)
  .concat(result.diagnostics);
if (diagnostics.length > 0 || result.emitSkipped) {
  throw new Error(
    ts.formatDiagnosticsWithColorAndContext(diagnostics, {
      getCanonicalFileName: (name) => name,
      getCurrentDirectory: () => packageRoot,
      getNewLine: () => "\n",
    }),
  );
}

await build({
  entryPoints: [resolve(packageRoot, "src/spec.ts")],
  outfile: resolve(dist, "spec.cjs"),
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  sourcemap: true,
});

const esmDeclaration = await readFile(resolve(dist, "spec.d.ts"), "utf8");
await writeFile(
  resolve(dist, "spec.d.cts"),
  esmDeclaration.replace(/\n?\/\/# sourceMappingURL=.*\n?$/, "\n"),
);

await mkdir(resolve(dist, "schema"), { recursive: true });
await cp(
  resolve(repoRoot, "schemas/jaunt-ts/protocol-v1.schema.json"),
  resolve(dist, "schema/protocol-v1.schema.json"),
);
await cp(
  resolve(repoRoot, "schemas/jaunt-ts/contract-ir-v1.schema.json"),
  resolve(dist, "schema/contract-ir-v1.schema.json"),
);
await cp(
  resolve(packageRoot, "src/test/permission_guard.cjs"),
  resolve(dist, "test/permission_guard.cjs"),
);
