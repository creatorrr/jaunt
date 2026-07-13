import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, npm_config_audit: "false", npm_config_fund: "false" },
  });
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed (${result.status})\n${result.stdout}\n${result.stderr}`,
    );
  }
  return result.stdout;
}

async function filesUnder(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...(await filesUnder(path)));
    else found.push(path);
  }
  return found;
}

const scratch = await mkdtemp(join(tmpdir(), "jaunt-ts-preview-consumer-"));
try {
  const packOutput = run(
    "npm",
    ["pack", "--json", "--ignore-scripts", "--pack-destination", scratch],
    root,
  );
  const [packed] = JSON.parse(packOutput);
  const packedPaths = packed.files.map(({ path }) => path);

  for (const required of ["dist/tokens/index.js", "dist/tokens/index.d.ts"]) {
    assert(packedPaths.includes(required), `tarball is missing ${required}`);
  }
  const generatedLayout = packedPaths.includes("dist/tokens/__generated__/index.js");
  if (generatedLayout) {
    assert(
      packedPaths.includes("dist/tokens/__generated__/index.api.d.ts"),
      "generated tarball is missing its API declaration mirror",
    );
  } else {
    assert(
      packedPaths.every((path) => !path.startsWith("dist/tokens/__generated__/")),
      "ejected tarball retained a partial generated layout",
    );
  }
  assert(
    packedPaths.every(
      (path) =>
        !path.includes(".jaunt.ts") &&
        !path.includes(".jaunt-test.ts") &&
        !path.startsWith("tests/") &&
        !path.startsWith("src/"),
    ),
    `tarball contains a private source: ${packedPaths.join(", ")}`,
  );

  const tarball = join(scratch, packed.filename);
  const consumer = join(scratch, "consumer");
  await mkdir(consumer);
  await writeFile(
    join(consumer, "package.json"),
    `${JSON.stringify({ name: "jaunt-ts-preview-consumer", private: true, type: "module" }, null, 2)}\n`,
  );
  await writeFile(
    join(consumer, "tsconfig.json"),
    `${JSON.stringify(
      {
        compilerOptions: {
          module: "NodeNext",
          moduleResolution: "NodeNext",
          target: "ES2023",
          strict: true,
          outDir: "dist",
        },
        include: ["index.ts"],
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    join(consumer, "index.ts"),
    `import { TokenStore, createToken, verifyToken } from "jaunt-ts-preview/tokens";

const token = createToken("consumer", "secret", { ttlSeconds: 60 });
if (verifyToken(token, "secret").sub !== "consumer") throw new Error("JWT roundtrip failed");

const store: TokenStore = new TokenStore(() => 100);
store.put("consumer", token, 200);
if (store.get("consumer") !== token || store.size !== 1) throw new Error("store failed");
`,
  );

  run("npm", ["install", "--ignore-scripts", tarball], consumer);

  const installed = join(consumer, "node_modules", "jaunt-ts-preview");
  const installedPackage = JSON.parse(await readFile(join(installed, "package.json"), "utf8"));
  assert(!installedPackage.dependencies?.["@usejaunt/ts"]);

  for (const path of await filesUnder(join(installed, "dist"))) {
    if (!path.endsWith(".js") && !path.endsWith(".d.ts")) continue;
    const text = await readFile(path, "utf8");
    assert(!text.includes(".jaunt."), `${path} references a private spec`);
    assert(!text.includes("@usejaunt/ts"), `${path} retains a Jaunt runtime dependency`);
  }

  const tsc = join(root, "node_modules", "typescript", "bin", "tsc");
  run(process.execPath, [tsc, "-p", join(consumer, "tsconfig.json")], consumer);
  run(process.execPath, [join(consumer, "dist", "index.js")], consumer);
  process.stdout.write(`packed and executed ${packed.filename} in a clean consumer\n`);
} finally {
  await rm(scratch, { recursive: true, force: true });
}
