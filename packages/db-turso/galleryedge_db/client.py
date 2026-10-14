"""Minimal DB clients sharing one interface: ``execute(sql, args) -> Result`` and ``batch``.

* ``HranaClient`` speaks Turso/libSQL Hrana v2 over HTTP (``/v2/pipeline``) — the
  same path an edge Worker uses. Works against Turso and a local ``sqld``.
* ``SqliteClient`` wraps stdlib sqlite3 for files / ``:memory:``.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class DBError(Exception):
    pass


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rows_affected: int = 0
    rows_read: int = 0
    rows_written: int = 0

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(v).decode().rstrip("=")}
    return {"type": "text", "value": str(v)}


def _dec(v):
    t = v["type"]
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        s = v["base64"]
        return base64.b64decode(s + "=" * (-len(s) % 4))
    return v["value"]


class HranaClient:
    def __init__(self, url: str, auth_token: str | None = None, *, timeout: float = 10.0, opener=None):
        u = url.replace("libsql://", "https://", 1)
        if u.startswith("https://") and ("127.0.0.1" in u or "localhost" in u):
            u = "http://" + u[len("https://"):]
        self.base = u.rstrip("/")
        self._token = auth_token
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def __repr__(self) -> str:  # never show the token
        return f"HranaClient({self.base!r}, auth={'yes' if self._token else 'no'})"

    def _post(self, reqs: list[dict]) -> list[dict]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(self.base + "/v2/pipeline", data=json.dumps({"requests": [*reqs, {"type": "close"}]}).encode(),
                                     headers=headers, method="POST")
        try:
            with self.opener(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise DBError(f"libsql http {e.code}") from None
        except OSError as e:
            raise DBError(f"libsql unreachable: {type(e).__name__}") from None
        for res in body["results"]:
            if res["type"] == "error":
                raise DBError(res["error"].get("message", "error"))
        return [res["response"] for res in body["results"][:-1]]

    @staticmethod
    def _result(r: dict) -> Result:
        return Result([c["name"] for c in r["cols"]], [tuple(_dec(v) for v in row) for row in r["rows"]],
                      r.get("affected_row_count", 0), r.get("rows_read", 0), r.get("rows_written", 0))

    def _pipeline(self, stmts: list[dict], transactional: bool = False) -> list[Result]:
        if not transactional:
            return [self._result(r["result"]) for r in self._post([{"type": "execute", "stmt": s} for s in stmts])]
        # Hrana batch with conditions: each step runs only if the previous succeeded; rollback otherwise.
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for k, s in enumerate(stmts):
            steps.append({"stmt": s, "condition": {"type": "ok", "step": k}})
        n = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": n - 1}})
        steps.append({"stmt": {"sql": "ROLLBACK"}, "condition": {"type": "not", "cond": {"type": "ok", "step": n}}})
        res = self._post([{"type": "batch", "batch": {"steps": steps}}])[0]["result"]
        errs = [e for e in res["step_errors"] if e]
        if errs:
            raise DBError(errs[0].get("message", "batch failed"))
        return [self._result(r) for r in res["step_results"][1:n]]

    def execute(self, sql: str, args: tuple | list = ()) -> Result:
        return self._pipeline([{"sql": sql, "args": [_enc(a) for a in args]}])[0]

    def batch(self, items: list[tuple[str, tuple]], transactional: bool = True) -> list[Result]:
        return self._pipeline([{"sql": s, "args": [_enc(a) for a in a_]} for s, a_ in items], transactional)

    def executescript(self, script: str) -> None:
        stmts = [s.strip() for s in split_sql(script) if s.strip()]
        self.batch([(s, ()) for s in stmts], transactional=False)


class SqliteClient:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path[5:] if path.startswith("file:") else path, check_same_thread=False)

    def execute(self, sql: str, args: tuple | list = ()) -> Result:
        try:
            cur = self.conn.execute(sql, tuple(args))
            rows = cur.fetchall()
            self.conn.commit()
        except sqlite3.Error as e:
            raise DBError(str(e)) from None
        return Result([d[0] for d in cur.description or []], [tuple(r) for r in rows], cur.rowcount if cur.rowcount > 0 else 0)

    def batch(self, items, transactional: bool = True) -> list[Result]:
        out = []
        try:
            with self.conn:
                for s, a in items:
                    cur = self.conn.execute(s, tuple(a))
                    out.append(Result([d[0] for d in cur.description or []], [tuple(r) for r in cur.fetchall()]))
        except sqlite3.Error as e:
            raise DBError(str(e)) from None
        return out

    def executescript(self, script: str) -> None:
        self.conn.executescript(script)
        self.conn.commit()


def split_sql(script: str) -> list[str]:
    """Split on ';' outside quotes and BEGIN...END trigger bodies."""
    out, buf, depth, q = [], [], 0, None
    tokens = script
    i = 0
    while i < len(tokens):
        ch = tokens[i]
        if q:
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch; buf.append(ch)
        elif ch == ";" and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
            w = "".join(buf).upper()
            if w.endswith("BEGIN") and (len(w) == 5 or not w[-6].isalnum()) and "TRIGGER" in w:
                depth += 1
            if w.endswith("END") and depth and (len(w) == 3 or not w[-4].isalnum()):
                nxt = tokens[i + 1:i + 2]
                if not nxt or not nxt.isalnum():
                    depth -= 1
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def connect(url: str, auth_token: str | None = None):
    if url.startswith(("libsql://", "http://", "https://")):
        return HranaClient(url, auth_token)
    return SqliteClient(url)
