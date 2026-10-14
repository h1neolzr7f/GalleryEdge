import { prefixSql } from "./text";
// Turso/libSQL Hrana v2 over HTTP (POST /v2/pipeline) using fetch only.
export type Value = null | number | bigint | string | Uint8Array;
export interface Result { columns: string[]; rows: Value[][] }
export class DBError extends Error {}

type HVal = { type: string; value?: string | number; base64?: string };

function enc(v: Value): HVal {
  if (v === null || v === undefined) return { type: "null" };
  if (typeof v === "bigint") return { type: "integer", value: v.toString() };
  if (typeof v === "number") return Number.isInteger(v) ? { type: "integer", value: String(v) } : { type: "float", value: v };
  if (v instanceof Uint8Array) return { type: "blob", base64: btoa(String.fromCharCode(...v)).replace(/=+$/, "") };
  return { type: "text", value: String(v) };
}

function dec(v: HVal): Value {
  switch (v.type) {
    case "null": return null;
    case "integer": { const n = BigInt(v.value as string); return n <= BigInt(Number.MAX_SAFE_INTEGER) && n >= -BigInt(Number.MAX_SAFE_INTEGER) ? Number(n) : n; }
    case "float": return Number(v.value);
    case "blob": { const s = atob((v.base64 as string) + "=".repeat((4 - ((v.base64 as string).length % 4)) % 4)); return Uint8Array.from(s, c => c.charCodeAt(0)); }
    default: return v.value as string;
  }
}

export class Hrana {
  private base: string;
  constructor(url: string, private token?: string, private fetcher: typeof fetch = ((i: any, o?: any) => fetch(i, o)) as typeof fetch, private prefix = "") {
    prefixSql("", prefix);
    let u = url.replace(/^libsql:\/\//, "https://");
    if (/^https:\/\/(127\.0\.0\.1|localhost)/.test(u)) u = "http://" + u.slice(8);
    this.base = u.replace(/\/+$/, "");
  }
  toString() { return `Hrana(${this.base}, auth=${this.token ? "yes" : "no"})`; }

  private async post(requests: unknown[]): Promise<any[]> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    let res: Response;
    try {
      res = await this.fetcher(this.base + "/v2/pipeline", { method: "POST", headers, body: JSON.stringify({ requests: [...requests, { type: "close" }] }) });
    } catch (e) { throw new DBError(`libsql unreachable`); }
    if (!res.ok) throw new DBError(`libsql http ${res.status}`);
    const body: any = await res.json();
    for (const r of body.results) if (r.type === "error") throw new DBError(r.error?.message ?? "error");
    return body.results.slice(0, -1).map((r: any) => r.response);
  }

  private static result(r: any): Result {
    return { columns: r.cols.map((c: any) => c.name), rows: r.rows.map((row: HVal[]) => row.map(dec)) };
  }

  async execute(sql: string, args: Value[] = []): Promise<Result> {
    const [r] = await this.post([{ type: "execute", stmt: { sql: prefixSql(sql, this.prefix), args: args.map(enc) } }]);
    return Hrana.result(r.result);
  }

  /** Transactional batch: each step conditional on the previous one, rollback on failure. */
  async batch(items: [string, Value[]][]): Promise<Result[]> {
    const steps: any[] = [{ stmt: { sql: "BEGIN" } }];
    items.forEach(([sql, args], k) => steps.push({ stmt: { sql: prefixSql(sql, this.prefix), args: args.map(enc) }, condition: { type: "ok", step: k } }));
    const n = steps.length;
    steps.push({ stmt: { sql: "COMMIT" }, condition: { type: "ok", step: n - 1 } });
    steps.push({ stmt: { sql: "ROLLBACK" }, condition: { type: "not", cond: { type: "ok", step: n } } });
    const [r] = await this.post([{ type: "batch", batch: { steps } }]);
    const err = r.result.step_errors.find((e: any) => e);
    if (err) throw new DBError(err.message ?? "batch failed");
    return r.result.step_results.slice(1, n).map(Hrana.result);
  }
}

export function rowsToDicts(r: Result): Record<string, Value>[] {
  return r.rows.map(row => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
}
