# 第三、四轮扩查：10 项新增缺陷与离线复现

日期：2026-10-15。审计仓库：`h1neolzr7f/GalleryEdge`。

本轮提交的是问题登记、复现代码和检查结果，**没有修改产品代码，没有修复这些问题，也没有部署**。P1/P2 是本项目的处理优先级，不是 CVSS 评分。

## 基线与去重

| 对象 | 核查快照 | 状态 |
| --- | --- | --- |
| main / v1.0.0 | `a72067220a0de6cf6635429a39e9778da117dab7` | 本轮源码基线 |
| PR #28 | `b17330396f1c29b70b18cc9516ef776d5ae2ad29` | 核查时 open，未合并 |
| PR #29 | `acb6868c80030c52666206205ddaef0c2ff2d5fe` | 核查时 open，未合并 |

读过 #28/#29 的完整审计文档与现有 #30–#33；没有重复提交已登记问题或将待合并修复当成新发现。下列 Python 复现还在两个 PR 的隔离源码快照中执行，缺陷仍存在；TS 的传输和路由探针执行于 main 源码。

## 第三轮：认证、传输、发布与敏感文件

| Issue | 优先级 | 确认的行为 | 触发边界 |
| --- | --- | --- | --- |
| [#34](https://github.com/h1neolzr7f/GalleryEdge/issues/34) | P1 | Python/TS 把非本机 HTTPS 数据库 URL 改为 HTTP，并继续附带 Bearer | URL 命中错误的 localhost/127.0.0.1 子串或前缀规则 |
| [#35](https://github.com/h1neolzr7f/GalleryEdge/issues/35) | P2 | 两个独立 SQLite 连接并发使用同一 OTP，签发两个会话 | 需要正确密码及有效 OTP；不是密码单因素绕过 |
| [#36](https://github.com/h1neolzr7f/GalleryEdge/issues/36) | P2 | galleryedge-* 校验没有绑定 uploader；传入其他 Worker 配置仍执行上传命令 | 管理员误选配置；本地截获命令，没有真实云上传或流量切换 |
| [#37](https://github.com/h1neolzr7f/GalleryEdge/issues/37) | P2 | umask 022 时 panel.db、外层备份包均为 0644，包含明文 TOTP seed | POSIX、父目录可遍历且存在其他本机用户；Windows ACL 未验收 |

## 第四轮：清理、采集、入库与公开路由

| Issue | 优先级 | 确认的行为 | 触发边界 |
| --- | --- | --- | --- |
| [#38](https://github.com/h1neolzr7f/GalleryEdge/issues/38) | P1 | tags=[42] 通过 pack 校验；第 201 条入库失败时前 200 条已经提交 | 确定的输入错误，不是网络故障导致的部分恢复 |
| [#39](https://github.com/h1neolzr7f/GalleryEdge/issues/39) | P1 | read_only 后端与 cleanup root 重叠时，带确认的清理会实际删除文件 | 需要管理员确认，且存在上传验证 marker；不是匿名删除 |
| [#40](https://github.com/h1neolzr7f/GalleryEdge/issues/40) | P2 | 新 checkpoint 重用输出目录，第二次成功采集静默覆盖旧 tar | 同一 adapter、不同状态库、同一输出目录 |
| [#41](https://github.com/h1neolzr7f/GalleryEdge/issues/41) | P2 | pack 接受 source:123/source.123；Python/TS 详情路由返回 404 | ID 合同与路由正则不一致；封面/API 使用同类受限正则 |
| [#42](https://github.com/h1neolzr7f/GalleryEdge/issues/42) | P2 | 临时 500 变为 seen.status=0，来源恢复后 incremental 仍不重投 | 人工 seed 可以补救；正常自动恢复路径缺失 |
| [#43](https://github.com/h1neolzr7f/GalleryEdge/issues/43) | P2 | max_pages=2 时，一个列表加十个 500 页面仍产生 11 次内容请求 | 失败不计预算；测试只用小规模 fake fetcher |

每个 Issue 都包含根因、固定源码位置、影响条件、复现结果和建议验收项。

## 已知问题复核

- [#30](https://github.com/h1neolzr7f/GalleryEdge/issues/30)：反代远程 TOTP 判断未解决。已追加证据：服务端虽然读取 `panel.require_totp`，正常配置 schema 却拒绝该属性，不能把“配置 require_totp=true”作为当前可用缓解步骤。两个待合并 PR 的 schema 也没有该字段。
- [#31](https://github.com/h1neolzr7f/GalleryEdge/issues/31)：PR #28 已部分处理 pack 成员预算和入库媒体流式哈希；尚未合并。panel backup 的 `_read` 仍整体读入，完整总预算/GB 内存验收未完成。已给旧 Issue 追加去重说明。
- [#32](https://github.com/h1neolzr7f/GalleryEdge/issues/32)：跨进程 DB/Blob 原子性仍需设计；#29 的 CAS 没有声称解决全部时序。
- [#33](https://github.com/h1neolzr7f/GalleryEdge/issues/33)：非本机 WebDAV 明文 Basic 传输仍在；本轮 #34 属于另一组件主动 HTTPS 降级，有独立修复位置。

## 复现与测试

**这些是缺陷复现探针：assert 通过表示观察到缺陷，不表示系统安全。** 修复后应将相应断言改为期望安全行为，并迁入正常回归测试；不要将此脚本原样加进安全验收门禁。

从仓库根目录：

```bash
python -m pip install -e '.[dev,panel]'
python -m pytest -q docs/audits/2026-10-15-round3/reproduce.py
```

Worker 探针先安装项目现有开发依赖，再回到仓库根目录运行：

```bash
cd packages/edge-worker/worker
npm ci
cd ../../..
node docs/audits/2026-10-15-round3/reproduce-worker.cjs
```

`reproduce.py` 中按测试函数名映射到各 Issue；HTTPS 有三组 URL，ID 有两组，因此用例数大于问题数。文件还保留一个明确标为 negative_control 的配额命名空间验证：该疑点在 #28 被排除，没有作为漏洞提交。

| 执行项 | 实际结果 |
| --- | --- |
| main 原有 Python suite：`python -m pytest -q -rs` | 211 passed，10 skipped |
| main 新增 opt-in Python 探针 | 13 passed，1 skipped |
| PR #28 隔离源码快照执行同一探针 | 14 passed |
| PR #29 隔离源码快照执行同一探针 | 13 passed，1 skipped |
| main TS 实际源码离线探针 | HTTPS 降级和两种 ID 路由缺口复现；退出码 0 |
| Worker `npm run typecheck` | 通过 |

两个 skip 是 main/#29 不含 #28 配额补丁时跳过 negative control。原套件十个 skip 属于本地未启动 sqld / S3 服务，不能视为通过；输出有一条 FastAPI/Starlette 测试客户端弃用警告。

## 排除、待核实与检查边界

1. **已排除：#28 配额触发器名字冲突。** 快照测试证实两组表前缀各有四个独立 trigger，第二组超限写入被拒绝。不能根据 main 中旧 GE_TABLES 推断 #28 新补丁存在同一问题。
2. **待核实：封面代理返回上游 SVG 活动内容。** TS 探针观察到上游 SVG 原样返回，响应 CSP 包含 unsafe-inline 且无 sandbox；没有进行真实浏览器脚本执行测试，所以未计入上述十项已确认问题、未单开漏洞 Issue。应进一步检查可信图片源边界与独立图像 CSP。
3. Cloudflare uploader 仅截获实际构造的命令；未调用真实 Wrangler 上传。敏感文件测试使用合成用户/seed、隔离临时文件；未读取任何实际凭据。
4. 未做真实 Turso/R2/WebDAV/Windows ACL、生产反向代理配置、生产站点浏览器测试及大规模性能压测。此次“扩大范围”覆盖更多根因与组件，不代表已覆盖所有代码或生产无漏洞。

建议先处理 #34、#38、#39，再处理认证与备份权限及其他稳定性问题；#30 远程运维信任边界也应保留优先处理。
