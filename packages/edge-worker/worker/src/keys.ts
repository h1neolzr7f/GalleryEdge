// Mirrors galleryedge_edge/keys.py (same tables, same SHA-256 key hashes).
import { Hrana } from "./db";

export async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export async function check(db: Hrana, key: string | null, now = new Date()): Promise<[number, string, Record<string, string>]> {
  if (!key || !key.startsWith("ge_") || key.split("_").length < 3) return [401, "missing or malformed API key", {}];
  const prefix = key.split("_")[1];
  const r = await db.execute("SELECT k.hash,k.status,p.rpm,p.rpd FROM api_keys k JOIN api_plans p ON p.name=k.plan WHERE k.prefix=?", [prefix]);
  if (!r.rows.length || !safeEqual(String(r.rows[0][0]), await sha256Hex(key))) return [401, "invalid API key", {}];
  const [, status, rpm, rpd] = r.rows[0] as [string, string, number, number];
  if (status !== "active") return [403, "API key revoked", {}];
  const iso = now.toISOString();
  const day = iso.slice(0, 10), minute = iso.slice(0, 16);
  const d = await db.execute("SELECT count FROM usage_daily WHERE prefix=? AND day=?", [prefix, day]);
  const m = await db.execute("SELECT count FROM usage_minute WHERE prefix=? AND minute=?", [prefix, minute]);
  const usedD = d.rows.length ? Number(d.rows[0][0]) : 0, usedM = m.rows.length ? Number(m.rows[0][0]) : 0;
  if (usedD >= rpd) return [429, "daily quota exceeded", { "Retry-After": "3600", "X-RateLimit-Remaining-Day": "0" }];
  if (usedM >= rpm) return [429, "rate limit exceeded", { "Retry-After": String(60 - now.getUTCSeconds()), "X-RateLimit-Remaining-Day": String(rpd - usedD) }];
  await db.batch([
    ["INSERT INTO usage_daily VALUES (?,?,1) ON CONFLICT(prefix,day) DO UPDATE SET count=count+1", [prefix, day]],
    ["INSERT INTO usage_minute VALUES (?,?,1) ON CONFLICT(prefix,minute) DO UPDATE SET count=count+1", [prefix, minute]],
  ]);
  return [200, "ok", { "X-RateLimit-Remaining-Day": String(rpd - usedD - 1), "X-RateLimit-Remaining-Minute": String(rpm - usedM - 1) }];
}
