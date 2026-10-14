# Contributing to GalleryEdge

感谢你愿意一起改进 GalleryEdge。下面几条是**硬边界**，PR 前请先读完。

## 我们欢迎

- Bug 修复、测试、文档与翻译
- 面板 / 向导 / 无障碍与小白体验改进
- 性能、安全加固、类型与 CI
- **合成数据**适配器、本地/演示用 fetcher
- 已在仓库内、且合规边界写清的来源上的小改进（例如 Commons 白名单分类、过滤误伤），需附测试

## 我们不接受

1. **任意真实网站的爬虫 / 站点适配器**  
   不要提交「某画站 / 某社交 / 某图床」的 `SiteAdapter`、登录绕过、Cookie/Token 池、批量注册或账号共享。  
   原因：版权、ToS、robots、隐私与滥用风险，维护者不替你背。

2. **绕过 robots.txt、登录墙、付费墙、验证码** 的代码或「教程」

3. **真实作品、密钥、账号、Cookie、token** 出现在代码、测试夹具、截图、示例配置或 PR 描述里  
   测试请用 `FAKE-…` 等明显假值（见 `.gitleaks.toml` allowlist）。

4. **指向生产环境** 的默认配置、一键部署脚本（例如替换名为 `nai` 的 Worker、写入他人的 R2/Turso）

若你有**官方开放 API、许可清晰、robots 允许**的新来源想法：请先开 Issue 讨论许可与 robots 证据，**不要直接丢适配器 PR**。

## 开发流程

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,panel,storage]"
pytest -q
cd packages/panel/web && npm ci && npm test && npm run build
```

- 用功能分支发 PR，CI（`test` / `panel-web` / `s3-compat` / `secret-scan`）需全绿。
- 新行为请带自动化测试；用户可见文案优先中文。
- 提交前可本地跑：`gitleaks detect --no-git -c .gitleaks.toml`（若已安装）。

## 行为准则（简）

善意沟通；不在 Issue/PR 里张贴他人隐私或未公开的凭证。维护者可关闭不符合本文件的 PR，且不必逐条辩论。

License：贡献即同意以 [MIT](LICENSE) 授权。
