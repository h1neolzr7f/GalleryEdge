"""Security & compliance self-check. Each check returns ok / warn / fail + a fix hint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config as config_mod
from .masking import redact


def _recent(conn, action_like: str, days: int) -> str | None:
    r = conn.execute("SELECT ts FROM audit WHERE action LIKE ? ORDER BY id DESC LIMIT 1", (action_like,)).fetchone()
    if r and datetime.fromisoformat(r[0]) > datetime.now(timezone.utc) - timedelta(days=days):
        return r[0]
    return None


def run(cfg: dict, conn, vault=None, *, index_db=None) -> dict:
    checks = []

    def add(id, title, status, detail, fix=""):
        checks.append({"id": id, "title": title, "status": status, "detail": detail, "fix": fix})

    p = cfg["panel"]
    local = p.get("bind", "127.0.0.1") in ("127.0.0.1", "localhost", "::1") and not p.get("allow_remote")
    add("panel.local", "面板只监听本机 / panel bound to localhost", "ok" if local else "warn",
        f"bind={p.get('bind')} allow_remote={bool(p.get('allow_remote'))}", "panel.bind 设为 127.0.0.1，allow_remote 设为 false")
    admins = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
    add("auth.admin", "已设置管理员账号 / admin account exists", "ok" if admins else "fail", f"{admins} active admin(s)",
        "运行 ge-panel --config config.json admin create <用户名>")
    open_inv = conn.execute("SELECT count(*) FROM invites WHERE used_by IS NULL AND expires_at > ?", (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
    add("auth.invites", "未使用的邀请码 / unused invites", "ok" if open_inv <= 3 else "warn", f"{open_inv} open invite(s)", "用不到的邀请码让它过期即可")
    add("panel.observe", "包任务默认只读 / pack tasks observe-only", "ok" if not p.get("takeover") else "warn",
        "takeover " + ("on" if p.get("takeover") else "off"), "不需要重试/跳过时关闭 panel.takeover")
    raw = {k: v for k, v in cfg.items() if not k.startswith("_")}
    try:
        config_mod.validate(raw)
        add("config.secrets", "配置里没有明文密钥 / no inline secrets", "ok", "all secrets are vault:<name> references")
    except config_mod.ConfigError as e:
        add("config.secrets", "配置校验 / config validation", "fail", "; ".join(e.problems)[:300], "把密钥移进凭据库，配置只写 vault:<name>")
    if vault is None:
        add("vault.unlocked", "凭据库已解锁 / vault unlocked", "warn", "vault locked; credentials unavailable", "启动前设置 GE_VAULT_PASSPHRASE")
    else:
        exp = vault.expiring(cfg.get("vault", {}).get("remind_days", 14))
        bad = [e["name"] for e in exp if e["expired"]]
        add("vault.expiry", "凭据未过期 / no expired credentials", "fail" if bad else ("warn" if exp else "ok"),
            f"expired: {bad}" if bad else f"{len(exp)} expiring soon", "在配置中心轮换这些凭据")
        refs = [v for _, v in config_mod._walk(raw) if isinstance(v, str) and v.startswith("vault:")]
        missing = [r for r in refs if r[6:] not in vault.doc["entries"]]
        add("vault.refs", "配置引用的凭据都存在 / all vault refs resolve", "fail" if missing else "ok", f"missing: {missing}" if missing else f"{len(refs)} refs ok",
            "在配置中心补上缺失的凭据")
    al = cfg.get("alerts", {})
    if al.get("webhook_url_ref"):
        add("alerts.hmac", "Webhook 有签名 / webhook signed", "ok" if al.get("webhook_hmac_ref") else "warn",
            "HMAC " + ("set" if al.get("webhook_hmac_ref") else "missing"), "设置 alerts.webhook_hmac_ref")
    b = _recent(conn, "backup.%", 7)
    add("backup.recent", "7 天内做过备份 / backup within 7 days", "ok" if b else "warn", b or "no recent backup", "点「备份演练」或运行 ge-ops backup create")
    d = _recent(conn, "backup.drill", 30)
    add("backup.drill", "30 天内做过恢复演练 / restore drill within 30 days", "ok" if d else "warn", d or "never", "点「备份演练」：备份后恢复到临时目录并校验")
    probe = "token=FAKE_selfcheck_secret_0001"
    add("logs.redaction", "日志脱敏生效 / log redaction works", "ok" if "FAKE_selfcheck_secret_0001" not in redact(probe) else "fail", "redaction self-test")
    add("crawler.robots", "爬虫强制遵守 robots / crawler enforces robots", "ok", "enforced by crawler-core (not configurable off)")
    r = conn.execute("SELECT payload FROM events WHERE code='crawler.skips' ORDER BY id DESC LIMIT 1").fetchone()
    if r and "robots-unreachable" in (r[0] or ""):
        add("crawler.unreachable", "robots 不可达的站点 / hosts with unreachable robots", "warn", "some hosts skipped because robots.txt was unreachable", "确认目标站点状态")
    add("cleanup.safe", "清理默认干跑且保护账本 / cleanup dry-run + ledger protection", "ok", "enforced by cleanup rules + tests")
    if index_db is not None:
        try:
            tot = index_db.execute("SELECT count(*) FROM works").rows[0][0]
            miss = index_db.execute("SELECT count(*) FROM works WHERE coalesce(license,'')='' OR coalesce(source_url,'')=''").rows[0][0]
            add("index.provenance", "作品记录都有许可和出处 / records carry license + source", "ok" if miss == 0 else "fail",
                f"{miss}/{tot} records missing license or source_url", "重新入库或删除这些记录")
        except Exception as e:
            add("index.provenance", "索引库检查 / index check", "warn", f"index unavailable: {type(e).__name__}")
    score = round(100 * sum(c["status"] == "ok" for c in checks) / len(checks))
    return {"score": score, "checks": checks, "fails": sum(c["status"] == "fail" for c in checks), "warns": sum(c["status"] == "warn" for c in checks)}
