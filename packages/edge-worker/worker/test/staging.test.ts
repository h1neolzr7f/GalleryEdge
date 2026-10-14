import { describe, it, expect } from "vitest";
import { cjkSpace, fts5Query, prefixSql } from "../src/text";
import { Site } from "../src/index";
import { Hrana } from "../src/db";

describe("CJK search text (mirrors Python)", () => {
  it("spaces CJK and builds phrase queries", () => {
    expect(cjkSpace("原神胡桃 girl")).toBe("原 神 胡 桃 girl");
    expect(fts5Query("胡桃 girl")).toBe('("胡桃" OR "胡 桃") "girl"*');
    expect(fts5Query('AI绘图 "x')).toBe('"AI 绘 图" "x"*');
  });
});

describe("table prefix", () => {
  it("rewrites only GalleryEdge names", () => {
    expect(prefixSql("SELECT w.id FROM works_fts f JOIN works w WHERE works_fts MATCH ? AND x.workshop=1", "ge_"))
      .toBe("SELECT w.id FROM ge_works_fts f JOIN ge_works w WHERE ge_works_fts MATCH ? AND x.workshop=1");
    expect(() => prefixSql("SELECT 1", "Bad;")).toThrow();
  });
  it("is applied on the wire", async () => {
    let sent = "";
    const fake = (async (_u: any, o: any) => { sent = JSON.parse(o.body).requests[0].stmt.sql;
      return new Response(JSON.stringify({ results: [{ type: "ok", response: { type: "execute", result: { cols: [], rows: [] } } }, { type: "ok" }] })); }) as any;
    await new Hrana("libsql://x.turso.invalid", "FAKE_tok", fake, "ge_").execute("SELECT count(*) FROM works");
    expect(sent).toBe("SELECT count(*) FROM ge_works");
  });
});

describe("cover proxy (read-only image origin)", () => {
  const work = { id: "pixiv-1", title: "t", cover_key: "images/2022-10-09/1_1.webp", pack_id: "ge-aitag-nai-0037", tags_text: "", synthetic: 0 };
  const db = { execute: async () => ({ columns: Object.keys(work), rows: [Object.values(work)] }) } as any;
  it("proxies GET to IMAGE_BASE with the original pack id", async () => {
    const seen: string[] = [];
    const f = (async (u: string, o: any) => { seen.push(`${o.method} ${u}`); return new Response("WEBP", { headers: { "Content-Type": "image/webp" } }); }) as any;
    const r = await new Site(db, undefined, true, false, "https://img.example", f).handle(new Request("http://e/cover/pixiv-1.svg"));
    expect(r.headers.get("Content-Type")).toBe("image/webp");
    expect(seen).toEqual(["GET https://img.example/img/images/2022-10-09/1_1.webp?pack=aitag-nai-0037"]);
  });
  it("falls back to the svg placeholder when upstream fails", async () => {
    const f = (async () => new Response("nope", { status: 503 })) as any;
    const r = await new Site(db, undefined, true, false, "https://img.example", f).handle(new Request("http://e/cover/pixiv-1.svg"));
    expect(r.headers.get("Content-Type")).toBe("image/svg+xml");
  });
});

import { coverSrc } from "../src/index";
describe("attribution + hotlink", () => {
  it("only hotlinks Commons thumbnails", () => {
    expect(coverSrc({ id: "a", cover_url: "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/500px-X.jpg" })).toMatch(/^https:\/\/upload/);
    expect(coverSrc({ id: "a", cover_url: "https://evil.example/x.jpg" })).toBe("/cover/a.svg");
  });
  it("work page shows author, license link and attribution", () => {
    const html = new Site({} as any).workPage({ id: "wmc-x", title: "T", artist: "Ann", license: "CC BY 4.0", license_url: "https://creativecommons.org/licenses/by/4.0",
      source_url: "https://commons.wikimedia.org/wiki/File:X.jpg", attribution: "“T” by Ann, CC BY 4.0, via Wikimedia Commons", tags: [], prompt: "d",
      cover_url: "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/500px-X.jpg" });
    expect(html).toContain('rel="license noopener"');
    expect(html).toContain("署名 Attribution");
    expect(html).toContain("via Wikimedia Commons");
  });
});

describe("separate usage store", () => {
  it("keyed API checks keys/usage in keysDb, content stays on the read-only db", async () => {
    const contentCalls: string[] = [], keyCalls: string[] = [];
    const work = { id: "w1", title: "t", tags_text: "a | b", synthetic: 0 };
    const content = { execute: async (sql: string) => { contentCalls.push(sql); if (/INSERT|UPDATE|DELETE/.test(sql)) throw new Error("read-only");
      return { columns: Object.keys(work), rows: [Object.values(work)] }; }, batch: async () => { throw new Error("read-only"); } } as any;
    const { sha256Hex } = await import("../src/keys");
    const key = "ge_abcd1234_FAKEsecret";
    const h = await sha256Hex(key);
    const keys = { execute: async (sql: string) => { keyCalls.push(sql);
        if (sql.includes("FROM api_keys")) return { columns: [], rows: [[h, "active", 30, 1000]] };
        return { columns: [], rows: [] }; },
      batch: async (items: any[]) => { items.forEach(([s]) => keyCalls.push(s)); return []; } } as any;
    const site = new Site(content, undefined, true, false, "", undefined, "", keys);
    const r = await site.handle(new Request("http://e/api/v1/works/w1", { headers: { "X-API-Key": key } }));
    expect(r.status).toBe(200);
    expect(keyCalls.some(s => s.startsWith("INSERT INTO usage_daily"))).toBe(true);
    expect(contentCalls.some(s => /api_keys|usage_/.test(s))).toBe(false);
  });
});

describe("zh query (word OR char fallback)", () => {
  it("builds word-or-phrase clauses", async () => {
    const { fts5Query } = await import("../src/text");
    expect(fts5Query("蝴蝶")).toBe('("蝴蝶" OR "蝴 蝶")');
    expect(fts5Query("花")).toBe('"花"');
    expect(fts5Query("flower 猫")).toBe('"flower"* "猫"');
  });
});
