import { useCallback, useEffect, useState } from "react";
import { api, fmtBytes } from "./api";

type Tab = "status" | "packs" | "storage" | "alerts" | "cleanup" | "events" | "config" | "users" | "releases" | "selfcheck" | "content" | "security" | "ops";
const TABS: [Tab, string][] = [["status", "状态"], ["packs", "包任务"], ["storage", "存储与成本"], ["alerts", "告警"], ["cleanup", "清理"], ["events", "事件日志"],
  ["config", "配置中心"], ["users", "用户"], ["releases", "发布"], ["selfcheck", "安全自检"], ["ops", "可视化运维"], ["content", "站点内容"], ["security", "两步验证"]];

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
  if (!data.configured_backends?.length) return <div className="empty-tip"><p>还没有配置任何存储后端。请到「配置中心」或重新运行安装向导。</p></div>;
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

const fmtVal = (v: number | null, unit: string) => v === null || v === undefined ? "-" : unit === "bytes" ? fmtBytes(v) : Math.round(v).toLocaleString();

export function StorageView() {
  const { data, err, reload } = useLoad<any>("/api/costs");
  const [plan, setPlan] = useState<any>(null);
  const [ok, setOk] = useState(false);
  const [msg, setMsg] = useState("");
  const loadPlan = () => { setOk(false); api("/api/storage/plan").then(setPlan).catch(e => setMsg(e.message)); };
  useEffect(loadPlan, []);
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const snap = async () => { await api("/api/costs/snapshot", { method: "POST", body: {} }); reload(); };
  const move = async () => {
    try { const r = await api("/api/storage/migrate", { method: "POST", body: { token: plan.token, confirm: true } }); setMsg(`已迁移 ${r.moved.length} 个对象到冷存储，失败 ${r.failed.length}`); }
    catch (e: any) { setMsg(e.message); }
    loadPlan(); reload();
  };
  return (<div>
    <div className="cards">
      <div className="kpi"><b>${data.monthly_total_usd.toFixed(2)}</b><span>预估月费（存储）</span></div>
      <div className={`kpi ${data.meters.some((m: any) => m.ratio >= 0.85) ? "warn" : ""}`}><b>{data.meters.filter((m: any) => m.ratio >= 0.85).length}</b><span>接近上限的计量</span></div>
      <div className={`kpi ${data.meters.some((m: any) => m.days_to_quota !== null && m.days_to_quota <= 14) ? "warn" : ""}`}><b>{(() => { const d = data.meters.map((m: any) => m.days_to_quota).filter((x: any) => x !== null); return d.length ? Math.round(Math.min(...d)) + " 天" : "—"; })()}</b><span>最快触顶</span></div>
      <div className="kpi"><b>{plan ? plan.moves.length : "-"}</b><span>可转冷对象</span></div>
    </div>
    <h3>用量与配额 <button onClick={snap}>立即采样</button></h3>
    <table><thead><tr><th>计量</th><th>当前</th><th>上限</th><th>占用</th><th>日增</th><th>预计触顶</th><th>月费</th></tr></thead><tbody>
      {data.meters.map((m: any) => <tr key={m.meter}>
        <td>{m.label}</td><td>{fmtVal(m.value, m.unit)}</td><td>{fmtVal(m.quota ?? null, m.unit)}</td>
        <td>{m.ratio === null ? "-" : <><progress max={1} value={Math.min(1, m.ratio)} className={m.ratio >= m.warn_ratio ? "hot" : ""} /> {(m.ratio * 100).toFixed(0)}%</>}</td>
        <td>{m.per_day === null ? "-" : m.per_day <= 0 ? "持平/下降" : fmtVal(m.per_day, m.unit) + "/天"}</td>
        <td>{m.days_to_quota === null ? "-" : m.days_to_quota > 3650 ? "> 10 年" : <span className={m.days_to_quota <= 14 ? "errtxt" : ""}>{Math.round(m.days_to_quota)} 天</span>}</td>
        <td>{m.monthly_cost_usd === null ? "-" : "$" + m.monthly_cost_usd.toFixed(2)}</td></tr>)}
    </tbody></table>
    <h3>热 → 冷迁移 {plan?.cold_backend && <span className="meta">目标：{plan.cold_backend}</span>}</h3>
    <p className="note">先复制到冷存储，校验大小和 SHA-256 并完整读回比对，通过后才删除热存储副本。账本、未完成上传和 staging 中的对象不会迁移。</p>
    {msg && <p className="note">{msg}</p>}
    {plan && <>
      <table><tbody>
        {plan.moves.map((m: any) => <tr key={m.key}><td><code>{m.key}</code></td><td>{m.src} → {m.dst}</td><td>{fmtBytes(m.size)}</td></tr>)}
        {plan.skipped.map((s: any) => <tr key={s.key}><td><code>{s.key}</code></td><td colSpan={2}><Badge v="protected" /> {s.reason}</td></tr>)}
        {plan.moves.length + plan.skipped.length === 0 && <tr><td>没有到期的热数据</td></tr>}
      </tbody></table>
      <div className="confirm">
        <label><input type="checkbox" checked={ok} onChange={e => setOk(e.target.checked)} /> 确认迁移这 {plan.moves.length} 个对象</label>
        <button className="danger" disabled={!ok || plan.moves.length === 0} onClick={move}>执行迁移</button>
      </div></>}
  </div>);
}

