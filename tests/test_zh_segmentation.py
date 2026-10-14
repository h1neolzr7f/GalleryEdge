import sqlite3
import sys
from pathlib import Path

import pytest

from galleryedge_db import text as T

jieba = pytest.importorskip("jieba")


def test_query_forms():
    assert T.fts5_query("花") == '"花"'
    assert T.fts5_query("蝴蝶") == '("蝴蝶" OR "蝴 蝶")'
    assert T.fts5_query("红色蝴蝶") == '(("红色" AND "蝴蝶") OR "红 色 蝴 蝶")'
    assert T.fts5_query("蝴蝶", segment=False) == '"蝴 蝶"'
    assert T.fts5_query("flower 猫") == '"flower"* "猫"'


def test_index_has_words_and_chars():
    s = T.search_text({"title": "温和服务的咖啡店"})
    toks = s.split()
    assert "和" in toks and "服" in toks and "服务" in toks and "和服" not in toks


def test_single_char_fallback_still_matches_without_word():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE f USING fts5(s)")
    c.execute("INSERT INTO f VALUES (?)", (T.search_text({"title": "立秋天气"}),))
    # "秋天" is a jieba word here, "天气" too; a query jieba would never produce still works via chars
    assert c.execute("SELECT count(*) FROM f WHERE f MATCH ?", (T.fts5_query("秋天气"),)).fetchone()[0] == 1


def test_word_hits_rank_above_boundary_crossing():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE f USING fts5(id UNINDEXED, s)")
    for i, t in (("bad", "温和服务的咖啡店"), ("good", "和服少女在京都散步")):
        c.execute("INSERT INTO f VALUES (?,?)", (i, T.search_text({"title": t})))
    got = [r[0] for r in c.execute("SELECT id FROM f WHERE f MATCH ? ORDER BY bm25(f)", (T.fts5_query("和服"),))]
    assert got == ["good", "bad"]  # recall kept, relevance improved


def test_relevance_tool_improves_and_keeps_recall():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import zh_relevance as Z
    a, b = Z.run(False), Z.run(True)
    assert b["recall"] == a["recall"] == 1.0
    assert b["R-precision"] > a["R-precision"] and b["MRR"] >= a["MRR"]


def test_without_jieba_falls_back(monkeypatch):
    monkeypatch.setattr(T, "_jieba", False)
    assert T.zh_words("蝴蝶") == []
    assert T.fts5_query("蝴蝶") == '("蝴蝶" OR "蝴 蝶")'  # whole-term word OR char phrase
    assert "蝴蝶" not in T.search_text({"title": "蝴蝶"}).split()


def test_refresh_search_text(tmp_path, monkeypatch):
    from galleryedge_db import dialect, schema
    from galleryedge_db.client import connect
    db = connect(f"file:{tmp_path}/a.db"); schema.init(db, dialect.detect(db))
    monkeypatch.setattr(T, "_jieba", False)
    schema.ingest(db, [{"id": "w1", "title": "蓝色蝴蝶", "source_url": "https://x.invalid/1", "license": "CC0", "rating": "general", "tags": ["Lakes of Nowhere", "花"]}])
    assert db.execute("SELECT count(*) FROM works_fts WHERE works_fts MATCH ?", ['"蝴蝶"']).rows[0][0] == 0
    monkeypatch.setattr(T, "_jieba", None)
    assert schema.refresh_search_text(db) == 1 and schema.refresh_search_text(db) == 0
    assert db.execute("SELECT count(*) FROM works_fts WHERE works_fts MATCH ?", ['"蝴蝶"']).rows[0][0] == 1
    assert db.execute("SELECT count(*) FROM works_fts WHERE works_fts MATCH ?", ['"Lakes" "Nowhere"']).rows[0][0] == 1
