# 合规说明 / Compliance

## 中文
GalleryEdge 是**自用**的运维与检索工具链，不是内容站，也不提供公共图库服务。

1. **不自动注册账号**：不提供、也不接受任何批量注册云账号、收验证码、账号池、拼额度的代码。请使用你自己正规开通的 R2 / S3 / Turso / 本地盘。
2. **只带合成数据**：仓库不包含任何真实作品、抓取图片、画师词或角色预设。演示数据全部由 `ge-demo-data` 生成。
3. **采集默认合规**（Phase 4 起）：强制遵守 robots.txt、限速、诚实 User-Agent；遇到登录墙 / 付费墙 / 验证码只记录 `skip_reason`，不破解；默认只采元数据（`fetch_media=false`）；每条记录必须带 `source_url` 与 `license`。
4. **数据安全**：密钥只存加密库，日志自动脱敏；清理逻辑永不删除账本（`done.txt` / `seen.txt` / `packed_works.txt` / `prompts.jsonl`）、未上传的 tar 和进行中的 staging，且默认 dry-run。
5. 使用者需自行确保其采集与托管行为符合目标站点条款与当地法律。

## English
GalleryEdge is a **self-use** ops and search toolkit, not a content service.

1. No account auto-registration, OTP harvesting, account pools or quota pooling — ever. Bring your own legitimately provisioned resources.
2. Synthetic data only. No real works, scraped images, artist tags or presets ship in this repo.
3. Compliant collection by default (from Phase 4): robots.txt enforced, rate limits, honest UA, no bypassing login/paywalls/CAPTCHAs, metadata-only by default, `source_url` + `license` required.
4. Secrets are encrypted at rest and masked in logs. Cleanup never deletes ledgers, un-uploaded tars or in-progress staging, and defaults to dry-run.
5. You are responsible for complying with site terms and local law.
