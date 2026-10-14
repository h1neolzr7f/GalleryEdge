// Mirrors galleryedge_db/dialect.py
const terms = (q: string) => (q.match(/[\p{L}\p{N}_\-]+/gu) ?? []).slice(0, 12);

export interface Dialect { name: string; searchSql(q: string): [string, string[]] }

export const fts5: Dialect = {
  name: "fts5",
  searchSql: q => ["SELECT w.id FROM works_fts f JOIN works w ON w.rowid=f.rowid WHERE works_fts MATCH ? ORDER BY bm25(works_fts)",
    [terms(q).map(t => `"${t.replace(/"/g, "")}"*`).join(" ")]],
};

export const tursoFts: Dialect = {
  name: "turso-fts",
  searchSql: q => { const t = terms(q).join(" "); return ["SELECT id FROM works WHERE fts_match(title, prompt, tags_text, artist, ?) ORDER BY fts_score(title, prompt, tags_text, artist, ?) DESC", [t, t]]; },
};

export const DIALECTS: Record<string, Dialect> = { fts5, "turso-fts": tursoFts };
