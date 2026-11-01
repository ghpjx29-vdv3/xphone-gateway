# xphone-gateway

云电脑侧常驻网关：通过出站 WebSocket + MCP 对接 XPhone Cloudflare Worker，
让个人代理无需公网 IP 即可收发云手机事件、提交加密动作。

## 结构

- `gateway/` — 网关主程序与运维脚本（详见 `gateway/README.md`）
- `hooks/` — 事件唤醒 hook

## 快速开始

**推荐：一键接入** —— 对你的个人代理说：

> 帮我部署 github.com/ghpjx29-vdv3/xphone-gateway 并连接 xphone

个人代理会按 `gateway/README.md`「一键接入」节全自动完成部署（含冒泡测试）。
手动安装见该节「前置条件」。

1. 内容密钥放 `~/.config/xphone/content.key`（0600）
2. MCP 令牌走 agent 平台的安全密钥机制（只进不出）
3. 出站代理按环境配 `gateway/proxy.env`（模板见包内）
4. 加解密用项目 venv（系统 python3 无 cryptography）

## 安全

密钥与令牌绝不进包、不进日志、不进版本库。明文事件 worker 处理完即删。
