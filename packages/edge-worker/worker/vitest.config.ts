import { defineConfig } from "vitest/config";
export default defineConfig({ test: { globalSetup: "./test/setup.ts", testTimeout: 60000, hookTimeout: 60000 } });
