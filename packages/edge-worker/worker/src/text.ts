// Mirrors galleryedge_db/text.py — CJK characters become single tokens so FTS5 unicode61 can match Chinese.
const CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af";
const CJK_RE = new RegExp(`([${CJK}])`, "gu");
const HAS_CJK = new RegExp(`[${CJK}]`, "u");

export const cjkSpace = (s: string | null | undefined) => String(s ?? "").replace(CJK_RE, " $1 ").replace(/\s+/g, " ").trim();
export const terms = (q: string) => (q.match(/[\p{L}\p{N}_\-]+/gu) ?? []).slice(0, 12);

const CJK_RUN = new RegExp(`^[${CJK}]+$`, "u");

/** Mirrors fts5_query(segment=True) without a dictionary: a pure-CJK term matches as a whole word token
 *  (indexed by jieba at ingest) OR as the single-character phrase fallback; bm25 ranks word hits higher. */
export function fts5Query(q: string): string {
  return terms(q).map(t => {
    t = t.replace(/"/g, "");
    const sp = cjkSpace(t);
    if (CJK_RUN.test(t)) return [...t].length === 1 ? `"${sp}"` : `("${t}" OR "${sp}")`;
    return sp.includes(" ") || HAS_CJK.test(t) ? `"${sp}"` : `"${t}"*`;
  }).join(" ");
}

const GE_TABLES = ["works_fts_idx", "works_fts", "works_ai", "works_ad", "works_au", "works", "work_tags", "idx_work_tags_tag", "idx_works_artist",
  "api_plans", "api_keys", "usage_daily", "usage_minute", "meta",
  "quota_daily_insert", "quota_daily_update", "quota_minute_insert", "quota_minute_update"];
const PREFIX_RE = new RegExp(`\\b(${GE_TABLES.join("|")})\\b`, "g");

/** Same rewrite as galleryedge_db.client.prefix_sql: lets GalleryEdge share a DB under e.g. ge_*. */
export function prefixSql(sql: string, prefix: string): string {
  if (!prefix) return sql;
  if (!/^[a-z][a-z0-9_]{0,15}_$/.test(prefix)) throw new Error("table prefix must look like 'ge_'");
  return sql.replace(PREFIX_RE, m => prefix + m);
}
