import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CleanupView, PacksView, AlertsView, StorageView } from "../src/App";

type Call = { url: string; init?: RequestInit };
let calls: Call[] = [];
function mockFetch(routes: Record<string, any>) {
  calls = [];
  (globalThis as any).fetch = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const key = Object.keys(routes).find(k => url.startsWith(k.split(" ")[1] ?? k) && (k.split(" ")[0] === (init?.method ?? "GET")));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: 200 });
  };
}

const plan = { token: "t1", bytes: 5, delete: [{ path: "/w/p/a.png", size: 5 }], protected: [{ path: "/w/done.txt", reason: "ledger" }] };

test("cleanup requires explicit confirm and sends the preview token", async () => {
  mockFetch({ "GET /api/cleanup/plan": plan, "POST /api/cleanup/apply": { deleted: 1, bytes: 5 } });
  render(<CleanupView />);
  await screen.findByText(/将删除 1 个文件/);
  expect(screen.getByText("ledger", { exact: false })).toBeTruthy();
  const btn = screen.getByText("执行清理") as HTMLButtonElement;
  expect(btn.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/确认删除/));
  expect(btn.disabled).toBe(false);
  fireEvent.click(btn);
  await waitFor(() => expect(calls.some(c => c.url === "/api/cleanup/apply")).toBe(true));
  const c = calls.find(c => c.url === "/api/cleanup/apply")!;
  expect(JSON.parse(String(c.init!.body))).toEqual({ token: "t1", confirm: true });
  expect((c.init!.headers as any)["X-GE-Panel"]).toBe("1");
});

test("pack board: retry only for failed packs, disabled in observe mode", async () => {
  const items = [{ pack_id: "p-err", stage: "verify", status: "error", progress: 0, error: "x", attempts: 0 },
                 { pack_id: "p-run", stage: "upload", status: "running", progress: 0.5, error: null, attempts: 0 }];
  mockFetch({ "GET /api/packs": { stages: ["download", "verify", "upload", "done"], items, takeover: false } });
  render(<PacksView />);
  await screen.findByText("p-err");
  const retry = screen.getAllByText("重试");
  expect(retry.length).toBe(1);
  expect((retry[0] as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/只读观察模式/)).toBeTruthy();
});

test("pack retry posts when takeover is on", async () => {
  mockFetch({ "GET /api/packs": { stages: ["verify", "done"], items: [{ pack_id: "p1", stage: "verify", status: "error", progress: 0, error: "e", attempts: 0 }], takeover: true }, "POST /api/packs/p1/retry": {} });
  render(<PacksView />);
  fireEvent.click(await screen.findByText("重试"));
  await waitFor(() => expect(calls.some(c => c.url === "/api/packs/p1/retry" && c.init?.method === "POST")).toBe(true));
});

test("alerts ack", async () => {
  mockFetch({ "GET /api/alerts": [{ id: 3, ts: "t", key: "backend.down.x", level: "error", message: "down", acked: 0, delivered: "panel" }], "POST /api/alerts/3/ack": { ok: true } });
  render(<AlertsView />);
  fireEvent.click(await screen.findByText("确认"));
  await waitFor(() => expect(calls.some(c => c.url === "/api/alerts/3/ack")).toBe(true));
});

test("storage page shows forecast and migration needs confirm", async () => {
  mockFetch({
    "GET /api/costs": { monthly_total_usd: 1.5, history: [], meters: [{ meter: "turso.rows_written", label: "Turso 行写入（本月）", unit: "rows", value: 9500000, quota: 10000000, ratio: 0.95, per_day: 100000, days_to_quota: 5, monthly_cost_usd: null, warn_ratio: 0.85 }] },
    "GET /api/storage/plan": { cold_backend: "cold", token: "tk", bytes: 3, moves: [{ key: "p/a.png", src: "hot", dst: "cold", size: 3 }], skipped: [{ key: "p/done.txt", reason: "ledger" }] },
    "POST /api/storage/migrate": { moved: ["p/a.png"], failed: [] },
  });
  render(<StorageView />);
  await screen.findByText("Turso 行写入（本月）");
  expect(screen.getByText("5 天", { selector: "span.errtxt" })).toBeTruthy();
  await screen.findByText("p/a.png");
  const btn = screen.getByText("执行迁移") as HTMLButtonElement;
  expect(btn.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/确认迁移/));
  fireEvent.click(btn);
  await waitFor(() => expect(calls.some(c => c.url === "/api/storage/migrate")).toBe(true));
  expect(JSON.parse(String(calls.find(c => c.url === "/api/storage/migrate")!.init!.body))).toEqual({ token: "tk", confirm: true });
});

