import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CleanupView, PacksView, AlertsView } from "../src/App";

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
