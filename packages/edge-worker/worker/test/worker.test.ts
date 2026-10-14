import { describe, it, expect, inject, beforeAll } from "vitest";
import { execFileSync } from "node:child_process";
import { join, resolve } from "node:path";
import worker, { Site, esc } from "../src/index";
import { Hrana, DBError } from "../src/db";
import { sha256Hex, check } from "../src/keys";

const url = inject("url") as string;
const key = inject("key") as string;
const env = { TURSO_URL: url };
const get = (path: string, headers: Record<string, string> = {}, method = "GET") =>
  worker.fetch(new Request("http://edge.test" + path, { method, headers }), env as any);

const root = resolve(__dirname, "../../../..");
function pyApi(code: string): any {
  const envp = { ...process.env, PYTHONPATH: ["packages/panel", "packages/demo-data", "packages/db-turso", "packages/edge-worker"].map(p => join(root, p)).join(":") };
  const src = `import json,sys\nfrom galleryedge_db.client import connect\nfrom galleryedge_edge.app import App\napp=App(connect(${JSON.stringify(url)}))\nprint(json.dumps(${code}))`;
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", src], { cwd: root, env: envp }).toString());
}

async function newKey(db: Hrana, plan = "demo"): Promise<string> {
  const prefix = Math.random().toString(16).slice(2, 10);
  const k = `ge_${prefix}_testsecret${Math.random().toString(36).slice(2)}`;
  await db.execute("INSERT INTO api_keys(prefix,hash,plan,label) VALUES (?,?,?,?)", [prefix, await sha256Hex(k), plan, "vitest"]);
  return k;
}

describe("Hrana client", () => {
  const db = new Hrana(url);
  it("roundtrips types", async () => {
    await db.execute("CREATE TABLE IF NOT EXISTS ts_types(i INTEGER, f REAL, s TEXT, b BLOB, n)");
    await db.execute("DELETE FROM ts_types");
    await db.execute("INSERT INTO ts_types VALUES (?,?,?,?,?)", [7, 1.5, "中文 ok", new Uint8Array([0, 1, 255]), null]);
    const r = await db.execute("SELECT * FROM ts_types");
    expect(r.columns).toEqual(["i", "f", "s", "b", "n"]);
    expect(r.rows[0].slice(0, 3)).toEqual([7, 1.5, "中文 ok"]);
    expect(Array.from(r.rows[0][3] as Uint8Array)).toEqual([0, 1, 255]);
  });
  it("batch rolls back on failure", async () => {
    await db.execute("CREATE TABLE IF NOT EXISTS ts_tx(a INTEGER PRIMARY KEY)");
    await db.execute("DELETE FROM ts_tx");
    await expect(db.batch([["INSERT INTO ts_tx VALUES (1)", []], ["INSERT INTO ts_tx VALUES (1)", []]])).rejects.toThrow(DBError);
    expect((await db.execute("SELECT count(*) FROM ts_tx")).rows[0][0]).toBe(0);
  });
  it("sends bearer token to https for libsql:// and hides it", async () => {
    let seen: any;
    const fake = (async (u: string, init: any) => { seen = { u, auth: init.headers.Authorization }; return new Response(JSON.stringify({ results: [{ type: "ok", response: { type: "execute", result: { cols: [], rows: [] } } }, { type: "ok", response: { type: "close" } }] })); }) as any;
    const c = new Hrana("libsql://db-x.turso.invalid", "FAKE_tok", fake);
    await c.execute("SELECT 1");
    expect(seen).toEqual({ u: "https://db-x.turso.invalid/v2/pipeline", auth: "Bearer FAKE_tok" });
    expect(String(c)).not.toContain("FAKE_tok");
  });
  it("reports unreachable server", async () => {
    await expect(new Hrana("http://127.0.0.1:1").execute("SELECT 1")).rejects.toThrow("unreachable");
  });
});

