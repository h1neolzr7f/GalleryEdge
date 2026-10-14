"""Real Cloudflare release target: deploy Worker *versions* and roll back via the Workers Versions/Deployments API.

Only scripts named ``galleryedge-*`` are accepted (hard guard: never touches other Workers such as prod).
Upload of a new version is delegated to ``wrangler versions upload`` (injectable); activation and rollback
use ``POST /accounts/{id}/workers/scripts/{name}/deployments`` with a 100% single-version deployment.
The API token is read from a file and never printed.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
NAME_RE = re.compile(r"^galleryedge-[a-z0-9-]+$")


class CFError(Exception):
    pass


def _http(method: str, url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            j = json.load(e)
        except Exception:
            j = {}
        raise CFError(f"HTTP {e.code}: " + "; ".join(x.get("message", "") for x in j.get("errors", [])))


class CloudflareTarget:
    def __init__(self, account_id: str, script: str, token: str, *, http=None, uploader=None, log_path: str | Path | None = None):
        if not NAME_RE.match(script or ""):
            raise CFError(f"refusing script {script!r}: only galleryedge-* Workers may be managed")
        self.acct, self.script, self._tok = account_id, script, token
        self.http, self.uploader = http or (lambda *a, **k: _http(*a, **k)), uploader
        self.log = Path(log_path) if log_path else None

    def _u(self, p: str) -> str:
        return f"{API}/accounts/{self.acct}/workers/scripts/{self.script}{p}"

    def versions(self) -> list[dict]:
        r = self.http("GET", self._u("/versions"), self._tok)
        items = r.get("result", {}).get("items", r.get("result") or [])
        return [{"id": v["id"], "number": v.get("number"), "created_on": (v.get("metadata") or {}).get("created_on"),
                 "tag": (v.get("annotations") or {}).get("workers/tag"), "message": (v.get("annotations") or {}).get("workers/message")}
                for v in items]

    def deployments(self) -> list[dict]:
        r = self.http("GET", self._u("/deployments"), self._tok)
        return r.get("result", {}).get("deployments", [])

    def current_version(self) -> str | None:
        d = self.deployments()
        if not d:
            return None
        latest = max(d, key=lambda x: x.get("created_on", ""))
        vs = sorted(latest.get("versions", []), key=lambda v: -v.get("percentage", 0))
        return vs[0]["version_id"] if vs else None

    def activate(self, version_id: str, *, action: str, message: str = "") -> dict:
        known = {v["id"] for v in self.versions()}
        if version_id not in known:
            raise CFError(f"unknown version {version_id}")
        prev = self.current_version()
        body = {"strategy": "percentage", "versions": [{"version_id": version_id, "percentage": 100}],
                "annotations": {"workers/message": (message or f"GalleryEdge {action}")[:100]}}
        self.http("POST", self._u("/deployments"), self._tok, body)
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, "script": self.script,
              "from": prev, "to": version_id}
        if self.log:
            with self.log.open("a") as f:
                f.write(json.dumps(ev) + "\n")
        return ev

    def deploy(self, tag: str, message: str = "") -> dict:
        """Upload a new version (wrangler versions upload) and route 100% traffic to it."""
        if not self.uploader:
            raise CFError("no uploader configured")
        self.uploader(tag, message)
        match = [v for v in self.versions() if v.get("tag") == tag]
        if not match:
            raise CFError(f"uploaded version with tag {tag} not found")
        return self.activate(max(match, key=lambda v: v.get("number") or 0)["id"], action="deploy", message=message or tag)

    def rollback(self, to: str | None = None) -> dict:
        cur = self.current_version()
        vs = sorted(self.versions(), key=lambda v: v.get("number") or 0)
        if to is None:
            ids = [v["id"] for v in vs]
            if cur not in ids or ids.index(cur) == 0:
                raise CFError("no previous version to roll back to")
            to = ids[ids.index(cur) - 1]
        if to == cur:
            raise CFError("already on that version")
        return self.activate(to, action="rollback", message=f"rollback from {cur}")


def wrangler_uploader(wrangler: str, config: str, env: dict, cwd: str):
    def up(tag: str, message: str):
        r = subprocess.run([wrangler, "versions", "upload", "--config", config, "--tag", tag, "--message", message or tag],
                           env=env, cwd=cwd, capture_output=True, text=True)
        if r.returncode:
            raise CFError("wrangler versions upload failed: " + (r.stderr or r.stdout)[-400:])
    return up


def main(argv=None) -> int:
    import argparse, os
    ap = argparse.ArgumentParser(prog="ge-cf-release", description="Cloudflare Worker versions: list / deploy / rollback (galleryedge-* only)")
    ap.add_argument("--script", required=True)
    ap.add_argument("--cred-file", required=True, help='JSON {"api_token","account_id"} (optionally nested under --cred-key)')
    ap.add_argument("--cred-key")
    ap.add_argument("--wrangler-config"); ap.add_argument("--wrangler", default="wrangler"); ap.add_argument("--log")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    d = sp.add_parser("deploy"); d.add_argument("--tag", required=True); d.add_argument("--message", default="")
    r = sp.add_parser("rollback"); r.add_argument("--to")
    a = ap.parse_args(argv)
    c = json.loads(Path(a.cred_file).read_text())
    c = c[a.cred_key] if a.cred_key else c
    up = None
    if a.wrangler_config:
        env = {**os.environ, "CLOUDFLARE_API_TOKEN": c["api_token"], "CLOUDFLARE_ACCOUNT_ID": c["account_id"], "WRANGLER_SEND_METRICS": "false"}
        up = wrangler_uploader(a.wrangler, a.wrangler_config, env, str(Path(a.wrangler_config).parent))
    t = CloudflareTarget(c["account_id"], a.script, c["api_token"], uploader=up, log_path=a.log)
    try:
        if a.cmd == "list":
            out = {"current": t.current_version(), "versions": t.versions()[:20]}
        elif a.cmd == "deploy":
            out = t.deploy(a.tag, a.message)
        else:
            out = t.rollback(a.to)
    except CFError as e:
        print(f"error: {e}"); return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
