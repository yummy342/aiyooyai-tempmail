# aiyooyai-tempmail

一个只收不发的一次性收件箱服务。单文件 Python，零第三方依赖。

打开页面即分配随机地址；邮件进箱后 1 小时自动删除，地址本身不过期。支持自选地址、换别名、看信、删信，HTML 邮件在沙箱 iframe 中渲染。

## 工作原理

```
外部邮件 ──MX──▶ 邮件服务器（catch-all 邮箱） ──IMAP──▶ tempmail.py
                                                        │
                                             SQLite（1 小时 TTL）
                                                        │
                                              浏览器 / HTTP API
```

- 收信：轮询一个 catch-all 邮箱，按收件人本地部分分箱。
- 存储：SQLite，WAL 模式；每箱最多 50 封、单封正文上限约 600KB。
- 页面：`/` 打开新收件箱，`?i=<inbox>` 回到指定收件箱。
- 限速：同一 IP 每小时最多建 30 个收件箱；跨站 POST 会被拒绝。

## 部署

需要一台能用 IMAP 读取 catch-all 邮箱的机器，以及一个把 MX 指向该邮箱服务器的域名。

```bash
sudo mkdir -p /opt/tempmail/data
sudo cp tempmail.py /opt/tempmail/
sudo cp deploy/tempmail.service /etc/systemd/system/
sudo tee /opt/tempmail/.env >/dev/null <<'EOF'
TM_DOMAIN=example.com
TM_PORT=8899
TM_BASE=/
TM_IMAP_HOST=mail.example.com
TM_IMAP_PORT=993
TM_IMAP_USER=catchall@example.com
TM_IMAP_PASS=change-me
EOF
sudo chmod 600 /opt/tempmail/.env
sudo systemctl enable --now tempmail
```

Nginx 反向代理见 `deploy/tempmail.nginx`。服务默认只监听 127.0.0.1。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TM_DOMAIN` | `example.com` | 收件域名，邮件地址后缀 |
| `TM_PORT` | `8899` | HTTP 端口 |
| `TM_BASE` | `/` | 反向代理子路径，如 `/tmpmail/` |
| `TM_DB` | `./data/tempmail.db` | SQLite 路径 |
| `TM_TTL` | `3600` | 邮件保留秒数 |
| `TM_LOG_TTL` | `86400` | 日志保留秒数 |
| `TM_MAX_MSG` | `50` | 每个收件箱最多保留封数 |
| `TM_CREATE_LIMIT` | `30` | 每 IP 每小时建箱上限 |
| `TM_IMAP_HOST` / `TM_IMAP_PORT` / `TM_IMAP_USER` / `TM_IMAP_PASS` | — | IMAP 连接参数 |

## API

- `POST /api/inbox` — 新建收件箱；可选 JSON `{"address":"hello"}` 自选本地部分。
- `GET /api/inbox/<id>` — 收件箱地址与未过期邮件列表（含剩余秒数）。
- `GET /api/inbox/<id>/msg/<mid>` — 单封邮件正文。
- `DELETE /api/inbox/<id>/msg/<mid>` — 删除单封。
- `POST /api/inbox/<id>/alias` — 给同一收件箱换一个新地址。

## 安全说明

- 不提供发送能力，也没有回信入口。
- HTML 邮件正文在 `sandbox` iframe 中渲染，脚本无法接触页面上下文。
- 收件箱 id 为 12 位随机串，知道 id 才能读该箱；地址与 id 分离。
- 日志只记事件与标题摘要，24 小时清除。

## License

MIT
