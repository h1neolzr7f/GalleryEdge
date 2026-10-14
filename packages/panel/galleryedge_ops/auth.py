"""Invite-only panel accounts, scrypt password hashes, server-side sessions, lockout."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from .events import audit

ROLES = ("admin", "viewer")
SESSION_TTL = timedelta(hours=12)
IDLE_TIMEOUT = timedelta(hours=2)
LOCK_FAILS, LOCK_WINDOW = 5, timedelta(minutes=15)
USER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


class AuthError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str, *, n: int = 2**14) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, dklen=32)
    return f"scrypt${n}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


def check_password_policy(pw: str, username: str = "") -> None:
    if len(pw) < 12:
        raise AuthError("password must be at least 12 characters")
    if username and username.lower() in pw.lower():
        raise AuthError("password must not contain the username")
    if len(set(pw)) < 5:
        raise AuthError("password too repetitive")


def create_user(conn, username: str, password: str, role: str = "viewer", *, invited_by: str | None = None) -> None:
    if not USER_RE.match(username or ""):
        raise AuthError("username: 3-32 chars, lowercase letters, digits, _ . -")
    if role not in ROLES:
        raise AuthError("bad role")
    check_password_policy(password, username)
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise AuthError("username taken")
    conn.execute("INSERT INTO users VALUES (?,?,?,'active',?,?)", (username, hash_password(password), role, _iso(_now()), invited_by))
    conn.commit()
    audit(conn, invited_by or "cli", "user.create", {"username": username, "role": role})


def user_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE status='active'").fetchone()[0]


def list_users(conn) -> list[dict]:
    return [dict(zip(("username", "role", "status", "created_at", "invited_by"), r))
            for r in conn.execute("SELECT username, role, status, created_at, invited_by FROM users ORDER BY created_at")]


def set_status(conn, actor: str, username: str, status: str) -> None:
    if status not in ("active", "disabled"):
        raise AuthError("bad status")
    if username == actor and status == "disabled":
        raise AuthError("cannot disable yourself")
    if status == "disabled":
        r = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        admins = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
        if r and r[0] == "admin" and admins <= 1:
            raise AuthError("cannot disable the last admin")
    conn.execute("UPDATE users SET status=? WHERE username=?", (status, username))
    if status == "disabled":
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))
    conn.commit()
    audit(conn, actor, f"user.{status}", {"username": username})


def set_role(conn, actor: str, username: str, role: str) -> None:
    if role not in ROLES:
        raise AuthError("bad role")
    r = conn.execute("SELECT role, status FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        raise AuthError("no such user")
    if r[0] == role:
        return
    if r[0] == "admin":
        admins = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
        if admins <= 1 and r[1] == "active":
            raise AuthError("cannot demote the last admin")
        if username == actor:
            raise AuthError("cannot change your own role")
    conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))
    conn.execute("DELETE FROM sessions WHERE username=?", (username,))  # new role takes effect on next login
    conn.commit()
    audit(conn, actor, "user.role", {"username": username, "from": r[0], "to": role})


def revoke_invite(conn, actor: str, invite_id: str) -> None:
    if not invite_id or len(invite_id) < 8:
        raise AuthError("bad invite id")
    rows = conn.execute("SELECT code_hash, used_by FROM invites WHERE substr(code_hash,1,?)=?", (len(invite_id), invite_id)).fetchall()
    if len(rows) != 1:
        raise AuthError("no such invite")
    if rows[0][1]:
        raise AuthError("invite already used or revoked")
    conn.execute("UPDATE invites SET used_by=? WHERE code_hash=?", (f"revoked:{actor}", rows[0][0])); conn.commit()
    audit(conn, actor, "invite.revoke", {"id": invite_id})


def create_invite(conn, actor: str, role: str = "viewer", ttl_days: int = 7, note: str = "") -> str:
    if role not in ROLES:
        raise AuthError("bad role")
    code = "inv_" + secrets.token_urlsafe(18)
    now = _now()
    conn.execute("INSERT INTO invites VALUES (?,?,?,?,?,NULL,?)", (_h(code), role, actor, _iso(now), _iso(now + timedelta(days=ttl_days)), note))
    conn.commit()
    audit(conn, actor, "invite.create", {"role": role, "ttl_days": ttl_days})
    return code


def list_invites(conn) -> list[dict]:
    return [dict(zip(("id", "role", "created_by", "created_at", "expires_at", "used_by", "note"), (r[0][:8], *r[1:])))
            for r in conn.execute("SELECT code_hash, role, created_by, created_at, expires_at, used_by, note FROM invites ORDER BY created_at DESC")]


def register(conn, code: str, username: str, password: str) -> None:
    r = conn.execute("SELECT role, created_by, expires_at, used_by FROM invites WHERE code_hash=?", (_h(code or ""),)).fetchone()
    if not r or r[3] or datetime.fromisoformat(r[2]) < _now():
        raise AuthError("invalid or expired invite")
    create_user(conn, username, password, r[0], invited_by=r[1])
    conn.execute("UPDATE invites SET used_by=? WHERE code_hash=?", (username, _h(code)))
    conn.commit()


def totp_status(conn, username: str) -> bool:
    r = conn.execute("SELECT enabled FROM user_totp WHERE username=?", (username,)).fetchone()
    return bool(r and r[0])


def totp_setup(conn, username: str) -> dict:
    """Start (or restart) enrolment; the secret is returned once and only becomes active after totp_enable()."""
    from . import totp
    if totp_status(conn, username):
        raise AuthError("2FA already enabled; disable it first")
    sec = totp.new_secret()
    conn.execute("INSERT OR REPLACE INTO user_totp VALUES (?,?,0,-1,?)", (username, sec, _iso(_now())))
    conn.commit()
    return {"secret": sec, "otpauth_uri": totp.uri(sec, username)}


def _totp_check(conn, username: str, code: str) -> bool:
    from . import totp
    r = conn.execute("SELECT secret, last_step FROM user_totp WHERE username=?", (username,)).fetchone()
    if not r:
        return False
    st = totp.verify(r[0], code, last_step=r[1])
    if st is None:
        return False
    conn.execute("UPDATE user_totp SET last_step=? WHERE username=?", (st, username)); conn.commit()
    return True


def totp_enable(conn, username: str, code: str) -> None:
    if not _totp_check(conn, username, code):
        raise AuthError("wrong 2FA code")
    conn.execute("UPDATE user_totp SET enabled=1 WHERE username=?", (username,)); conn.commit()
    audit(conn, username, "auth.totp_enabled")


def totp_disable(conn, username: str, code: str) -> None:
    if not totp_status(conn, username) or not _totp_check(conn, username, code):
        raise AuthError("wrong 2FA code")
    conn.execute("DELETE FROM user_totp WHERE username=?", (username,))
    conn.execute("DELETE FROM sessions WHERE username=?", (username,)); conn.commit()
    audit(conn, username, "auth.totp_disabled")


def login(conn, username: str, password: str, otp: str | None = None, *, require_totp: bool = False) -> str:
    now = _now()
    fails = conn.execute("SELECT count(*) FROM login_failures WHERE username=? AND ts>?", (username, _iso(now - LOCK_WINDOW))).fetchone()[0]
    if fails >= LOCK_FAILS:
        raise AuthError("too many failed attempts, try again later")
    r = conn.execute("SELECT pw_hash, status FROM users WHERE username=?", (username,)).fetchone()
    ok = verify_password(password, r[0] if r else hash_password("x" * 12, n=2**10))  # constant-ish work
    if not r or not ok or r[1] != "active":
        conn.execute("INSERT INTO login_failures VALUES (?,?)", (username, _iso(now))); conn.commit()
        audit(conn, username or "?", "auth.login_failed")
        raise AuthError("wrong username or password")
    if totp_status(conn, username) or require_totp:
        if not totp_status(conn, username):
            audit(conn, username, "auth.login_refused_no_2fa")
            raise AuthError("2FA (TOTP) must be enabled before remote login; enrol from localhost first")
        if not otp:
            raise AuthError("totp_required")
        if not _totp_check(conn, username, otp):
            conn.execute("INSERT INTO login_failures VALUES (?,?)", (username, _iso(now))); conn.commit()
            audit(conn, username, "auth.totp_failed")
            raise AuthError("wrong 2FA code")
    conn.execute("DELETE FROM login_failures WHERE username=?", (username,))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (_h(token), username, _iso(now), _iso(now + SESSION_TTL), _iso(now)))
    conn.commit()
    audit(conn, username, "auth.login")
    return token


def session_user(conn, token: str | None) -> dict | None:
    if not token:
        return None
    now = _now()
    r = conn.execute("SELECT s.username, s.expires_at, s.last_seen, u.role, u.status FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=?",
                     (_h(token),)).fetchone()
    if not r:
        return None
    if r[4] != "active" or datetime.fromisoformat(r[1]) < now or datetime.fromisoformat(r[2]) + IDLE_TIMEOUT < now:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),)); conn.commit()
        return None
    conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (_iso(now), _h(token))); conn.commit()
    return {"username": r[0], "role": r[3]}


def logout(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),)); conn.commit()


def change_password(conn, username: str, old: str, new: str) -> None:
    r = conn.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if not r or not verify_password(old, r[0]):
        raise AuthError("current password is wrong")
    check_password_policy(new, username)
    conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(new), username))
    conn.execute("DELETE FROM sessions WHERE username=?", (username,))
    conn.commit()
    audit(conn, username, "auth.password_changed")
