"""Measure Chinese search relevance: single-char phrase only vs jieba words + char fallback.

Labeled FAKE corpus with classic boundary-crossing traps (e.g. 和服 inside 温和服务). Prints P@1, P@3, MRR, recall.
Usage: python tools/zh_relevance.py
"""
from __future__ import annotations

import json
import sqlite3

from galleryedge_db import text as T

DOCS = {
    "d1": "和服少女在京都散步", "d2": "温和服务的咖啡店", "d3": "红色和服与樱花",
    "d4": "上海外滩夜景", "d5": "海上日出与渔船", "d6": "上海的老街",
    "d7": "蓝色蝴蝶停在花上", "d8": "蝴蝶结发饰", "d9": "花园里的蝴蝶",
    "d10": "白天鹅在湖面", "d11": "明天鹅毛大雪", "d12": "天鹅与小天鹅",
    "d13": "山水画卷", "d14": "火山口的湖水", "d15": "高山流水",
    "d16": "秋天的枫叶", "d17": "立秋天气转凉", "d18": "秋天红叶小径",
}
QRELS = {"和服": {"d1", "d3"}, "上海": {"d4", "d6"}, "蝴蝶": {"d7", "d9"}, "天鹅": {"d10", "d12"},
         "秋天": {"d16", "d18"}, "山水": {"d13"}, "火山": {"d14"}}


def build(segment: bool):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE f USING fts5(id UNINDEXED, s, tokenize='unicode61')")
    for i, t in DOCS.items():
        s = T.search_text({"title": t}) if segment else T.cjk_space(t)
        c.execute("INSERT INTO f VALUES (?,?)", (i, s))
    return c


def run(segment: bool) -> dict:
    c = build(segment)
    p1 = p3 = mrr = rec = rp = 0.0
    for q, rel in QRELS.items():
        got = [r[0] for r in c.execute("SELECT id FROM f WHERE f MATCH ? ORDER BY bm25(f)", (T.fts5_query(q, segment=segment),))]
        p1 += (got[:1] and got[0] in rel) or 0
        p3 += len(set(got[:3]) & rel) / min(3, max(len(got[:3]), 1)) if got else 0
        mrr += next((1 / (k + 1) for k, g in enumerate(got) if g in rel), 0)
        rec += len(set(got) & rel) / len(rel)
        rp += len(set(got[:len(rel)]) & rel) / len(rel)  # R-precision
    n = len(QRELS)
    return {"P@1": round(p1 / n, 3), "P@3": round(p3 / n, 3), "MRR": round(mrr / n, 3), "R-precision": round(rp / n, 3), "recall": round(rec / n, 3)}


if __name__ == "__main__":
    print(json.dumps({"char_only": run(False), "jieba+char": run(True), "jieba": bool(T._seg())}, ensure_ascii=False, indent=1))
