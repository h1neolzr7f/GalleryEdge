// Runs the real Worker bundle inside workerd (wrangler's local runtime / miniflare). No deploy.
import { describe, it, expect, inject, beforeAll, afterAll } from "vitest";
import { unstable_dev, type Unstable_DevWorker } from "wrangler";

const url = inject("url") as string;
const key = inject("key") as string;
let w: Unstable_DevWorker;

beforeAll(async () => {
  w = await unstable_dev("src/index.ts", {
    config: "wrangler.toml.example",
    vars: { TURSO_URL: url, DEMO_BANNER: "1", DEBUG_ERRORS: "1" },
    experimental: { disableExperimentalWarning: true },
    local: true, logLevel: "error",
  } as any);
}, 60000);
afterAll(async () => { await w?.stop(); });

describe("worker in workerd", () => {
  it("healthz reaches libSQL from inside workerd", async () => {
    const r = await w.fetch("/healthz");
    expect(await r.json()).toEqual({ ok: true, fts: "fts5" });
  });
  it("renders pages and serves keyed API", async () => {
    expect(await (await w.fetch("/")).text()).toContain('class="card"');
    expect((await w.fetch("/api/v1/search")).status).toBe(401);
    const r = await w.fetch("/api/v1/search?q=harbor", { headers: { "X-API-Key": key } });
    expect(r.status).toBe(200);
    expect((await r.json() as any).items).toBeInstanceOf(Array);
    expect((await w.fetch("/cover/demo-000001.svg")).headers.get("Content-Type")).toBe("image/svg+xml");
  });
});