export function LoginView({ needsSetup, onDone }: { needsSetup: boolean; onDone: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [f, setF] = useState({ username: "", password: "", code: "", otp: "" });
  const [needOtp, setNeedOtp] = useState(false);
  const [err, setErr] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr("");
    try {
      if (mode === "register") await api("/api/auth/register", { method: "POST", body: f });
      await api("/api/auth/login", { method: "POST", body: { username: f.username, password: f.password, otp: f.otp || undefined } });
      onDone();
    } catch (x: any) {
      if (String(x.message).includes("totp_required")) { setNeedOtp(true); setErr("请输入身份验证器中的 6 位动态码"); }
      else setErr(x.message);
    }
  };
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (<div className="login"><form onSubmit={submit}>
    <h1>GalleryEdge<small>运维面板 · 图形化操作</small></h1>
    {needsSetup && <p className="warn-box">还没有账号。若是第一次使用，请关掉本页，运行带 <code>--setup</code> 的安装向导；或请管理员发邀请码。</p>}
    <label>用户名<input value={f.username} onChange={set("username")} autoComplete="username" /></label>
    <label>密码<input type="password" value={f.password} onChange={set("password")} autoComplete={mode === "login" ? "current-password" : "new-password"} /></label>
    {needOtp && <label>动态码（TOTP）<input value={f.otp} onChange={set("otp")} inputMode="numeric" autoComplete="one-time-code" maxLength={6} /></label>}
    {mode === "register" && <label>邀请码<input value={f.code} onChange={set("code")} /></label>}
    {err && <p className="err">{err}</p>}
    <button type="submit">{mode === "login" ? "登录" : "用邀请码注册"}</button>
    <p className="meta"><a href="#" onClick={e => { e.preventDefault(); setMode(mode === "login" ? "register" : "login"); }}>{mode === "login" ? "有邀请码？注册" : "返回登录"}</a> · 仅邀请注册，密码至少 12 位，连续 5 次失败锁定 15 分钟</p>
  </form></div>);
}

