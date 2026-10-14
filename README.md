# GalleryEdge · 边缘图阁

[![CI](https://github.com/h1neolzr7f/GalleryEdge/actions/workflows/ci.yml/badge.svg)](https://github.com/h1neolzr7f/GalleryEdge/actions/workflows/ci.yml)
License: MIT

**中文** | [English](#english)

给自己用的「边缘作品索引 + 采集入库运维面板」。目标是一个人也能长期管好一套检索系统：健康检查、告警、加密凭据、备份恢复、安全清理、审计、配置中心。主库为 **Turso（libSQL）**，D1 可选。仓库只带**合成数据**，不含任何真实作品或预设，也**不做任何账号自动注册**（见 [合规说明](docs/COMPLIANCE.md)）。

### 当前能做什么（Phase 1）
| 命令 | 作用 |
|---|---|
| `ge-ops config validate config.json` | 配置中心 schema 校验，错误逐项说明 |
| `ge-ops health` | 探测本地目录 / HTTP / libSQL 后端，状态变化触发告警 |
| `ge-ops alerts` | 查看面板内告警；Webhook 推送（可选 HMAC 签名） |
| `ge-ops vault set/get/list/rotate/expiring` | 加密凭据库（AES-GCM + scrypt），轮换、到期提醒、列表只显示掩码 |
| `ge-ops backup create/verify/restore` | 一键备份配置、面板库、加密库，带 SHA-256 清单，恢复到干净目录 |
| `ge-ops cleanup plan/apply` | 只删已确认上传的本地副本；账本、未上传 tar、进行中 staging 永不删；默认 dry-run |
| `ge-ops audit` | 审计日志 |
| `ge-demo-data -n 100` | 生成合成作品数据 |

### 快速开始
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
cp examples/config.example.json config.json
export GE_VAULT_PASSPHRASE='choose-a-long-passphrase'
ge-ops --config config.json health
```

路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)，架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## English

A **self-use** edge work index plus a one-person ops toolkit: health checks, alerts, encrypted credentials, backup/restore, safe cleanup, audit log and a validated config center. Primary DB is **Turso (libSQL)**; D1 is optional. The repo ships **synthetic data only** and contains **no account auto-registration** of any kind (see [Compliance](docs/COMPLIANCE.md)).

Phase 1 commands are listed in the table above (`ge-ops ...`). Quick start:

```bash
pip install -e ".[dev]" && pytest -q
cp examples/config.example.json config.json
GE_VAULT_PASSPHRASE='...' ge-ops --config config.json health
```

Safety guarantees: the panel binds to `127.0.0.1` by default, secrets are encrypted at rest and masked in all logs, cleanup never touches ledgers (`done.txt`, `seen.txt`, `packed_works.txt`, `prompts.jsonl`), un-uploaded tars or in-progress staging, and CI runs gitleaks on every PR.
