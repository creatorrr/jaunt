#!/usr/bin/env node
import { runWorker } from "../dist/worker/main.js";

process.on("message", (message) => {
  if (
    !message ||
    typeof message !== "object" ||
    message.method !== "collectGarbage" ||
    typeof message.id !== "number"
  ) {
    return;
  }
  if (typeof globalThis.gc !== "function") {
    process.send?.({ id: message.id, error: "global.gc is unavailable" });
    return;
  }
  globalThis.gc();
  process.send?.({
    id: message.id,
    memoryUsage: process.memoryUsage(),
    activeResources: process.getActiveResourcesInfo().sort(),
  });
});

await runWorker();
process.disconnect?.();
