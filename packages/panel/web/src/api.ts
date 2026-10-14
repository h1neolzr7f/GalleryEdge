export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const r = await fetch(path, {
    method: opts.method ?? "GET",
    headers: { "Content-Type": "application/json", "X-GE-Panel": "1" },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ?? data.error ?? `HTTP ${r.status}`);
  return data as T;
}
export const fmtBytes = (n: number) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : n < 1 << 30 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${(n / (1 << 30)).toFixed(2)} GB`;
