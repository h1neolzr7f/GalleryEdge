# 运维手册（Phase 1）/ Ops guide

- **健康检查** `ge-ops health`：local（存在、可写、用量/配额）、http、r2/s3（未签名 HEAD，401/403 视为可达）、libsql（`SELECT 1`）。状态 up → down 时告警一次，恢复时再告警一次；`alerts.fail_threshold` 控制连续失败几次才算 down。建议用 cron / systemd timer 每 5 分钟跑一次。
- **告警**：总是写进面板库（`ge-ops alerts`，`--ack ID` 确认）；配置 `alerts.webhook_url_ref` 后同时 POST JSON 到 Webhook，`webhook_hmac_ref` 可加 `X-GalleryEdge-Signature: sha256=...` 签名。同 key 在 `dedupe_minutes` 内去重。Webhook 失败不影响面板告警。
- **凭据库** `ge-ops vault set|get|list|rotate|expiring|delete|rekey`：AES-256-GCM + scrypt，口令来自 `GE_VAULT_PASSPHRASE`（≥12 字符）。值从 stdin / 隐藏输入读取，不走命令行参数；`list`/`get` 只显示掩码，`--reveal` 才输出明文并写审计。`--expires-days` 设置到期，`health` 会对 `remind_days` 内到期的凭据告警。配置里只写 `vault:<name>` 引用，内联密钥会被校验拒绝。
- **备份/恢复** `ge-ops backup create X.tar.gz` / `verify` / `restore X --to DIR`：包含配置、面板库（SQLite 在线快照）和加密库（保持加密），带 SHA-256 清单；恢复只写入空目录，拒绝路径穿越和被篡改的包。
- **清理** `ge-ops cleanup plan` / `apply --yes`：不加 `--yes` 一律 dry-run。只删 `.verified_remote` 标记的包目录里的文件，以及带匹配 size+sha256 的 `.uploaded` 标记的 tar。账本（done.txt / seen.txt / packed_works.txt / prompts.jsonl）、manifest、`.vault`、未上传 tar、`staging/` 与带 `.inprogress`/`.lock` 的目录、符号链接永不删除；每个文件删除前再检查一次。
- **审计/事件** `ge-ops audit` / `ge-ops events`：所有写入都经过脱敏。
- **数据库**：主库 Turso/libSQL（`libsql://` 需 `pip install galleryedge[libsql]`，令牌用 `auth_token_ref`）；本地开发和测试用 SQLite 文件，SQL 与 libSQL 兼容。


## API 配额并发保护（v1.0.1 修复）

分钟/日配额使用数据库触发器保护每次计数写入，避免多个并发请求在读取旧额度后同时超额放行；超额事务会回滚并返回 429。
**升级已经存在的独立 usage 数据库必须先运行** `ge-keys --db <usage-url> --token-file <rw-token-file> init`，让触发器就位，再开放高并发 API 流量。主内容库的无前缀本地测试在 `schema.init` 时创建相同保护；已有数据库也应先运行初始化迁移。