export function ConfigView() {
  const { data, err, reload } = useLoad<any>("/api/config");
  const [name, setName] = useState(""); const [val, setVal] = useState(""); const [days, setDays] = useState("");
  const [msg, setMsg] = useState("");
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const save = async (n: string) => {
    try {
      const r = await api<any>(`/api/vault/${encodeURIComponent(n)}`, { method: "POST", body: { value: val, expires_days: days ? Number(days) : null } });
      setMsg(`${n} 已保存（v${r.version}，${r.mask}）`); setVal(""); setName(""); reload();
    } catch (e: any) { setMsg(e.message); }
  };
  return (<div>
    <p className="meta">所有需要你提供的地址和密钥都在这里。密钥只存在加密保险库里，页面上永远只显示掩码。配置文件：<code>{data.path ?? "（未从文件启动）"}</code></p>
    <h3>端点</h3>
    <table><thead><tr><th>配置项</th><th>值</th></tr></thead><tbody>
      {data.endpoints.map((e: any) => <tr key={e.path}><td><code>{e.path}</code></td><td>{e.value}</td></tr>)}
    </tbody></table>
    <h3>凭据引用</h3>
    <table><thead><tr><th>配置项</th><th>引用</th><th>状态</th><th>掩码</th><th>到期</th></tr></thead><tbody>
      {data.refs.length === 0 && <tr><td colSpan={5}>配置里没有 vault: 引用</td></tr>}
      {data.refs.map((r: any) => <tr key={r.path}><td><code>{r.path}</code></td><td><code>{r.ref}</code></td><td><Badge v={r.exists ? "up" : "down"} /></td><td>{r.mask ?? "-"}</td><td>{r.expires_at ?? "-"}</td></tr>)}
    </tbody></table>
    <h3>保险库 {data.vault_unlocked ? <Badge v="up" /> : <Badge v="down" />}</h3>
    {!data.vault_unlocked ? <p className="warn-box">保险库未解锁：设置 <code>GE_VAULT_PASSPHRASE</code> 后重启面板。</p> : <>
      <table><thead><tr><th>名称</th><th>掩码</th><th>版本</th><th>轮换时间</th><th>到期</th></tr></thead><tbody>
        {data.credentials.map((c: any) => <tr key={c.name}><td>{c.name}</td><td>{c.mask}</td><td>v{c.version}</td><td>{c.rotated_at}</td><td>{c.expires_at ?? "-"}</td></tr>)}
      </tbody></table>
      <div className="row"><input placeholder="凭据名" value={name} onChange={e => setName(e.target.value)} />
        <input placeholder="新值（不会回显）" type="password" value={val} onChange={e => setVal(e.target.value)} />
        <input placeholder="有效天数" value={days} onChange={e => setDays(e.target.value)} style={{ width: 90 }} />
        <button disabled={!name || !val} onClick={() => save(name)}>保存 / 轮换</button></div></>}
    {msg && <p className="meta">{msg}</p>}
  </div>);
}

