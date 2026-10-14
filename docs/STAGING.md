# Phase 7 — 接入现网（预发布）/ production integration (staging)

## 原则 / Principles
- 现网 Worker 和现网库**不替换、不写入**。GalleryEdge 部署成新的 Worker（`galleryedge-staging`），表都带 `ge_` 前缀。
- 现有的 aitag 包、账本和 `import.sqlite` 只读打开（`mode=ro&immutable=1`），转换结果写成新文件。
- 面板通过 `read_only: true` 的后端观察真实目录：不写探测文件、不建目录、不迁移、不清理。

## 工具 / Tools
```bash
ge-aitag inspect /path/to/export/aitag-nai-*.MANIFEST.json     # 校验 aitag 包清单，并列出与 site-pack-v1 的差异
ge-aitag convert /path/import.sqlite --pack-id aitag-nai-0037 --out /new/dir/ge-0037.tar --limit 2000
GE_LIBSQL_TOKEN=$(cat token) ge-ingest --db libsql://<db> --table-prefix ge_ --panel-db file:panel.db ge-0037.tar
```

## 中文搜索 / Chinese search
- Turso Cloud（libSQL 3.47）**不支持** Turso 原生的 `CREATE INDEX ... USING fts`（实测语法错误），所以继续用 FTS5。
- FTS5 的 `unicode61` 会把一串连续的汉字当成一个词。现在每个作品多存一个 `search_text` 列，汉字之间加空格；查询时中文词变成短语（`"胡 桃"`）。实测结果和 `LIKE` 子串匹配完全一致，单次查询只读几行。
- 原生 FTS 方言的代码保留着，等 Turso 支持后再用 `dialect.detect()` 自动启用。

## 切换计划 / Cutover plan (needs explicit go-ahead)
1. 全量转换 0001–0037：要先拿到 0001–0036 的 `import.sqlite`。预计写入约 3,000 万行，超出 Turso 免费档每月 1,000 万行的写入额度，需要升级或分月导入。
2. 新建一个专用 Turso 库（需要 Turso 平台登录），做一次全量导入，在 staging 上并行验证一周。
3. 给现网 Worker 加功能开关，或者把路由切到新 Worker。旧 Worker 保留不删，可以一键切回。
4. 回滚：路由切回旧 Worker，现网库原样不动。

## Phase 7c — 合规内容源：Wikimedia Commons
用户决定：staging 不再展示 aitag 原图（已下线：服务绑定已解除，`ge_*` 表已删除，原有表核对未改动）。

- 适配器 `wikimedia-commons`（`packages/crawler-adapters/galleryedge_adapters/commons.py`）：
  - 只抓 SFW 白名单分类（风景、山、水体、森林、海岸、火山、洞穴、花园、公园、岛屿、中国、日本、台湾、香港、鸟、花、灯塔、桥）里的精选图片，不递归进子分类。
  - 许可只收 CC0、公有领域、CC BY、CC BY-SA；NC、ND、仅 GFDL 和非自由文件一律跳过。
  - 文件名、描述或分类里出现敏感词就跳过（`sfw-blocked`）。
  - **robots**：Commons 的 robots.txt 对通用爬虫禁止 `/w/`（包括 `api.php`），所以只读 robots 允许的 `/wiki/Category:` 和 `/wiki/File:` 页面。速率每秒不超过 1 次，UA 带联系方式；网络错误重试一次后跳过。
  - 每条记录带作者、许可、许可链接、来源 URL、署名文本；英文分类会补少量中文标签（词表），并在 rights_note 里注明。
- 图片：不下载、不进 R2，按 Commons 的政策热链 500px 缩略图（`upload.wikimedia.org`），作品页显示署名和许可链接。CSP 只额外放行这一个域名。
```bash
ge-crawl --adapter wikimedia-commons --contact <你的邮箱或 URL> --out ./packs --max-pages 1750
```

### 爬虫实测中修复的问题（回归测试在 `tests/test_commons_adapter.py`）
1. 网络超时（URLError）会让整个爬虫崩溃 → 现在重试一次（加 5 秒退避），再失败就记为 `network-error` 跳过；进度可以断点续跑。
2. 429/503 以前直接跳过 → 现在遵守 `Retry-After`，没有就指数退避（上限 300 秒，默认重试 3 次），并发出 `crawler.backoff` 事件。
3. 长时间爬取时没有任何输出 → stderr 每 N 页打印一行进度（页数、记录数、跳过数、队列长度），续跑时会提示"resuming from page …"。有 `--quiet` 和 `--progress-every`。
4. `--contact` 不合法时直接抛 traceback → 改成一句明确的报错；commons 适配器强制 `--min-interval ≥ 1`。
5. `ge-ingest` 只能从环境变量读 token → 新增 `--token-file`（纯文本，或 JSON 里的 rw_token/auth_token），避免 token 出现在命令行或 shell 历史里。
6. 多词标签被空格拆散（"Clouds and blue sky" 变成 4 个词）→ `tags_text` 改用 ` | ` 连接，Python 和 TS 两端都兼容旧数据。
7. 作者字段混进了许可证全文和 TemplateStyles 的 CSS → 新增 `clean_author` 并在 HTML 转文本时去掉 `<style>`；新增 `normalize_record`，已抓到的记录可以离线按新规则重新清洗，不用重爬。
8. 中文词表漏掉了复数形式（volcanoes）→ 改用 `(?:e?s)?` 匹配。
9. SFW 规则收紧后可以离线复核（`sfw_ok`），新增屏蔽词 shunga、bijin-ga、bathing 等。
- 已知限制：Commons 分类页的翻页链接在 `/w/index.php` 下，被 robots 禁止，所以每个分类最多只能拿到 200 个文件；要多抓只能增加白名单分类。
