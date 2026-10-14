// Mirrors galleryedge_db/dialect.py
import { cjkSpace, fts5Query, terms } from "./text";

export interface Dialect { name: string; searchSql(q: string): [string, string[]] }

export const fts5: Dialect = {
  name: "fts5",
  searchSql: q => ["SELECT w.id FROM works_fts f JOIN works w ON w.rowid=f.rowid WHERE works_fts MATCH ? ORDER BY bm25(works_fts)", [fts5Query(q)]],
};

export const tursoFts: Dialect = {
  name: "turso-fts",
  searchSql: q => { const t = cjkSpace(terms(q).join(" ")); return ["SELECT id FROM works WHERE fts_match(title, prompt, tags_text, artist, search_text, ?) ORDER BY fts_score(title, prompt, tags_text, artist, search_text, ?) DESC", [t, t]]; },
};

export const DIALECTS: Record<string, Dialect> = { fts5, "turso-fts": tursoFts };