export function UsersView() {
  const { data, err, reload } = useLoad<any>("/api/users");
  const [code, setCode] = useState(""); const [role, setRole] = useState("viewer"); const [msg, setMsg] = useState("");
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const invite = async () => { try { setCode((await api<any>("/api/invites", { method: "POST", body: { role } })).code); reload(); } catch (e: any) { setMsg(e.message); } };
  const st = async (u: string, s: string) => { try { await api(`/api/users/${u}/${s}`, { method: "POST" }); reload(); } catch (e: any) { setMsg(e.message); } };
  const changeRole = async (u: string, r: string) => { try { await api(`/api/users/${u}/role`, { method: "POST", body: { role: r } }); setMsg(`${u} 角色已改为 ${r}（需重新登录生效）`); reload(); } catch (e: any) { setMsg(e.message); reload(); } };
  const revoke = async (id: string) => { if (!confirm("撤销这个邀请码？")) return; try { await api(`/api/invites/${id}/revoke`, { method: "POST" }); reload(); } catch (e: any) { setMsg(e.message); } };
  return (<div>
    <table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>最近登录</th><th></th></tr></thead><tbody>
      {data.users.map((u: any) => <tr key={u.username}><td>{u.username}</td><td><select aria-label={`角色 ${u.username}`} value={u.role} onChange={e => changeRole(u.username, e.target.value)}>
          <option value="viewer">viewer</option><option value="admin">admin</option></select></td><td><Badge v={u.status === "active" ? "up" : "down"} /></td><td>{u.last_login_at ?? "-"}</td>
        <td>{u.status === "active" ? <button onClick={() => st(u.username, "disabled")}>停用</button> : <button onClick={() => st(u.username, "active")}>启用</button>}</td></tr>)}
    </tbody></table>
    <h3>邀请</h3>
    <div className="row"><select value={role} onChange={e => setRole(e.target.value)}><option value="viewer">viewer（只读）</option><option value="admin">admin</option></select>
      <button onClick={invite}>生成邀请码</button></div>
    {code && <p className="warn-box">邀请码（只显示这一次，7 天有效、一次性）：<code>{code}</code></p>}
    <table><thead><tr><th>ID</th><th>角色</th><th>创建者</th><th>到期</th><th>状态</th><th></th></tr></thead><tbody>
      {data.invites.map((i: any) => { const revoked = String(i.used_by ?? "").startsWith("revoked:"); const expired = !i.used_by && new Date(i.expires_at) < new Date();
        return <tr key={i.id}><td><code>{i.id}</code></td><td>{i.role}</td><td>{i.created_by}</td><td>{i.expires_at}</td>
          <td>{revoked ? "已撤销" : i.used_by ? `已被 ${i.used_by} 使用` : expired ? "已过期" : "待使用"}</td>
          <td>{!i.used_by && !expired && <button className="danger" onClick={() => revoke(i.id)}>撤销</button>}</td></tr>; })}
    </tbody></table>
    {msg && <p className="err">{msg}</p>}
  </div>);
}

export function ReleasesView() {
  const { data, err, reload } = useLoad<any>("/api/releases");
  const [msg, setMsg] = useState("");
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const rb = async (to?: string) => {
    if (!window.confirm(`确定回滚到 ${to ?? "上一个版本"}？切换前会自动备份。`)) return;
    try { const r = await api<any>("/api/releases/rollback", { method: "POST", body: { confirm: true, to } }); setMsg(`${r.from} → ${r.to}` + (r.db_restore_required ? "（数据库迁移版本更高：需要从备份恢复数据库）" : "")); reload(); }
    catch (e: any) { setMsg(e.message); }
  };
  return (<div>
    <p className="meta">本地/模拟发布目标，不会部署到任何云端。当前版本：<b>{data.current ?? "无"}</b> <button disabled={data.releases.length < 2} onClick={() => rb()}>一键回滚</button></p>
    <table><thead><tr><th>版本</th><th>迁移</th><th>创建</th><th>文件</th><th>说明</th><th></th></tr></thead><tbody>
      {data.releases.map((r: any) => <tr key={r.version}><td>{r.version}{r.version === data.current && <> <Badge v="up" /></>}</td><td>{r.migration}</td><td>{r.created_at}</td><td>{Object.keys(r.files ?? {}).length}</td><td>{r.notes}</td>
        <td>{r.version !== data.current && <button onClick={() => rb(r.version)}>切换到此版本</button>}</td></tr>)}
    </tbody></table>
    <h3>历史</h3>
    <table><thead><tr><th>时间</th><th>动作</th><th>从</th><th>到</th><th>操作者</th></tr></thead><tbody>
      {data.history.map((h: any, k: number) => <tr key={k}><td>{h.ts}</td><td>{h.action}</td><td>{h.from ?? "-"}</td><td>{h.to}</td><td>{h.actor}</td></tr>)}
    </tbody></table>
    {msg && <p className="meta">{msg}</p>}
  </div>);
}

