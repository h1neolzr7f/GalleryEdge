// GalleryEdge Cloudflare Worker: same routes/schema as the Python reference (galleryedge_edge/app.py).
import { Hrana, rowsToDicts, Value } from "./db";
import { DIALECTS, Dialect, fts5 } from "./dialect";
import { check, sha256Hex } from "./keys";

export interface Env {
  TURSO_URL: string; TURSO_TOKEN?: string; DEMO_BANNER?: string; DEBUG_ERRORS?: string;
  /** namespace for a shared DB, e.g. "ge_" */
  TABLE_PREFIX?: string;
  /** read-only image origin; /cover/<id> proxies GET <IMAGE_BASE>/img/<cover_key>?pack=<pack> */
  IMAGE_BASE?: string;
  /** banner text override (e.g. staging notice) */
  BANNER_TEXT?: string;
  /** optional service binding to the image worker (same-account workers.dev fetches are blocked by Cloudflare) */
  /** separate writable DB for API keys + usage counters (content DB token can then be read-only) */
  USAGE_URL?: string; USAGE_TOKEN?: string;
  IMAGES?: { fetch(input: string, init?: RequestInit): Promise<Response> };
}

const PAGE = 24;
const CSS = `*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e6e6}
a{color:#8ab4ff;text-decoration:none}header{padding:16px 24px;background:#161a22;border-bottom:1px solid #262c38;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header h1 small{color:#8a93a6;font-weight:400}form{display:flex;gap:8px;flex:1;min-width:280px}
input{flex:1;padding:8px 12px;border-radius:8px;border:1px solid #303848;background:#0f1115;color:#e6e6e6}
button{padding:8px 14px;border-radius:8px;border:0;background:#3b6cff;color:#fff;cursor:pointer}
main{padding:20px 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.card{background:#161a22;border:1px solid #262c38;border-radius:12px;overflow:hidden}.card img{width:100%;display:block;aspect-ratio:4/3}
.card .b{padding:10px 12px}.card h3{font-size:14px;margin:0 0 6px}.tag{display:inline-block;font-size:12px;padding:1px 8px;margin:2px 4px 2px 0;border-radius:99px;background:#232a38;color:#b9c4dd}
.meta{color:#8a93a6;font-size:12px}.banner{background:#2a2414;color:#e8d29a;padding:6px 24px;font-size:13px}
pre{white-space:pre-wrap;background:#161a22;border:1px solid #262c38;padding:12px;border-radius:8px}.pager{margin:20px 0;display:flex;gap:12px}`;

/** Only hotlink thumbnails from hosts whose policy allows it (with attribution). */
export const coverSrc = (w: any) => /^https:\/\/upload\.wikimedia\.org\/wikipedia\/commons\/thumb\//.test(String(w.cover_url ?? "")) ? String(w.cover_url) : `/cover/${w.id}.svg`;

/** tags_text is " | "-joined (multi-word tags); older rows were space-joined. */
export const splitTags = (t: unknown) => { const s = String(t ?? ""); return (s.includes("|") ? s.split("|") : s.split(" ")).map(x => x.trim()).filter(Boolean); };

export const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" }[c]!));
/** HTML-escaped attributes alone do not prevent javascript: URLs in untrusted records. */
export const safeHttpUrl = (s: unknown): string | null => {
  try {
    const url = new URL(String(s ?? ""));
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
};
const qs = (o: Record<string, string | number | undefined>) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)])).toString();

const SEC_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'self'; img-src 'self' https://upload.wikimedia.org; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
  "Referrer-Policy": "no-referrer",
};
const resp = (status: number, type: string, body: string, extra: Record<string, string> = {}) =>
  new Response(body, { status, headers: { "Content-Type": type, ...SEC_HEADERS, ...extra } });
const json = (status: number, obj: unknown, extra: Record<string, string> = {}) => resp(status, "application/json; charset=utf-8", JSON.stringify(obj), extra);
const htmlResp = (status: number, body: string) => resp(status, "text/html; charset=utf-8", body);

export async function coverSvg(id: string, title: string): Promise<string> {
  const h = (await sha256Hex(id)).match(/../g)!.map(x => parseInt(x, 16));
  const c1 = `hsl(${Math.floor(h[0] * 360 / 256)},55%,45%)`, c2 = `hsl(${Math.floor(h[1] * 360 / 256)},60%,25%)`;
  let shapes = "";
  for (let i = 0; i < 5; i++) shapes += `<circle cx="${Math.floor(h[2 + i] * 400 / 256)}" cy="${Math.floor(h[8 + i] * 300 / 256)}" r="${20 + h[14 + i] % 60}" fill="#fff" opacity="0.${1 + h[20 + i] % 3}"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs><rect width="400" height="300" fill="url(#g)"/>${shapes}<text x="16" y="284" fill="#fff" font-family="sans-serif" font-size="16" opacity="0.85">${esc(title)}</text></svg>`;
}

