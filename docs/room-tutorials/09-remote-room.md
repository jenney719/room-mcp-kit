# 远程网页与 MCP

`python -m room_service.remote` 提供一个端口上的网页登录、Room REST/SSE 与
Streamable HTTP MCP。它默认只监听 `127.0.0.1:18460`，通过 HTTPS 反向代理访问。
原有本地预览、stdio MCP 和四个 Room 工具继续可用。

## 配置

在服务器私有环境文件中配置，不提交到 Git：

```ini
ROOM_HOST=127.0.0.1
ROOM_PORT=18460
ROOM_PUBLIC_URL=https://room.example.com
ROOM_DB=/persistent/room.db
ROOM_WEB_PASSWORD=<独立随机房间密码，至少32字符>
MCP_AUTH_TOKEN=<独立随机MCP令牌，至少32字符>
```

两个密码必须不同。网页使用房间密码登录，获得 Secure / HttpOnly /
SameSite=Strict Cookie；浏览器不会得到 MCP 令牌。网页写入只允许 owner，
MCP 写入只允许 companion。网页修改请求要求同源 Origin。

## 客户端

- 网页：`https://room.example.com/`
- MCP：`https://room.example.com/mcp`
- 传输：Streamable HTTP
- 认证头：`Authorization: Bearer <MCP_AUTH_TOKEN>`

这是 Bearer 认证的个人服务，不提供 OAuth 登录流程或 legacy SSE MCP。
网页状态流 `/api/v1/room/events` 是浏览器 SSE，与 MCP 传输不同。
客户端必须支持自定义 Bearer 认证头。操作回执可能仍处于 moving / approaching，
需要再次查询状态确认完成。

## 部署

HTTPS 代理转发到 `127.0.0.1:18460`，保留 Host，传递 X-Forwarded-Proto，
并关闭 SSE 响应缓冲。数据库必须放在代码发布目录之外；备份 SQLite
应使用 SQLite backup API，而不是只复制正在写入的 db 文件。

验证未认证的 `/health` 与 `/mcp` 返回 401，网页登录可读取 Room 状态，
认证 MCP 能初始化并列出四个工具，网页和 MCP 的 revision 一致。
公开 HTTPS、证书和实际客户端连接均应单独验收。

## 浏览器登录回归检查

登录页通过同源、CORS 模式的 fetch 提交密码。普通表单导航在
`Referrer-Policy: no-referrer` 下会发送 `Origin: null`，无法通过严格来源校验。
服务仍拒绝 null、缺失和外站 Origin，参见 [Fetch 规范](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。

额外安装仅供测试的 Playwright 后，可以运行真实浏览器检查：

```powershell
python -m pip install playwright
python tools/check_remote_browser_login.py
```

脚本使用本地临时 HTTPS 服务与测试凭据，不访问真实服务器或真实密码。
验证错误密码提示、正确登录、Secure Cookie、共享状态和房间 Canvas 启动。
Windows 优先使用已安装的 Edge/Chrome；其他环境可通过 `ROOM_TEST_BROWSER`
指定 Chromium 可执行文件，或先运行 `python -m playwright install chromium`。
