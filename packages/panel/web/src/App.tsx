import { useCallback, useEffect, useState } from "react";
import { api, fmtBytes } from "./api";

type Tab = "status" | "packs" | "alerts" | "cleanup" | "events";
const TABS: [Tab, string][] = [["status", "状态"], ["packs", "包任务"], ["alerts", "告警"], ["cleanup", "清理"], ["events", "事件日志"]];

function useLoad<T>(path: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string>("");
  const reload = useCallback(() => { api<T>(path).then(d => { setData(d); setErr(""); }).catch(e => setErr(String(e.message))); }, [path, ...deps]);
  useEffect(reload, [reload]);
  return { data, err, reload };
}

const Badge = ({ v }: { v: string }) => <span className={`badge b-${v}`}>{v}</span>;

export function StatusView() {
  const { data, err, reload } = useLoad<any>("/api/status");
  const [busy, setBusy] = useState(false);
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const run = async () => { setBusy(true); try { await api("/api/health/run", { method: "POST" }); } finally { setBusy(false); reload(); } };
  const total = Object.values(data.packs as Record<string, number>).reduce((a, b) => a + b, 0);
  return (<div>
    <div className="cards">
      <div className="kpi"><b>{data.backends.filter((b: any) => b.status === "up").length}/{data.configured_backends.length}</b><span>后端在线</span></div>
      <div className="kpi"><b>{total}</b><span>包任务（失败 {data.packs.error ?? 0}）</span></div>
      <div className={`kpi ${data.alerts_open ? "warn" : ""}`}><b>{data.alerts_open}</b><span>未确认告警</span></div>
      <div className={`kpi ${data.credentials_expiring.length ? "warn" : ""}`}><b>{data.credentials_expiring.length}</b><span>即将到期凭据</span></div>
    </div>
    <h3>爬虫</h3>
    {data.crawler ? <table><tbody>
      <tr><td>最近心跳</td><td>{data.crawler.last_ts} · <code>{data.crawler.last_code}</code> · {data.crawler.last_message}</td></tr>
      <tr><td>统计</td><td>抓取 {data.crawler.stats.fetched ?? "-"} · 记录 {data.crawler.stats.records ?? "-"} · 跳过 {data.crawler.stats.skipped ?? "-"} · 重复 {data.crawler.stats.dupes ?? "-"}</td></tr>
      <tr><td>跳过原因</td><td>{Object.entries(data.crawler.skip_reasons).map(([k, v]) => <span key={k} className="badge b-protected" style={{ marginRight: 6 }}>{k} {String(v)}</span>)}</td></tr>
    </tbody></table> : <p className="meta">还没有爬虫事件（用 <code>ge-crawl --panel-db</code> 接入）</p>}
    <h3>存储与数据库后端 <button onClick={run} disabled={busy}>{busy ? "检查中…" : "立即健康检查"}</button></h3>
    <table><thead><tr><th>ID</th><th>类型</th><th>状态</th><th>详情</th><th>最近探测</th></tr></thead><tbody>
      {data.backends.length === 0 && <tr><td colSpan={5}>还没跑过健康检查</td></tr>}
      {data.backends.map((b: any) => <tr key={b.id}><td>{b.id}</td><td>{b.type}</td><td><Badge v={b.status} /></td><td>{b.detail}</td><td>{b.last_probe_at}</td></tr>)}
    </tbody></table>
    <h3>最近事件</h3><EventTable rows={data.recent_events} />
  </div>);
}

function EventTable({ rows }: { rows: any[] }) {
  return <table><thead><tr><th>时间 (UTC)</th><th>来源</th><th>级别</th><th>代码</th><th>消息</th></tr></thead><tbody>
    {rows.map(e => <tr key={e.id}><td>{e.ts}</td><td>{e.source}</td><td><Badge v={e.level} /></td><td><code>{e.code}</code></td><td>{e.message}</td></tr>)}
  </tbody></table>;
}

export function PacksView() {
  const { data, err, reload } = useLoad<any>("/api/packs");
  const [msg, setMsg] = useState("");
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const act = async (id: string, a: string) => {
    if (a === "skip" && !window.confirm(`确定跳过 ${id}？跳过后不会再处理这个包。`)) return;
    try { await api(`/api/packs/${id}/${a}`, { method: "POST", body: a === "skip" ? { confirm: true } : {} }); setMsg(`${id}: ${a} ✓`); }
    catch (e: any) { setMsg(`${id}: ${e.message}`); }
    reload();
  };
  const stages: string[] = data.stages;
  return (<div>
    {!data.takeover && <p className="note">只读观察模式：在配置里打开 <code>panel.takeover</code> 后才能重试 / 暂停 / 跳过。</p>}
    {msg && <p className="note">{msg}</p>}
    <table><thead><tr><th>包</th><th>阶段</th><th>状态</th><th>进度</th><th>错误</th><th>重试次数</th><th>操作</th></tr></thead><tbody>
      {data.items.map((p: any) => (<tr key={p.pack_id}>
        <td>{p.pack_id}</td>
        <td><div className="stages">{stages.map((s, i) => <span key={s} title={s} className={i < stages.indexOf(p.stage) ? "s-done" : s === p.stage ? `s-cur s-${p.status}` : ""} />)}</div>{p.stage}</td>
        <td><Badge v={p.status} /></td>
        <td><progress max={1} value={p.progress} /></td>
        <td className="errtxt">{p.error ?? ""}</td><td>{p.attempts}</td>
        <td className="acts">
          {p.status === "error" && <button disabled={!data.takeover} onClick={() => act(p.pack_id, "retry")}>重试</button>}
          {(p.status === "running" || p.status === "pending") && <button className="ghost" disabled={!data.takeover} onClick={() => act(p.pack_id, "pause")}>暂停</button>}
          {p.status === "paused" && <button disabled={!data.takeover} onClick={() => act(p.pack_id, "resume")}>继续</button>}
          {p.status !== "done" && p.status !== "skipped" && <button className="ghost danger" disabled={!data.takeover} onClick={() => act(p.pack_id, "skip")}>跳过</button>}
        </td></tr>))}
    </tbody></table>
  </div>);
}

