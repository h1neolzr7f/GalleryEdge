"""Search-text normalisation shared by Python ingest and the TS Worker (worker/src/text.ts).

FTS5 ``unicode61`` treats a run of CJK characters as ONE token, so "原神" would never
match inside "原神胡桃". We store a search column where every CJK character is its
own token and turn CJK query terms into phrase queries ("胡 桃"), which gives exact
substring-like matching for Chinese/Japanese without a custom tokenizer.
Verified on Turso Cloud (libSQL 3.47, FTS5); Turso's native ``USING fts`` is not
available on these databases (see docs/STAGING.md).
"""
from __future__ import annotations

import re

CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_CJK_RE = re.compile(f"([{CJK}])")


def cjk_space(s: str | None) -> str:
    return re.sub(r"\s+", " ", _CJK_RE.sub(r" \1 ", s or "")).strip()


def search_text(w: dict) -> str:
    parts = [w.get("title"), w.get("prompt"), " ".join(w.get("tags", [])) if isinstance(w.get("tags"), list) else w.get("tags_text"), w.get("artist")]
    return cjk_space(" ".join(p for p in parts if p))


def fts5_query(q: str) -> str:
    """Free user text -> safe FTS5 MATCH (AND of terms; CJK terms become phrases, latin terms prefix)."""
    out = []
    for t in re.findall(r"[\w\-]+", q or "", flags=re.UNICODE)[:12]:
        t = t.replace('"', "")
        sp = cjk_space(t)
        out.append(f'"{sp}"' if " " in sp or _CJK_RE.search(t) else f'"{t}"*')
    return " ".join(out)
