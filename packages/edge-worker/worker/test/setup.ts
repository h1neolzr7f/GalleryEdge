// Starts a throwaway sqld (unless GE_TEST_LIBSQL_URL is set) and seeds it with the
// *Python* pipeline, so the Worker is contract-tested against the same schema.
import { spawn, execFileSync, ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join, resolve } from "node:path";
import net from "node:net";

let proc: ChildProcess | undefined;
const port = () => new Promise<number>(r => { const s = net.createServer().listen(0, "127.0.0.1", () => { const p = (s.address() as net.AddressInfo).port; s.close(() => r(p)); }); });

export async function setup({ provide }: any) {
  let url = process.env.GE_TEST_LIBSQL_URL;
  if (!url) {
    const bin = process.env.SQLD_BIN || join(homedir(), ".turso/sqld");
    if (!existsSync(bin)) throw new Error("set GE_TEST_LIBSQL_URL or SQLD_BIN");
    const p = await port();
    proc = spawn(bin, ["--http-listen-addr", `127.0.0.1:${p}`, "--db-path", join(mkdtempSync(join(tmpdir(), "ge-sqld-")), "db")], { stdio: "ignore" });
    url = `http://127.0.0.1:${p}`;
    for (let i = 0; i < 100; i++) { try { if ((await fetch(url + "/health")).ok) break; } catch { /* wait */ } await new Promise(r => setTimeout(r, 100)); }
  }
  const root = resolve(__dirname, "../../../..");
  const py = process.env.PYTHON || "python3";
  const env = { ...process.env, PYTHONPATH: ["packages/panel", "packages/demo-data", "packages/db-turso", "packages/edge-worker"].map(p => join(root, p)).join(":") };
  execFileSync(py, ["-m", "galleryedge_edge.demo", "--db", url, "seed", "-n", "80", "--seed", "11"], { cwd: root, env, stdio: "inherit" });
  const key = execFileSync(py, ["-m", "galleryedge_edge.demo", "--db", url, "key"], { cwd: root, env }).toString().trim().split("\n").pop()!;
  provide("url", url);
  provide("key", key);
}

export async function teardown() { proc?.kill(); }
