# GalleryEdge 白皮书 / Whitepaper

> 版本 0.6 · 2026-10 · 中文在前，English below.

## 中文

### 1. 是什么
GalleryEdge（边缘图阁）是一个**自托管**的图集归档与边缘分发工具包：
你自己的机器负责抓取（只针对你有权使用的源）、打包、校验和上传；索引存在 Turso/libSQL；图片放在你选择的存储（本地、S3 兼容、R2）；
用 Cloudflare Worker 在边缘提供只读浏览。项目**不带任何预设站点、不带真实图片、不自动注册账号**。

### 2. 架构
- `crawler-core`：限速，强制遵守 robots（关不掉），断点续抓，账本只增不删。
- `pack-format`：site-pack-v1，每个文件都有 sha256 清单。
- `ingest`：把包写进索引，自动上报 Turso 行读写量。
- `storage-router`：冷热分层、迁移（先预览，确认令牌才执行）、容量和成本预测。
- `edge-worker`：TypeScript Worker，只读，带 FTS 搜索。
- `panel`：FastAPI + React 的本机运维面板。

### 3. 安全设计
| 方面 | 默认做法 |
|---|---|
| 网络 | 面板只监听 127.0.0.1，检查 Host，写请求必须带 `X-GE-Panel` 头（防 CSRF/DNS rebinding）。要远程访问必须显式打开 `allow_remote`，而且仍然要登录 |
| 登录 | 所有 API 都要会话。密码用 scrypt 哈希，至少 12 位；连续 5 次失败锁定 15 分钟；会话 12 小时过期，空闲 2 小时过期；Cookie 是 HttpOnly + SameSite=Strict，走 https 时加 Secure |
| 账号 | 只能邀请注册。邀请码一次性、有期限，数据库只存哈希；不能停用自己或最后一个管理员；viewer 只读 |
| 密钥 | 配置文件里只写 `vault:<名字>`；密钥存在加密保险库（scrypt + AES-GCM）里，页面和日志只显示掩码，设置和轮换都记审计 |
| 变更 | 清理、迁移、跳过、回滚都要先预览再确认；配置修改先校验，原子写入，并保留历史版本 |
| 发布 | 版本不可改，每个文件都校验 sha256；切换前自动备份；回滚到迁移版本更低的发布时，会标记"需要恢复数据库" |
| 数据 | 账本（done/seen/packed_works/prompts.jsonl）、还没上传的 tar、正在写的 staging 永远不会被清理 |

### 4. 合规
- 只抓有授权或明确许可的内容。每条记录必须带 `license` 和 `source_url`，自检页会检查这一点。
- robots.txt 拿不到的站点直接跳过，并在面板里提示。
- 没有内置第三方站点适配器。示例和测试只用合成数据。
- 安全自检页会给出得分，并列出每项不通过或警告的修复建议。备份恢复演练可以一键运行。

### 5. 运维
在 `ge-panel admin create` → `ge-panel --config config.json` → 配置中心 填端点/密钥 → `ge-panel release deploy <dir> --version x.y.z` → 出问题就在"发布"页一键回滚。

### 6. 限制
- 发布目标目前只有本地模拟，不会真的部署到 Cloudflare。
- Turso 原生 `USING fts` 方言只测了生成的 SQL。
- 回滚只切换程序，不会自动回滚数据库，需要手动从备份恢复。

## English

### 1. What
GalleryEdge is a **self-hosted** toolkit for archiving image collections and serving them from the edge:
- Your machine crawls (only sources you are entitled to), packs, verifies and uploads.
- The index lives in Turso/libSQL.
- Blobs go to the storage you pick: local, S3-compatible or R2.
- A Cloudflare Worker serves read-only browsing at the edge.

It ships **no site presets, no real images and no auto-registration**.

### 2. Architecture
- `crawler-core`: rate-limited, robots always enforced, resumable, append-only ledgers.
- `pack-format`: site-pack-v1 with per-file sha256.
- `ingest`: writes packs into the index and auto-reports Turso row usage.
- `storage-router`: hot/cold tiering, preview-then-confirm migration, cost and capacity forecasts.
- `edge-worker`: read-only TypeScript Worker with FTS.
- `panel`: local FastAPI + React ops panel.

### 3. Security design
| Area | Default |
|---|---|
| Network | Loopback bind, Host check and a required `X-GE-Panel` header on writes (against CSRF and DNS rebinding). Remote access needs an explicit `allow_remote` and still requires login. |
| Login | Every API needs a session. Passwords are hashed with scrypt and need at least 12 characters. Five failures in 15 minutes lock the account. Sessions expire after 12h, or after 2h idle. Cookies are HttpOnly with SameSite=Strict, plus Secure on https. |
| Accounts | Invite-only. Invites are single-use, expire, and only their hash is stored. You cannot disable yourself or the last admin. Viewers are read-only. |
| Secrets | Config files hold only `vault:<name>`. Secrets sit in an encrypted vault (scrypt + AES-GCM) and are shown as masks only. Every set and rotate is audited. |
| Changes | Cleanup, migration, skip and rollback all preview first and need confirmation. Config edits are validated, written atomically and versioned. |
| Releases | Releases are immutable and every file is sha256-verified. A backup is taken before each switch. Rolling back to an older migration flags that the DB must be restored. |
| Data | Ledgers, un-uploaded tars and in-progress staging are never cleaned. |

### 4. Compliance
- Only authorised or permissively licensed content. Every record needs `license` and `source_url`, and the self-check verifies this.
- Hosts with an unreachable robots.txt are skipped and reported in the panel.
- No third-party site adapters are bundled. Examples and tests use synthetic data only.
- The self-check page scores the setup and gives a fix for every fail or warning. The backup restore drill runs with one click.

### 5. Operating it
1. `ge-panel admin create`
2. `ge-panel --config config.json`
3. Enter endpoints and keys in the Config center.
4. `ge-panel release deploy <dir> --version x.y.z`
5. If something breaks, roll back with one click on the Releases page.

### 6. Limitations
- The release target is a local simulation only. Nothing is deployed to Cloudflare yet.
- For the Turso-native `USING fts` dialect, only SQL generation is tested.
- Rollback switches the code only. The database has to be restored from the backup by hand.