export class Site {
  constructor(private db: Hrana, private dialect: Dialect = fts5, private banner = true, private debug = false,
              private imageBase = "", private fetcher: typeof fetch = ((i: any, o?: any) => fetch(i, o)) as typeof fetch, private bannerText = "",
              private keysDb?: Hrana) {}

  static async create(env: Env, fetcher?: typeof fetch): Promise<Site> {
    const db = new Hrana(env.TURSO_URL, env.TURSO_TOKEN, fetcher, env.TABLE_PREFIX ?? "");
    let d = fts5;
    try { const r = await db.execute("SELECT v FROM meta WHERE k='fts_dialect'"); d = DIALECTS[String(r.rows[0]?.[0])] ?? fts5; } catch { /* default */ }
    return new Site(db, d, env.DEMO_BANNER !== "0", env.DEBUG_ERRORS === "1", (env.IMAGE_BASE ?? "").replace(/\/+$/, ""),
      env.IMAGES ? (((u: any, o?: any) => env.IMAGES!.fetch(u, o)) as typeof fetch) : fetcher, env.BANNER_TEXT ?? "",
      env.USAGE_URL ? new Hrana(env.USAGE_URL, env.USAGE_TOKEN, fetcher) : undefined);
  }

  async search(q = "", tag = "", artist = "", page = 1) {
    const where: string[] = [], args: Value[] = [];
    if (q.trim()) { const [s, a] = this.dialect.searchSql(q); where.push(`w.id IN (${s})`); args.push(...a); }
    if (tag) { where.push("w.id IN (SELECT work_id FROM work_tags WHERE tag=?)"); args.push(tag); }
    if (artist) { where.push("w.artist=?"); args.push(artist); }
    const w = where.length ? " WHERE " + where.join(" AND ") : "";
    page = Math.max(1, page);
    const total = Number((await this.db.execute(`SELECT count(*) FROM works w${w}`, args)).rows[0][0]);
    const items = rowsToDicts(await this.db.execute(`SELECT w.id,w.title,w.artist,w.tags_text,w.license,w.image_count,w.cover_url FROM works w${w} ORDER BY w.created_at DESC, w.id LIMIT ? OFFSET ?`, [...args, PAGE, (page - 1) * PAGE]))
      .map(({ tags_text, ...r }) => ({ ...r, tags: splitTags(tags_text) }));
    return { total, page, page_size: PAGE, items };
  }

  async work(id: string): Promise<Record<string, any> | null> {
    const r = rowsToDicts(await this.db.execute("SELECT * FROM works WHERE id=?", [id]));
    if (!r.length) return null;
    const { tags_text, synthetic, ...w } = r[0];
    return { ...w, tags: splitTags(tags_text), synthetic: Boolean(synthetic) };
  }

  async tags() {
    return rowsToDicts(await this.db.execute("SELECT tag, count(*) n FROM work_tags GROUP BY tag ORDER BY n DESC, tag LIMIT 200"));
  }

  async handle(req: Request): Promise<Response> {
    const url = new URL(req.url), p = url.pathname, g = (k: string) => url.searchParams.get(k) ?? "";
    try {
      if (req.method !== "GET" && req.method !== "HEAD") return resp(405, "text/plain", "method not allowed");
      if (p === "/healthz") { await this.db.execute("SELECT 1"); return json(200, { ok: true, fts: this.dialect.name }); }
      if (p.startsWith("/api/")) return await this.api(req, p, g);
      if (p === "/") return htmlResp(200, await this.index(g("q"), g("tag"), g("artist"), parseInt(g("page") || "1") || 1));
      let m = p.match(/^\/work\/([\w-]+)$/);
      if (m) {
        const w = await this.work(m[1]);
        return w ? htmlResp(200, this.workPage(w)) : htmlResp(404, this.layout("Not found", "<main><p>作品不存在 / not found</p></main>"));
      }
      m = p.match(/^\/cover\/([\w-]+)\.svg$/);
      if (m) {
        const w = await this.work(m[1]);
        if (!w) return resp(404, "text/plain", "not found");
        if (this.imageBase && w.cover_key && /^[\w./-]+$/.test(String(w.cover_key)) && !String(w.cover_key).includes("..")) {
          const pack = String(w.pack_id ?? "").replace(/^ge-/, "");
          const up = await this.fetcher(`${this.imageBase}/img/${w.cover_key}?${qs({ pack })}`, { method: "GET" });
          if (up.ok && /^image\//.test(up.headers.get("Content-Type") ?? "")) {
            return new Response(up.body, { status: 200, headers: { "Content-Type": up.headers.get("Content-Type")!, "Cache-Control": "public, max-age=86400", ...SEC_HEADERS } });
          }
        }
        return resp(200, "image/svg+xml", await coverSvg(String(w.id), String(w.title)), { "Cache-Control": "public, max-age=86400" });
      }
      return resp(404, "text/plain", "not found");
    } catch (e) {
      return resp(500, "text/plain", `internal error (${(e as Error).name})` + (this.debug ? `: ${(e as Error).message}` : ""));
    }
  }

