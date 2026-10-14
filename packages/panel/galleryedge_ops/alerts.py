"""Alerts: always stored in the panel DB; optionally pushed to a webhook."""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from datetime import datetime, timedelta, timezone

from .events import emit, now
from .masking import redact


class Alerter:
    def __init__(self, conn, *, webhook_url: str | None = None, hmac_secret: str | None = None,
                 dedupe_minutes: int = 30, timeout: float = 5.0, opener=None):
        self.conn, self.webhook_url, self.hmac_secret = conn, webhook_url, hmac_secret
        self.dedupe = timedelta(minutes=dedupe_minutes)
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def _recent(self, key: str) -> bool:
        row = self.conn.execute("SELECT ts FROM alerts WHERE key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return bool(row) and datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < self.dedupe

    def fire(self, key: str, level: str, message: str, *, force: bool = False) -> bool:
        if not force and self._recent(key):
            return False
        message = redact(message)
        delivered = ["panel"]
        if self.webhook_url:
            body = json.dumps({"key": key, "level": level, "message": message, "ts": now(), "source": "galleryedge"}).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "GalleryEdge-Alerts/0.1"}
            if self.hmac_secret:
                headers["X-GalleryEdge-Signature"] = "sha256=" + hmac.new(self.hmac_secret.encode(), body, hashlib.sha256).hexdigest()
            try:
                req = urllib.request.Request(self.webhook_url, data=body, headers=headers, method="POST")
                with self.opener(req, timeout=self.timeout) as r:
                    if 200 <= getattr(r, "status", 200) < 300:
                        delivered.append("webhook")
            except Exception as e:  # webhook failure must never break alerting
                emit(self.conn, "alerts", "warn", "webhook.failed", f"webhook delivery failed: {type(e).__name__}")
        self.conn.execute("INSERT INTO alerts(ts,key,level,message,delivered) VALUES (?,?,?,?,?)",
                          (now(), key, level, message, ",".join(delivered)))
        self.conn.commit()
        emit(self.conn, "alerts", level, f"alert.{key}", message)
        return True

    def list(self, include_acked: bool = False) -> list[tuple]:
        q = "SELECT id,ts,key,level,message,acked,delivered FROM alerts" + ("" if include_acked else " WHERE acked=0") + " ORDER BY id DESC"
        return self.conn.execute(q).fetchall()

    def ack(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
        self.conn.commit()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    exp = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, header or "")
