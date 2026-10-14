# Security

- Report suspected vulnerabilities privately if possible (GitHub Security Advisory), or open an Issue **without** pasting real secrets.
- Default panel binds `127.0.0.1` only. Do not expose it to the internet without auth + HTTPS + TOTP.
- Cloudflare release tooling only allows Worker names matching `galleryedge-*`.
- Never commit tokens, vault passphrases, or API keys. Use `vault:` refs or 0600 token files outside the repo.