export function SelfcheckView() {
  const { data, err, reload } = useLoad<any>("/api/selfcheck");
  const [msg, setMsg] = useState("");
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  const drill = async () => { try { const r = await api<any>("/api/backup/drill", { method: "POST" }); setMsg(`演练成功：恢复了 ${r.files.length} 个文件`); reload(); } catch (e: any) { setMsg(e.message); } };
  return (<div>
    <div className="cards">
      <div className="kpi"><b>{data.score}</b><span>得分</span></div>
      <div className={`kpi ${data.fails ? "warn" : ""}`}><b>{data.fails}</b><span>不通过</span></div>
      <div className="kpi"><b>{data.warns}</b><span>警告</span></div>
    </div>
    <p><button onClick={drill}>运行备份恢复演练</button> <button onClick={reload}>重新检查</button> {msg && <span className="meta">{msg}</span>}</p>
    <table><thead><tr><th>检查项</th><th>结果</th><th>详情</th><th>建议</th></tr></thead><tbody>
      {data.checks.map((c: any) => <tr key={c.id}><td>{c.title}<br /><code className="meta">{c.id}</code></td><td><Badge v={c.status} /></td><td>{c.detail}</td><td>{c.fix}</td></tr>)}
    </tbody></table>
  </div>);
}

export function ContentView() {
  const [q, setQ] = useState(""); const [path, setPath] = useState("/api/content");
  const { data, err } = useLoad<any>(path, [path]);
  if (err) return <p className="err">{err}</p>;
  if (!data) return <p>加载中…</p>;
  if (!data.configured) return <p className="warn-box">未配置内容库（config: database.index + index_token_ref）。</p>;
  const rows = data.search ?? data.recent;
  return (<div>
    <p className="meta">只读视图：直接读取站点内容库（只读 token），面板不会写入。</p>
    <div className="cards"><div className="kpi"><b>{data.works}</b><span>作品</span></div>
      {data.licenses.slice(0, 4).map((l: any) => <div className="kpi" key={l[0]}><b>{l[1]}</b><span>{l[0]}</span></div>)}</div>
    <form className="row" onSubmit={e => { e.preventDefault(); setPath(`/api/content?q=${encodeURIComponent(q)}`); }}>
      <input placeholder="搜索（中文/英文）" value={q} onChange={e => setQ(e.target.value)} /><button type="submit">搜索</button></form>
    <table><thead><tr><th></th><th>标题</th><th>作者</th><th>许可</th></tr></thead><tbody>
      {rows.map((r: any) => <tr key={r[0]}><td>{r[5] && <img src={r[5]} alt="" width={64} loading="lazy" referrerPolicy="no-referrer" />}</td>
        <td><a href={r[4]} target="_blank" rel="noreferrer">{r[1]}</a></td><td>{r[2]}</td><td>{r[3]}</td></tr>)}
    </tbody></table>
  </div>);
}

export function SecurityView() {
  const { data, reload } = useLoad<any>("/api/auth/totp");
  const [setup, setSetup] = useState<any>(null); const [code, setCode] = useState(""); const [msg, setMsg] = useState("");
  if (!data) return <p>加载中…</p>;
  const start = async () => { try { setSetup(await api<any>("/api/auth/totp/setup", { method: "POST" })); setMsg(""); } catch (e: any) { setMsg(e.message); } };
  const act = async (what: string) => { try { await api(`/api/auth/totp/${what}`, { method: "POST", body: { code } }); setSetup(null); setCode(""); setMsg(what === "enable" ? "两步验证已启用" : "两步验证已关闭"); reload(); } catch (e: any) { setMsg(e.message); } };
  return (<div>
    <p>状态：{data.enabled ? <Badge v="up" /> : <Badge v="down" />} {data.enabled ? "已启用" : "未启用"}</p>
    <p className="meta">远程模式（panel.allow_remote）下，未启用两步验证的账号无法从非本机登录；启用/重置只能在本机进行。</p>
    {!data.enabled && !setup && <button onClick={start}>开始绑定身份验证器</button>}
    {setup && <div className="warn-box">在身份验证器中添加（只显示这一次）：<br /><code>{setup.secret}</code><br /><code className="meta">{setup.otpauth_uri}</code></div>}
    {(setup || data.enabled) && <div className="row"><input placeholder="6 位动态码" value={code} onChange={e => setCode(e.target.value)} maxLength={6} />
      {setup ? <button onClick={() => act("enable")}>确认启用</button> : <button className="danger" onClick={() => act("disable")}>关闭两步验证</button>}</div>}
    {msg && <p className="meta">{msg}</p>}
  </div>);
}


