import { existsSync } from "node:fs";
import { posix, win32 } from "node:path";

/** Resolve npm without asking a shell to interpret npm.cmd on Windows. */
export function npmCliInvocation({
  platform = process.platform,
  environment = process.env,
  nodePath = process.execPath,
  fileExists = existsSync,
} = {}) {
  const configured = environment.npm_execpath;
  if (typeof configured === "string" && configured.trim() !== "") {
    return { command: nodePath, args: [configured] };
  }

  const paths = platform === "win32" ? win32 : posix;
  const nodeDirectory = paths.dirname(nodePath);
  const candidates =
    platform === "win32"
      ? [paths.join(nodeDirectory, "node_modules/npm/bin/npm-cli.js")]
      : [
          paths.join(nodeDirectory, "../lib/node_modules/npm/bin/npm-cli.js"),
          paths.join(nodeDirectory, "node_modules/npm/bin/npm-cli.js"),
        ];
  const npmCli = candidates.find((candidate) => fileExists(candidate));
  if (npmCli) return { command: nodePath, args: [npmCli] };

  if (platform === "win32") {
    throw new Error(
      "Could not locate npm-cli.js. Run this check through `npm run pack:check`.",
    );
  }
  return { command: "npm", args: [] };
}
