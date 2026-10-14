# Security Policy / 安全策略

- 请通过 GitHub Security Advisories 私下报告漏洞，不要开公开 issue。
- Report vulnerabilities privately via GitHub Security Advisories.

Design rules: secrets live only in the encrypted vault (`ge-ops vault`), logs are masked, the ops panel binds to 127.0.0.1 by default, CI runs gitleaks on every push and PR.