import { App, ConfigView, ReleasesView, SelfcheckView, UsersView } from "../src/App";

test("unauthenticated app shows login, never the dashboard", async () => {
  mockFetch({ "GET /api/auth/state": { user: null, needs_setup: true } });
  render(<App />);
  await screen.findAllByText(/安装向导/);
  expect(screen.queryByText("包任务")).toBeNull();
});

test("config center shows masks only and posts secrets to the vault", async () => {
  mockFetch({ "GET /api/config": { path: "/c.json", endpoints: [{ path: "database.primary", value: "file:x.db" }], refs: [{ path: "backends.2.credential_ref", ref: "vault:r2", exists: true, mask: "FA…YZ", expires_at: null }], vault_unlocked: true, credentials: [{ name: "r2", mask: "FA…YZ", version: 1, rotated_at: "t" }] },
             "POST /api/vault/r2": { version: 2, mask: "FA…01" } });
  render(<ConfigView />);
  expect((await screen.findAllByText("FA…YZ")).length).toBe(2);
  fireEvent.change(screen.getByPlaceholderText("凭据名"), { target: { value: "r2" } });
  fireEvent.change(screen.getByPlaceholderText(/新值/), { target: { value: "FAKE-NEW" } });
  fireEvent.click(screen.getByText("保存 / 轮换"));
  await screen.findByText(/已保存（v2/);
  expect(document.body.textContent).not.toContain("FAKE-NEW");
});

test("rollback asks for confirmation and sends confirm:true", async () => {
  mockFetch({ "GET /api/releases": { current: "0.6.0", releases: [{ version: "0.6.0", migration: 3 }, { version: "0.5.0", migration: 2 }], history: [] },
             "POST /api/releases/rollback": { from: "0.6.0", to: "0.5.0", db_restore_required: true } });
  (window as any).confirm = () => false;
  render(<ReleasesView />);
  fireEvent.click(await screen.findByText("一键回滚"));
  expect(calls.some(c => c.url === "/api/releases/rollback")).toBe(false);
  (window as any).confirm = () => true;
  fireEvent.click(screen.getByText("一键回滚"));
  await screen.findByText(/需要从备份恢复数据库/);
  expect(JSON.parse(String(calls.find(c => c.url === "/api/releases/rollback")!.init!.body)).confirm).toBe(true);
});

test("users: invite code shown once; selfcheck renders score", async () => {
  mockFetch({ "GET /api/users": { users: [{ username: "admin", role: "admin", status: "active" }], invites: [] }, "POST /api/invites": { code: "inv_FAKE" } });
  render(<UsersView />);
  fireEvent.click(await screen.findByText("生成邀请码"));
  await screen.findByText("inv_FAKE");
  mockFetch({ "GET /api/selfcheck": { score: 80, fails: 1, warns: 1, checks: [{ id: "auth.admin", title: "管理员", status: "ok", detail: "1", fix: "" }] } });
  render(<SelfcheckView />);
  await screen.findByText("80");
});

import { LoginView, ContentView, SecurityView } from "../src/App";

test("login asks for TOTP when the server requires it, then sends otp", async () => {
  const bodies: any[] = [];
  (globalThis as any).fetch = async (url: string, init?: RequestInit) => {
    const b = JSON.parse(String(init?.body ?? "{}")); bodies.push(b);
    if (!b.otp) return new Response(JSON.stringify({ detail: "totp_required" }), { status: 401 });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  let done = false;
  render(<LoginView needsSetup={false} onDone={() => { done = true; }} />);
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "dick" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "FAKE-pass-123456" } });
  fireEvent.click(screen.getByText("登录"));
  const otp = await screen.findByLabelText(/动态码/);
  fireEvent.change(otp, { target: { value: "123456" } });
  fireEvent.click(screen.getByText("登录"));
  await waitFor(() => expect(done).toBe(true));
  expect(bodies[bodies.length - 1].otp).toBe("123456");
});

