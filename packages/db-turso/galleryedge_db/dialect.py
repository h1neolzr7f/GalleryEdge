"""Full-text search dialects.

* ``Fts5Dialect`` — classic SQLite/libSQL FTS5 external-content table + triggers.
  Verified against local SQLite and ``sqld`` in CI.
* ``TursoFtsDialect`` — Turso's newer native index (``CREATE INDEX ... USING fts``).
  Only SQL generation is unit-tested; it must be probed on a real (empty) Turso DB
  before use (plan §16.2). ``detect()`` falls back to FTS5 when unsupported.
"""
from __future__ import annotations

import re


from .text import fts5_query  # noqa: E402,F401  (re-exported)


class Fts5Dialect:
    name = "fts5"

    def ddl(self) -> str:
        return """
CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(title, prompt, tags, artist, search_text, content='works', content_rowid='rowid', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS works_ai AFTER INSERT ON works BEGIN
  INSERT INTO works_fts(rowid,title,prompt,tags,artist,search_text) VALUES (new.rowid,new.title,new.prompt,new.tags_text,new.artist,new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS works_ad AFTER DELETE ON works BEGIN
  INSERT INTO works_fts(works_fts,rowid,title,prompt,tags,artist,search_text) VALUES ('delete',old.rowid,old.title,old.prompt,old.tags_text,old.artist,old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS works_au AFTER UPDATE ON works BEGIN
  INSERT INTO works_fts(works_fts,rowid,title,prompt,tags,artist,search_text) VALUES ('delete',old.rowid,old.title,old.prompt,old.tags_text,old.artist,old.search_text);
  INSERT INTO works_fts(rowid,title,prompt,tags,artist,search_text) VALUES (new.rowid,new.title,new.prompt,new.tags_text,new.artist,new.search_text);
END;
"""

    def search_sql(self, q: str) -> tuple[str, list]:
        return ("SELECT w.id FROM works_fts f JOIN works w ON w.rowid=f.rowid WHERE works_fts MATCH ? ORDER BY bm25(works_fts)", [fts5_query(q)])


class TursoFtsDialect:
    name = "turso-fts"

    def ddl(self) -> str:
        return "CREATE INDEX IF NOT EXISTS works_fts_idx ON works USING fts (title, prompt, tags_text, artist, search_text);"

    def search_sql(self, q: str) -> tuple[str, list]:
        from .text import cjk_space
        terms = cjk_space(" ".join(re.findall(r"[\w\-]+", q, flags=re.UNICODE)[:12]))
        return ("SELECT id FROM works WHERE fts_match(title, prompt, tags_text, artist, search_text, ?) ORDER BY fts_score(title, prompt, tags_text, artist, search_text, ?) DESC", [terms, terms])


DIALECTS = {d.name: d for d in (Fts5Dialect(), TursoFtsDialect())}


def detect(client, prefer: str = "fts5"):
    """Pick a dialect the server actually supports (probe with a throwaway table)."""
    order = [prefer] + [n for n in DIALECTS if n != prefer]
    for n in order:
        try:
            if n == "fts5":
                client.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _ge_probe_fts USING fts5(a)")
                client.execute("DROP TABLE _ge_probe_fts")
            else:
                client.execute("CREATE TABLE IF NOT EXISTS _ge_probe_t(a TEXT)")
                try:
                    client.execute("CREATE INDEX _ge_probe_i ON _ge_probe_t USING fts (a)")
                finally:
                    client.execute("DROP TABLE IF EXISTS _ge_probe_t")
            return DIALECTS[n]
        except Exception:
            continue
    raise RuntimeError("no supported full-text search dialect")