function Empty({ tip }: { tip: string }) {
  return <div className="empty-tip"><p>{tip}</p></div>;
}

function Confirm({ ok, setOk, label = "我已确认，执行此操作" }: { ok: boolean; setOk: (v: boolean) => void; label?: string }) {
  return <label className="confirm"><input type="checkbox" checked={ok} onChange={e => setOk(e.target.checked)} /> {label}</label>;
}

export function OpsView() {
  const { data: cat } = useLoad<any>("/api/ops/catalog");
  const { data: jobData, reload: reloadJobs } = useLoad<any>("/api/ops/jobs");
  const { data: packData, reload: reloadPacks } = useLoad<any>("/api/ops/packs");
  const [tab, setTab] = useState<"crawl" | "ingest" | "keys" | "cf" | "backup">("crawl");
  const [msg, setMsg] = useState(""); const [busy, setBusy] = useState(false); const [ok, setOk] = useState(false);
  const [crawl, setCrawl] = useState({ adapter: "synthetic", contact: "", categories: "", max_pages: 50, min_interval: 1, n: 30 });
  const [keyForm, setKeyForm] = useState({ label: "panel", prefix: "" });
  const [cf, setCf] = useState({ script: "galleryedge-staging", cred_file: "", cred_key: "index", wrangler_config: "", tag: "panel-1", message: "", to: "" });
  const [bak, setBak] = useState({ src: "", target_db: "", token_file: "" });

  useEffect(() => {
    const t = setInterval(reloadJobs, 2000);
    return () => clearInterval(t);
  }, [reloadJobs]);

  const run = async (path: string, body: Record<string, unknown>) => {
    setBusy(true); setMsg("");
    try {
      const r = await api<any>(path, { method: "POST", body: { ...body, confirm: true } });
      setMsg(`已排队任务 #${r.id}，可在下方查看进度`); setOk(false); reloadJobs();
    } catch (e: any) { setMsg(e.message); }
    finally { setBusy(false); }
  };

  if (!cat) return <p>加载中…</p>;

  return (<div className="ops">
    <p className="meta">所有运维动作都在这里点按钮完成，不用记命令。危险操作需勾选确认；密钥不会显示全文。</p>
    <div className="ops-tabs">
      {([["crawl", "① 抓取"], ["ingest", "② 入库"], ["keys", "③ API 密钥"], ["cf", "④ 发布/回滚"], ["backup", "⑤ 备份"]] as const)
        .map(([k, l]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => { setTab(k); setOk(false); setMsg(""); }}>{l}</button>)}
    </div>

    {tab === "crawl" && <div className="ops-panel">
      <p className="tip">{cat.tips.crawl}</p>
      <label>数据源<select value={crawl.adapter} onChange={e => setCrawl({ ...crawl, adapter: e.target.value })}>
        {cat.adapters.map((a: any) => <option key={a.id} value={a.id}>{a.label}</option>)}</select></label>
      <p className="meta">{cat.adapters.find((a: any) => a.id === crawl.adapter)?.tip}</p>
      {crawl.adapter !== "synthetic" && <label>联系方式（邮箱或网址）<input value={crawl.contact} onChange={e => setCrawl({ ...crawl, contact: e.target.value })} placeholder="you@example.com" /></label>}
      {crawl.adapter === "wikimedia-commons" && <label>分类（逗号分隔，留空=前几个白名单）
        <input value={crawl.categories} onChange={e => setCrawl({ ...crawl, categories: e.target.value })} placeholder="Featured_pictures_of_flowers,…" /></label>}
      <div className="row">
        <label>最多页数<input type="number" value={crawl.max_pages} onChange={e => setCrawl({ ...crawl, max_pages: Number(e.target.value) })} /></label>
        <label>间隔秒<input type="number" step="0.5" value={crawl.min_interval} onChange={e => setCrawl({ ...crawl, min_interval: Number(e.target.value) })} /></label>
        {crawl.adapter === "synthetic" && <label>条数<input type="number" value={crawl.n} onChange={e => setCrawl({ ...crawl, n: Number(e.target.value) })} /></label>}
      </div>
      <details><summary>白名单分类一览</summary><ul className="cat-list">{cat.commons_categories.map((c: string) => <li key={c}><code>{c}</code></li>)}</ul></details>
      <Confirm ok={ok} setOk={setOk} />
      <button disabled={!ok || busy} onClick={() => run("/api/ops/crawl", crawl)}>开始抓取</button>
    </div>}

    {tab === "ingest" && <div className="ops-panel">
      <p className="tip">{cat.tips.ingest}</p>
      {!packData?.packs?.length ? <Empty tip="还没有数据包。先去「① 抓取」生成，或把 .tar 放到 packs 目录。" /> :
        <table><thead><tr><th>包</th><th>大小</th></tr></thead><tbody>
          {packData.packs.map((p: any) => <tr key={p.path}><td><code>{p.name}</code></td><td>{fmtBytes(p.size)}</td></tr>)}
        </tbody></table>}
      <button type="button" className="ghost" onClick={reloadPacks}>刷新列表</button>
      <Confirm ok={ok} setOk={setOk} label="确认把以上包写入内容库（测试库）" />
      <button disabled={!ok || busy || !packData?.packs?.length} onClick={() => run("/api/ops/ingest", {})}>全部入库</button>
    </div>}

    {tab === "keys" && <div className="ops-panel">
      <p className="tip">{cat.tips.keys}</p>
      <label>备注<label /><input value={keyForm.label} onChange={e => setKeyForm({ ...keyForm, label: e.target.value })} /></label>
      <Confirm ok={ok} setOk={setOk} />
      <div className="row">
        <button disabled={!ok || busy} onClick={() => run("/api/ops/keys/init", {})}>初始化用量表</button>
        <button disabled={!ok || busy} onClick={() => run("/api/ops/keys/create", { label: keyForm.label })}>创建密钥</button>
      </div>
      <label>撤销（填 prefix）<input value={keyForm.prefix} onChange={e => setKeyForm({ ...keyForm, prefix: e.target.value })} placeholder="如 abcd1234" /></label>
      <button className="danger" disabled={!ok || busy || !keyForm.prefix} onClick={() => run("/api/ops/keys/revoke", { prefix: keyForm.prefix })}>撤销密钥</button>
    </div>}

    {tab === "cf" && <div className="ops-panel">
      <p className="tip">{cat.tips.cf}</p>
      <label>Worker<select value={cf.script} onChange={e => setCf({ ...cf, script: e.target.value })}>
        {cat.cf_scripts.map((s: string) => <option key={s}>{s}</option>)}</select></label>
      <label>凭据文件路径<input value={cf.cred_file} onChange={e => setCf({ ...cf, cred_file: e.target.value })} placeholder="/home/…/cf-api.json" /></label>
      <label>凭据键（可选）<input value={cf.cred_key} onChange={e => setCf({ ...cf, cred_key: e.target.value })} /></label>
      <label>wrangler.toml 路径（发布时需要）<input value={cf.wrangler_config} onChange={e => setCf({ ...cf, wrangler_config: e.target.value })} /></label>
      <div className="row">
        <label>版本标签<input value={cf.tag} onChange={e => setCf({ ...cf, tag: e.target.value })} /></label>
        <label>回滚到（版本 id，可空=上一版）<input value={cf.to} onChange={e => setCf({ ...cf, to: e.target.value })} /></label>
      </div>
      <Confirm ok={ok} setOk={setOk} label="确认：只操作 galleryedge-*，不动生产" />
      <div className="row">
        <button disabled={!ok || busy} onClick={() => run("/api/ops/cf/list", cf)}>查看版本</button>
        <button disabled={!ok || busy || !cf.wrangler_config} onClick={() => run("/api/ops/cf/deploy", cf)}>发布</button>
        <button className="danger" disabled={!ok || busy} onClick={() => run("/api/ops/cf/rollback", cf)}>回滚</button>
      </div>
    </div>}

    {tab === "backup" && <div className="ops-panel">
      <p className="tip">{cat.tips.backup}</p>
      <Confirm ok={ok} setOk={setOk} />
      <button disabled={!ok || busy} onClick={() => run("/api/ops/db/dump", {})}>导出内容库</button>
      <label>备份文件<input value={bak.src} onChange={e => setBak({ ...bak, src: e.target.value })} placeholder="…/index-….jsonl.gz" /></label>
      <label>目标空库 URL<input value={bak.target_db} onChange={e => setBak({ ...bak, target_db: e.target.value })} placeholder="libsql://… 或 file:…" /></label>
      <label>目标库 token 文件<input value={bak.token_file} onChange={e => setBak({ ...bak, token_file: e.target.value })} /></label>
      <button className="danger" disabled={!ok || busy || !bak.src || !bak.target_db} onClick={() => run("/api/ops/db/restore", bak)}>恢复到空库</button>
    </div>}

    {msg && <p className={msg.includes("失败") || msg.includes("error") ? "err" : "meta"}>{msg}</p>}

    <h3>任务进度</h3>
    {!jobData?.jobs?.length ? <Empty tip="还没有任务。在上方选一个操作并确认执行。" /> :
      <table><thead><tr><th>#</th><th>类型</th><th>状态</th><th>进度</th><th>说明</th><th>时间</th></tr></thead><tbody>
        {jobData.jobs.map((j: any) => <tr key={j.id}>
          <td>{j.id}</td><td>{j.kind}</td><td><Badge v={j.status === "done" ? "up" : j.status === "error" ? "down" : "warn"} /></td>
          <td>{Math.round((j.progress || 0) * 100)}%</td><td>{j.message}</td><td className="meta">{j.updated_at}</td></tr>)}
      </tbody></table>}
  </div>);
}

