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


_RUN_RE = re.compile(f"[{CJK}]+")
_jieba = None


def _seg():
    """jieba (optional extra `zh`); None when not installed => single-character matching only."""
    global _jieba
    if _jieba is None:
        try:
            import logging

            import jieba  # type: ignore
            jieba.setLogLevel(logging.WARNING)
            _jieba = jieba
        except ImportError:
            _jieba = False
    return _jieba or None


def zh_words(s: str | None) -> list[str]:
    """Multi-character Chinese words (jieba search mode) found in the CJK runs of s."""
    jb = _seg()
    if not jb or not s:
        return []
    out: list[str] = []
    for run in _RUN_RE.findall(s):
        out += [w for w in jb.cut_for_search(run) if len(w) >= 2]
    return list(dict.fromkeys(out))


def search_text(w: dict) -> str:
    """Single CJK chars (fallback, substring-like) + jieba words as whole tokens (relevance)."""
    parts = [w.get("title"), w.get("prompt"), " ".join(w.get("tags", [])) if isinstance(w.get("tags"), list) else w.get("tags_text"), w.get("artist")]
    base = " ".join(p for p in parts if p)
    words = zh_words(base)
    return cjk_space(base) + (" " + " ".join(words) if words else "")


def query_words(t: str) -> list[str]:
    """Query side uses jieba precise mode (index side uses search mode, which also contains these words)."""
    jb = _seg()
    if not jb:
        return [t]
    ws = [w for w in jb.cut(t) if w.strip()]
    return ws or [t]


def cjk_clause(t: str, words: list[str] | None = None) -> str:
    """One CJK query term: (its words AND-ed) OR the single-char phrase fallback (always kept for recall)."""
    sp = cjk_space(t)
    if len(t) == 1:
        return f'"{sp}"'
    ws = [w for w in (words if words is not None else query_words(t)) if len(w) >= 1]
    word_part = " AND ".join(f'"{cjk_space(w)}"' if len(w) == 1 else f'"{w}"' for w in ws)
    return f"(({word_part}) OR \"{sp}\")" if len(ws) > 1 else f'("{ws[0]}" OR "{sp}")'


def fts5_query(q: str, *, segment: bool = True) -> str:
    """Free user text -> safe FTS5 MATCH (AND of terms; CJK terms = word OR char-phrase, latin terms prefix)."""
    out = []
    for t in re.findall(r"[\w\-]+", q or "", flags=re.UNICODE)[:12]:
        t = t.replace('"', "")
        if _CJK_RE.search(t):
            if not _RUN_RE.fullmatch(t):  # mixed latin+CJK: phrase only
                out.append(f'"{cjk_space(t)}"')
            else:
                out.append(cjk_clause(t) if segment else f'"{cjk_space(t)}"')
        else:
            out.append(f'"{t}"*')
    return " ".join(out)