  private async api(req: Request, p: string, g: (k: string) => string): Promise<Response> {
    const [status, reason, hdrs] = await check(this.keysDb ?? this.db, req.headers.get("X-API-Key"));
    if (status !== 200) return json(status, { error: reason }, hdrs);
    if (p === "/api/v1/search") {
      const page = g("page") ? Number(g("page")) : 1;
      if (!Number.isInteger(page)) return json(400, { error: "bad page" }, hdrs);
      return json(200, await this.search(g("q"), g("tag"), g("artist"), page), hdrs);
    }
    const m = p.match(/^\/api\/v1\/works\/([\w-]+)$/);
    if (m) { const w = await this.work(m[1]); return json(w ? 200 : 404, w ?? { error: "not found" }, hdrs); }
    if (p === "/api/v1/tags") return json(200, { items: await this.tags() }, hdrs);
    return json(404, { error: "unknown endpoint" }, hdrs);
  }

  layout(title: string, body: string, q = ""): string {
    const banner = this.bannerText ? `<div class="banner">${esc(this.bannerText)}</div>` : this.banner ? '<div class="banner">演示站：全部为合成数据 · Demo site — all data is synthetic</div>' : "";
    return `<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)} · GalleryEdge</title><style>${CSS}</style></head><body>${banner}<header><h1><a href="/">GalleryEdge</a> <small>边缘图阁</small></h1><form action="/"><input name="q" value="${esc(q)}" placeholder="搜索标题 / 提示词 / 标签 / 画师 · search"><button>搜索</button></form></header>${body}</body></html>`;
  }

  async index(q: string, tag: string, artist: string, page: number): Promise<string> {
    const res = await this.search(q, tag, artist, page);
    const cards = res.items.map((w: any) => `<a class="card" href="/work/${esc(w.id)}"><img src="${esc(coverSrc(w))}" alt="${esc(w.title)}" loading="lazy" referrerpolicy="no-referrer"><div class="b"><h3>${esc(w.title)}</h3><div class="meta">${esc(w.artist)} · ${esc(w.license)} · ${w.image_count} 张</div>${w.tags.map((t: string) => `<span class="tag">${esc(t)}</span>`).join("")}</div></a>`).join("");
    const filt = [tag ? `标签 tag: <b>${esc(tag)}</b>` : "", artist ? `画师 artist: <b>${esc(artist)}</b>` : ""].filter(Boolean).join(" · ");
    const tagbar = (await this.tags()).slice(0, 20).map((t: any) => `<a class="tag" href="/?${qs({ tag: t.tag })}">${esc(t.tag)} ${t.n}</a>`).join(" ");
    const pages = Math.max(1, Math.ceil(res.total / PAGE));
    const link = (p: number) => "/?" + qs({ q, tag, artist, page: p });
    const pager = `<div class="pager">${res.page > 1 ? `<a href="${link(res.page - 1)}">← 上一页</a>` : ""}<span class="meta">第 ${res.page}/${pages} 页 · 共 ${res.total} 件</span>${res.page < pages ? `<a href="${link(res.page + 1)}">下一页 →</a>` : ""}</div>`;
    return this.layout(q ? "搜索" : "首页", `<main><p>${tagbar}</p>${filt ? `<p>${filt}</p>` : ""}<div class="grid">${cards || "<p>没有结果 / no results</p>"}</div>${pager}</main>`, q);
  }

  workPage(w: any): string {
    const tags = w.tags.map((t: string) => `<a class="tag" href="/?${qs({ tag: t })}">${esc(t)}</a>`).join("");
    const license = safeHttpUrl(w.license_url);
    const source = safeHttpUrl(w.source_url);
    const licenseText = license ? `<a href="${esc(license)}" rel="license noopener">${esc(w.license)}</a>` : esc(w.license);
    const sourceText = source ? `<a href="${esc(source)}" rel="nofollow noopener">${esc(w.source_url)}</a>` : esc(w.source_url);
    return this.layout(String(w.title), `<main style="max-width:900px"><img src="${esc(coverSrc(w))}" alt="${esc(w.title)}" referrerpolicy="no-referrer" style="width:100%;max-width:480px;border-radius:12px"><h2>${esc(w.title)}</h2><p class="meta">作者 <a href="/?${qs({ artist: w.artist ?? "" })}">${esc(w.artist)}</a> · 许可 ${licenseText} · 来源 ${sourceText}</p>${w.attribution ? `<p class="attribution" id="attribution">署名 Attribution：${esc(w.attribution)}</p>` : ""}<p>${tags}</p><h3>${w.attribution ? "描述 Description" : "提示词 Prompt"}</h3><pre id="prompt">${esc(w.prompt)}</pre><button id="copy" onclick="navigator.clipboard.writeText(document.getElementById('prompt').innerText).then(()=>this.textContent='已复制 ✓')">${w.attribution ? "复制描述 Copy" : "复制提示词 Copy"}</button></main>`);
  }
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    return (await Site.create(env)).handle(req);
  },
};