export function AlertsView() {
  const [all, setAll] = useState(false);
  const { data, err, reload } = useLoad<any[]>(`/api/alerts${all ? "?all=true" : ""}`, [all]);
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  return (<div>
    <label><input type="checkbox" checked={all} onChange={e => setAll(e.target.checked)} /> 显示已确认</label>
    {data.length === 0 && <p>没有告警 🎉</p>}
    <ul className="alerts">{data.map(a => <li key={a.id} className={`al al-${a.level}`}>
      <div><Badge v={a.level} /> <b>{a.key}</b> <span className="meta">{a.ts} · 送达 {a.delivered}</span></div>
      <div>{a.message}</div>
      {!a.acked && <button onClick={() => api(`/api/alerts/${a.id}/ack`, { method: "POST" }).then(reload)}>确认</button>}
    </li>)}</ul>
  </div>);
}

export function CleanupView() {
  const [plan, setPlan] = useState<any>(null);
  const [ok, setOk] = useState(false);
  const [msg, setMsg] = useState("");
  const preview = () => { setOk(false); setMsg(""); api("/api/cleanup/plan").then(setPlan).catch(e => setMsg(e.message)); };
  useEffect(preview, []);
  const apply = async () => {
    try { const r = await api("/api/cleanup/apply", { method: "POST", body: { token: plan.token, confirm: true } }); setMsg(`已删除 ${r.deleted} 个文件，释放 ${fmtBytes(r.bytes)}`); }
    catch (e: any) { setMsg(e.message); }
    preview();
  };
  if (!plan) return <p>{msg || "预览中…"}</p>;
  return (<div>
    <p className="note">这是预览（dry-run），还没有删任何东西。只会删除已确认上传的本地副本；账本、未上传的 tar、进行中的 staging 永不删除。</p>
    {msg && <p className="note">{msg}</p>}
    <h3>将删除 {plan.delete.length} 个文件（{fmtBytes(plan.bytes)}）</h3>
    <table><tbody>{plan.delete.map((d: any) => <tr key={d.path}><td><code>{d.path}</code></td><td>{fmtBytes(d.size)}</td></tr>)}</tbody></table>
    <h3>受保护 {plan.protected.length} 个</h3>
    <table><tbody>{plan.protected.map((d: any) => <tr key={d.path}><td><code>{d.path}</code></td><td><Badge v="protected" /> {d.reason}</td></tr>)}</tbody></table>
    <div className="confirm">
      <label><input type="checkbox" checked={ok} onChange={e => setOk(e.target.checked)} /> 我已检查上面的列表，确认删除</label>
      <button className="danger" disabled={!ok || plan.delete.length === 0} onClick={apply}>执行清理</button>
      <button className="ghost" onClick={preview}>重新预览</button>
    </div>
  </div>);
}

export function EventsView() {
  const [source, setSource] = useState(""), [level, setLevel] = useState("");
  const q = new URLSearchParams(Object.entries({ source, level }).filter(([, v]) => v)).toString();
  const { data, err } = useLoad<any[]>(`/api/events${q ? "?" + q : ""}`, [q]);
  return (<div>
    <div className="filters">
      <select value={source} onChange={e => setSource(e.target.value)} aria-label="来源"><option value="">全部来源</option>{["crawler", "ingest", "edge", "agent", "health", "alerts"].map(s => <option key={s}>{s}</option>)}</select>
      <select value={level} onChange={e => setLevel(e.target.value)} aria-label="级别"><option value="">全部级别</option>{["info", "warn", "error"].map(s => <option key={s}>{s}</option>)}</select>
    </div>
    {err ? <p className="err">{err}</p> : data ? <EventTable rows={data} /> : <p>加载中…</p>}
  </div>);
}

export function App() {
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1) as Tab) || "status");
  useEffect(() => { location.hash = tab; }, [tab]);
  const View = { status: StatusView, packs: PacksView, alerts: AlertsView, cleanup: CleanupView, events: EventsView }[tab];
  return (<div className="app">
    <aside><h1>GalleryEdge<small>运维面板 · 仅本机</small></h1>
      <nav>{TABS.map(([k, label]) => <button key={k} className={k === tab ? "on" : ""} onClick={() => setTab(k)}>{label}</button>)}</nav>
    </aside>
    <main><h2>{TABS.find(t => t[0] === tab)![1]}</h2><View /></main>
  </div>);
}
