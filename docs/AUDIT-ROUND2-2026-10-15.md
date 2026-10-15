# GalleryEdge 第二轮扩大安全/稳定性审计（2026-10-15）

**范围**：`main@a720672`（v1.0.0）的 storage-router、WebDAV、crawler-core、pack-format、ingest、setup、运维面板权限与归档。上轮修复参见 PR #28。本轮修复分支 `fix/storage-crawler-security-20261015`，PR #29。

**证据标准**：只有能够定位源码执行路径并有明确失败条件的缺陷标为“已证实”；尚不能证明生产环境已暴露的部署/架构风险单独记录。未对真实部署或第三方站点执行安全探测。

## 已修复 / 已提交（PR #29）

| 严重程度 | 缺陷 | 代码与回归验证 |
|---|---|---|
| 高 | 本地 backend 仅禁止 `../`，仍可通过存储根内的符号链接读写根外文件 | `galleryedge_storage.LocalBackend._p` 解析并校验真实路径；`list/usage` 排除外部符号链接；`test_local_backend_refuses_symlink_escape` |
| 高 | WebDAV Basic Authorization 使用默认 urllib redirect，跨源跳转时可能向另一服务发送认证头 | `_SameOriginRedirect` 限定 scheme/host/port；`test_webdav_never_redirects_basic_auth_to_another_origin` |
| 中高 | WebDAV `PROPFIND` 的非本目录 href 被当作相对条目；递归遍历缺少循环/目录数边界 | `_propfind` 校验绝对 origin、路径前缀及 Depth=1 子级，`_walk` 有访问上限；`test_webdav_directory_entries_must_stay_inside_base_path` |
| 高 | 冷迁移、读回热在异步写入中使用无条件索引 UPDATE，可能指向旧副本并删除新副本 | `migrate/_promote` 使用 backend/hash/state 条件更新，失败不删除源；相应并发回归测试 |
| 中 | 本地 head 对多 GB 文件调用 `read_bytes()` 整包驻留内存 | `LocalBackend.head` 分块哈希 |
| 中 | Crawler 对 HTTPError 响应无限制 `.read()` | 与成功响应一样限制错误体读取到 5MB；`test_crawler_limits_error_response_bytes` |
| 中 | 跨主机 3xx 后仍沿用之前主机的 Crawl-delay/min_interval | 每跳重新计算 host/robots/delay；`test_redirect_uses_destination_host_rate_limit` |
| 中 | 首次安装在管理员密码、vault 口令或 token 文件预校验前写入部分文件 | setup 先检查输入与 token 文件、再创建目录及配置；错误输入无残留文件回归测试 |

## 已提交到 Issue、需要进一步设计/验收

- [#30 反代本机地址下远程 TOTP 的信任边界](https://github.com/h1neolzr7f/GalleryEdge/issues/30)：`allow_remote` 不应通过直接对等地址判断用户是否真正本机，建议远程模式全程强制 TOTP 并提供安全的本地登记/恢复路径。
- [#31 pack/panel-backup 归档解析的无界内存与重复成员](https://github.com/h1neolzr7f/GalleryEdge/issues/31)：需流式媒体接口和成员预算，不应简单限制合法 3GB 大包。
- [#32 跨进程对象存储写入/迁移一致性](https://github.com/h1neolzr7f/GalleryEdge/issues/32)：PR #29 的索引 CAS 只能消除已经复现的一类竞态，仍需版本化不可变对象 key、租约或垃圾回收协议来消除 DB 更新与 blob 删除之间的剩余竞态；路径 `resolve` 与真正打开也存在 TOCTOU。
- [#33 WebDAV 明文 HTTP Basic 凭据](https://github.com/h1neolzr7f/GalleryEdge/issues/33)：非本机应优先 HTTPS；既有 HTTP 用户需迁移及明确告警。

## 其他观察与后续检查

1. `panel/server.py` 的 GET 路由权限按“已登录”执行，普通 viewer 可以查看运维状态、配置的脱敏版本和任务元数据；建议重新制定数据级 RBAC：哪些路径、记录、后台任务结果可以被 viewer 看见，避免在后续加入可读的密钥返回字段时越权。
2. `pack-format` 的 `robots='allowed'` 是来自 pack 的声明，不是 ingestion 侧重新进行 robots 证明；适合由可信 crawler 生产的 pack，第三方上传 pack 应明确不作为已独立核验的授权证据。
3. `WebDAV` 的 `PROPFIND` 有目录数上限，响应体大小仍未独立限制；对不可靠服务端要增加流读取总上限、XML 解析预算与更丰富的故障测试。
4. `cleanup`、`Router.migrate`、`setup.apply` 存在需要真实目录权限/数据库超时/进程崩溃测试的边界；单元测试通过并不构成完整的系统安全结论。

## 验收与回滚

```bash
pip install -e ".[dev,panel,storage]"
pytest -q
cd packages/edge-worker/worker && npm ci && npm run typecheck && npm test
```

GitHub Actions 的 Python 3.10/3.12、S3 兼容、前端及泄密检查须全绿后再合并 PR。PR #29 不自动部署。修复中 `WebDAVBackend` 跨源 HTTP 跳转从“自动跟随”变为拒绝，若第三方 WebDAV 依赖跨域 CDN 跳转读取，将收到存储错误，需要服务端将文件留在同源或使用不携带 Basic 凭据的专门下载链路。

**审计结论**：已证实/修复多个新路径边界和一致性缺陷；仍不能声称生产环境“无漏洞”，尤其不要在未处理 #30/#31/#32 前承诺公网远程运维、任意 3GB 归档输入或多写入者并发迁移绝对安全。