describe("pages", () => {
  it("healthz", async () => { expect(await (await get("/healthz")).json()).toEqual({ ok: true, fts: "fts5" }); });
  it("index shows 24 cards + banner + CSP", async () => {
    const r = await get("/");
    const b = await r.text();
    expect(r.status).toBe(200);
    expect(b.match(/class="card"/g)!.length).toBe(24);
    expect(b).toContain("合成数据");
    expect(r.headers.get("Content-Security-Policy")).toContain("default-src 'self'");
  });
  it("work page with prompt copy + cover + 404", async () => {
    const b = await (await get("/work/demo-000001")).text();
    expect(b).toContain('id="prompt"');
    expect(b).toContain("navigator.clipboard.writeText");
    const c = await get("/cover/demo-000001.svg");
    expect(c.headers.get("Content-Type")).toBe("image/svg+xml");
    expect((await c.text()).startsWith("<svg")).toBe(true);
    expect((await get("/work/nope")).status).toBe(404);
    expect((await get("/", {}, "POST")).status).toBe(405);
  });
  it("escapes html", async () => {
    expect(esc('<script>"x"</script>')).toBe("&lt;script&gt;&quot;x&quot;&lt;/script&gt;");
    expect(await (await get('/?q="><img src=x>')).text()).not.toContain('"><img');
  });
  it("cover SVG is byte-identical to the Python reference", async () => {
    const ts = await (await get("/cover/demo-000003.svg")).text();
    const py = pyApi("__import__('galleryedge_edge.app',fromlist=['x']).cover_svg('demo-000003', app.work('demo-000003')['title'])");
    expect(ts).toBe(py);
  });
});

describe("API parity with the Python reference", () => {
  it.each([["", "", ""], ["harbor", "", ""], ["", "night", ""], ["", "", "demo-artist-05"], ["lunar", "city", ""]])("search q=%s tag=%s artist=%s", async (q, tag, artist) => {
    const r = await get(`/api/v1/search?${new URLSearchParams({ q, tag, artist })}`, { "X-API-Key": key });
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual(pyApi(`app.search(${JSON.stringify(q)}, ${JSON.stringify(tag)}, ${JSON.stringify(artist)})`));
  });
  it("work + tags", async () => {
    expect(await (await get("/api/v1/works/demo-000002", { "X-API-Key": key })).json()).toEqual(pyApi("app.work('demo-000002')"));
    expect((await (await get("/api/v1/tags", { "X-API-Key": key })).json()).items).toEqual(pyApi("app.tags()"));
  });
});

describe("keys and quota", () => {
  const db = new Hrana(url);
  beforeAll(async () => { await db.execute("INSERT OR IGNORE INTO api_plans VALUES ('ts-tiny', 2, 3)"); });
  it("rejects missing/invalid, revoked", async () => {
    expect((await get("/api/v1/search")).status).toBe(401);
    expect((await get("/api/v1/search", { "X-API-Key": "ge_bad_key" })).status).toBe(401);
    const k = await newKey(db);
    expect((await get("/api/v1/search?page=x", { "X-API-Key": k })).status).toBe(400);
    expect((await get("/api/v1/works/zzz", { "X-API-Key": k })).status).toBe(404);
    await db.execute("UPDATE api_keys SET status='revoked' WHERE prefix=?", [k.split("_")[1]]);
    expect((await get("/api/v1/search", { "X-API-Key": k })).status).toBe(403);
  });
  it("Python-issued key works (same hash scheme)", async () => {
    const r = await get("/api/v1/tags", { "X-API-Key": key });
    expect(r.status).toBe(200);
    expect(r.headers.get("X-RateLimit-Remaining-Day")).toMatch(/^\d+$/);
  });
  it("minute and daily limits", async () => {
    const k = await newKey(db, "ts-tiny");
    const t = new Date(Date.UTC(2026, 0, 1, 10, 0, 5));
    expect((await check(db, k, t))[0]).toBe(200);
    expect((await check(db, k, t))[0]).toBe(200);
    const [s, why, h] = await check(db, k, t);
    expect([s, why, h["Retry-After"]]).toEqual([429, "rate limit exceeded", "55"]);
    expect((await check(db, k, new Date(t.getTime() + 60e3)))[0]).toBe(200);
    expect((await check(db, k, new Date(t.getTime() + 120e3)))[1]).toBe("daily quota exceeded");
    expect((await check(db, k, new Date(t.getTime() + 86400e3)))[0]).toBe(200);
  });
});

describe("dialect", () => {
  it("Site.create reads dialect from meta", async () => {
    const s = await Site.create(env as any);
    expect((await s.search("qqqnoresult")).total).toBe(0);
  });
});