test("content view is read-only and searches", async () => {
  mockFetch({ "GET /api/content": { configured: true, read_only: true, works: 2, licenses: [["CC0", 2]],
    recent: [["w1", "FAKE flower", "FAKE", "CC0", "https://x.invalid/1", ""]], search: undefined } });
  render(<ContentView />);
  await screen.findByText("FAKE flower");
  expect(screen.getByText(/只读视图/)).toBeTruthy();
  expect(screen.queryByText(/删除|编辑/)).toBeNull();
});

test("2FA enrol flow posts the code to enable", async () => {
  mockFetch({ "GET /api/auth/totp": { enabled: false }, "POST /api/auth/totp/setup": { secret: "FAKESECRET", otpauth_uri: "otpauth://totp/x" },
              "POST /api/auth/totp/enable": { ok: true } });
  render(<SecurityView />);
  fireEvent.click(await screen.findByText("开始绑定身份验证器"));
  await screen.findByText("FAKESECRET");
  fireEvent.change(screen.getByPlaceholderText("6 位动态码"), { target: { value: "654321" } });
  fireEvent.click(screen.getByText("确认启用"));
  await waitFor(() => expect(calls.some(c => c.url === "/api/auth/totp/enable")).toBe(true));
  expect(JSON.parse(String(calls.find(c => c.url === "/api/auth/totp/enable")!.init!.body))).toEqual({ code: "654321" });
});

test("users page: change role and revoke invite", async () => {
  mockFetch({ "GET /api/users": { users: [{ username: "bob", role: "viewer", status: "active" }],
    invites: [{ id: "abcd1234", role: "viewer", created_by: "dick", expires_at: "2099-01-01T00:00:00+00:00", used_by: null }] },
    "POST /api/users/bob/role": { ok: true }, "POST /api/invites/abcd1234/revoke": { ok: true } });
  (globalThis as any).confirm = () => true;
  render(<UsersView />);
  fireEvent.change(await screen.findByLabelText("角色 bob"), { target: { value: "admin" } });
  await waitFor(() => expect(calls.some(c => c.url === "/api/users/bob/role")).toBe(true));
  expect(JSON.parse(String(calls.find(c => c.url === "/api/users/bob/role")!.init!.body))).toEqual({ role: "admin" });
  fireEvent.click(screen.getByText("撤销"));
  await waitFor(() => expect(calls.some(c => c.url === "/api/invites/abcd1234/revoke")).toBe(true));
});

import { OpsView } from "../src/App";

test("ops hub lists actions and requires confirm before crawl", async () => {
  mockFetch({
    "GET /api/ops/catalog": { adapters: [{ id: "synthetic", label: "演示数据（离线）", tip: "练手" }], commons_categories: ["Featured_pictures_of_flowers"], cf_scripts: ["galleryedge-staging"], tips: { crawl: "先试演示", ingest: "i", keys: "k", cf: "c", backup: "b" } },
    "GET /api/ops/jobs": { jobs: [] },
    "GET /api/ops/packs": { packs: [] },
    "POST /api/ops/crawl": { id: 7 },
  });
  render(<OpsView />);
  await screen.findByText(/演示数据/);
  expect(screen.getByText(/先试演示/)).toBeTruthy();
  const btn = screen.getByText("开始抓取") as HTMLButtonElement;
  expect(btn.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/我已确认/));
  expect(btn.disabled).toBe(false);
  fireEvent.click(btn);
  await waitFor(() => expect(calls.some(c => c.url === "/api/ops/crawl")).toBe(true));
  expect(JSON.parse(String(calls.find(c => c.url === "/api/ops/crawl")!.init!.body)).confirm).toBe(true);
});