export function App() {
  const [auth, setAuth] = useState<any>(null);
  const check = useCallback(() => { api("/api/auth/state").then(setAuth).catch(() => setAuth({ user: null, needs_setup: false })); }, []);
  useEffect(check, [check]);
  if (!auth) return <p>加载中…</p>;
  if (!auth.user) return <LoginView needsSetup={auth.needs_setup} onDone={check} />;
  return <Shell user={auth.user} onLogout={async () => { await api("/api/auth/logout", { method: "POST" }); check(); }} />;
}

function Shell({ user, onLogout }: { user: any; onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1) as Tab) || "status");
  useEffect(() => { location.hash = tab; }, [tab]);
  const View = { status: StatusView, packs: PacksView, storage: StorageView, alerts: AlertsView, cleanup: CleanupView, events: EventsView,
    config: ConfigView, users: UsersView, releases: ReleasesView, selfcheck: SelfcheckView, content: ContentView, security: SecurityView, ops: OpsView }[tab] ?? StatusView;
  return (<div className="app">
    <aside><h1>GalleryEdge<small>运维面板 · 图形化操作</small></h1>
      <nav>{TABS.map(([k, label]) => <button key={k} className={k === tab ? "on" : ""} onClick={() => setTab(k)}>{label}</button>)}</nav>
      <div className="who">{user.username} · {user.role}<br /><button onClick={onLogout}>退出登录</button></div>
    </aside>
    <main><h2>{(TABS.find(t => t[0] === tab) ?? TABS[0])[1]}</h2><View /></main>
  </div>);
}
