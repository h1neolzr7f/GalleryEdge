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
4. 回滚：路由切回旧 Worker，`nai-gallery-v3` 原样不动。
