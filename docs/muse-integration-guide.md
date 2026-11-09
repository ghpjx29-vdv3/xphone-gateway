# XPhone × Muse 联调指示文档与工程规范

> **版本**：v2.0 (统一 8 位配对码 + 标准 OAuth2 DCR)  
> **适用环境**：Muse VM (Linux / systemd) + Muse Connector Platform + GIGI 前端 + Cloudflare Worker

---

## 一、 核心架构与分层职责

系统将 **底层长连与数据加解密** 和 **上层 MCP 工具调用** 进行了明确的分工解耦：

```
┌────────────────────────────────────────────────────────────────────────┐
│                        XPhone 云手机 (GIGI PWA 前端)                     │
│               https://phone.xenoflex.xyz (或本地 4173/8081)             │
└──────────────┬──────────────────────────────────────────▲──────────────┘
               │ 1. 输入 8 位配对码授权凭证                  │ 2. 浏览器弹出 OAuth
               │   (ECDH 加密 contentKey + accountToken)   │   一键确认授权
               ▼                                          │
┌─────────────────────────────────────────────────────────┴──────────────┐
│                    Cloudflare Worker (中转与鉴权中心)                    │
│          https://xphone-agent-worker.xphone-maps-gateway.workers.dev   │
└──────────────▲──────────────────────────────────────────▲──────────────┘
               │                                          │
  (出站 WebSocket + 配对兑换)                         (MCP JSON-RPC 调用)
  走 8 位配对码:                                     走标准 OAuth2:
  /pair/start → /pair/redeem                         DCR 注册 → Code 换 Access Token
               │                                          │
┌──────────────┴──────────────────────────┐    ┌──────────┴──────────────┐
│  Muse VM 常驻服务 (xphone-gateway)       │    │  Muse Connector Platform │
│  - 运行 pair.py 自动落地凭证              │    │  - 注册类型: oauth2_code │
│  - WS 接收事件、AES-GCM 解密             │    │  - 凭证由 Vault 自动管线 │
│  - 调度本地 muse-cli 旁聊                │    │  - side-chat 触发 MCP   │
└─────────────────────────────────────────┘    └─────────────────────────┘
```

| 模块 | 负责内容 | 鉴权方式 | 凭证落盘/存储 | 用户操作体验 |
| :--- | :--- | :---: | :--- | :--- |
| **网关<br>(Muse VM)** | WebSocket 常驻监听、拉取事件、AES-GCM 解密正文、本地任务文档落地、调用 `muse-cli` 唤醒旁聊、Phase 1/2 派工 | **8 位配对码<br>(`pair.py`)** | `~/.config/xphone/content.key`<br>`~/.config/xphone/account.token`<br>(0600 权限) | 在 VM 终端运行 `pair.py` 得到 8 位码，在 GIGI 手机前端输入确认一次即可，无需手动复制粘贴长密钥。 |
| **连接器<br>(Muse Platform)** | Muse 旁聊在执行语义 Action / 任务派发时调用 Worker `/mcp` 端点 | **标准 OAuth2<br>(RFC 7591 DCR)** | 平台 Secure Vault 自动管理 Access Token | 平台根据 DCR 自动注册客户端，用户点击连接后在浏览器弹窗中一键点击授权，全程无需手填 API Key 或客户端编号。 |

---

## 二、 关键地址、仓库与文档索引

### 1. 代码仓库 (Git Repositories)

* **XPhone Gateway（Muse VM 运行代码）**
  * 仓库地址：`https://github.com/ghpjx29-vdv3/xphone-gateway.git`
  * 本地路径：`E:/NewProject/xphone-gateway`
  * VM 目标路径：`~/xphone-gateway`（源码） / `~/xphone/gateway`（部署运行目录）
* **XPhone Agent Worker（Cloudflare Worker 枢纽）**
  * 仓库地址：`https://github.com/ghpjx29-vdv3/xphone-agent-worker-private.git`
  * 本地路径：`E:/NewProject/xphone-agent-worker`
  * 生产环境基址：`https://xphone-agent-worker.xphone-maps-gateway.workers.dev`
* **GIGI 前端（XPhone 虚拟手机客户端）**
  * 本地路径：`E:/GIGI`
  * 生产/预览地址：`https://phone.xenoflex.xyz`（或本地 `http://localhost:4173`）

### 2. 关键接口与发现端点 (Live Endpoints)

* **MCP 主入口**：`https://xphone-agent-worker.xphone-maps-gateway.workers.dev/mcp`
* **OAuth2 授权发现 (RFC 8414)**：
  `GET https://xphone-agent-worker.xphone-maps-gateway.workers.dev/.well-known/oauth-authorization-server`
* **OAuth2 保护资源发现**：
  `GET https://xphone-agent-worker.xphone-maps-gateway.workers.dev/.well-known/oauth-protected-resource`
* **RFC 7591 动态客户端注册 (DCR)**：
  `POST https://xphone-agent-worker.xphone-maps-gateway.workers.dev/oauth/register`
* **OAuth2 用户授权页面**：
  `GET https://xphone-agent-worker.xphone-maps-gateway.workers.dev/oauth/authorize`
* **OAuth2 换票接口**：
  `POST https://xphone-agent-worker.xphone-maps-gateway.workers.dev/oauth/token`
* **8 位配对码接口**：
  * 发起：`POST /pair/start`
  * 兑换：`POST /pair/redeem`

### 3. 参考文档

* 网关部署说明：`E:/NewProject/xphone-gateway/gateway/README.md`
* 前端联调指南：`E:/NewProject/xphone-gateway/docs/muse-front-end-integration.md`
* 语义动作与旁聊派工架构：`E:/NewProject/xphone-gateway/docs/semantic-actions-sidechat-integration.md`
* 批次提交与租约协议：`E:/NewProject/xphone-gateway/docs/action-batch-architecture.md`

---

## 三、 联调实操步骤（5 步走）

### 第一步：更新 Muse VM 代码与依赖

在 Muse VM 的终端中执行：

```bash
cd ~/xphone-gateway
git pull origin main

# 同步运行目录
cp -a gateway/. ~/xphone/gateway/
cd ~/xphone/gateway

# 确保依赖已安装（需 Python 3.10+）
.venv/bin/pip install cryptography
```

### 第二步：执行配对码换票（绑定网关）

1. 在 Muse VM 终端执行：
   ```bash
   .venv/bin/python pair.py
   ```
2. 终端会打印 8 位配对码（形如 `8ABC9XYZ`，5 分钟有效）：
   ```text
   ====================================================
   XPhone 8 位配对码: 8ABC9XYZ (有效期 5 分钟)
   请在 GIGI 手机前端的「个人代理配对」页输入该配对码并确认授权。
   ====================================================
   ```
3. 打开 GIGI 手机前端（`https://phone.xenoflex.xyz`），进入 **「个人代理」** 设置页：
   * 输入终端中的 8 位配对码，勾选 18 周岁确认，点击 **「确认配对」**（若已有旧代理，点击「更换代理，创建新档」）。
4. VM 终端中的 `pair.py` 将自动完成兑换、解密，并将密钥落地到本地：
   * `~/.config/xphone/content.key`（AES-256-GCM 内容解密密钥）
   * `~/.config/xphone/account.token`（网关短期票据派发凭证）
5. 启动或重启常驻服务：
   ```bash
   sudo systemctl restart xphone-gateway
   sudo systemctl status xphone-gateway --no-pager
   ```
   查看日志确认 WebSocket 连接成功：
   ```bash
   tail -n 50 ~/xphone/gateway/gateway.log
   ```

### 第三步：配置 Muse 连接器（OAuth2 零配置接入）

1. 由 Muse 负责人将连接器平台注册类型设置为 **`oauth2_code`**。
2. 填入 MCP 服务端点：
   `https://xphone-agent-worker.xphone-maps-gateway.workers.dev/mcp`
3. 触发连接：
   * Muse 平台自动请求 `/.well-known/oauth-authorization-server` 发现端点；
   * 自动调用 `/oauth/register` 完成 DCR 客户端注册（无需用户手填 Client ID/Secret）；
   * 浏览器弹窗打开授权页面，用户点击 **「在 XPhone 中一键授权」**（或在前端输入页面展示的 8 位代码）；
   * 授权成功后弹窗自动跳回 Muse 回调，Muse Vault 自动保存 Access Token。

### 第四步：冒泡测试（验证网关上行与客户端渲染）

在 Muse VM 上执行一条冒泡测试消息：

```bash
cd ~/xphone/gateway
.venv/bin/python submit_actions.py --pack-id <packId> --playthrough-id <playthroughId>
```
* **核验标准**：
  1. `gateway.log` 中打印 `delivery status: applied`；
  2. GIGI 手机前端中收到 OS Agent 的消息：`🫧 冒泡测试：网关链路正常`。

### 第五步：端到端剧情与派工联调（核心业务场景）

1. **晚舟私聊（Messages 角色语义 Action）**：
   * 在 XPhone Messages 中给晚舟发一条消息。
   * Muse 唤醒对应旁聊，旁聊调用连接器的 `messages.append({"text":"..."})`。
   * 客户端收到后自适应匹配角色名称，渲染私信，回执 `applied`。
2. **故事续写两阶段派工（Phase 1 写作 + Phase 2 派工）**：
   * 在故事阅读器中发起续写（触发 `journal.main_record.requested`）。
   * **Phase 1**：OS 旁聊流式推流，独占提交 `journal.appendMainRecord`（step: 0），手机打字机呈现并落盘。
   * **Phase 2**：网关检测到 step 0 `applied` 后，触发 OS 在任务目录写 `<uuid>.md` 并调用 `xphone.delegate_task`；Subagent 被唤醒，调用 `xphone.submit_task_actions` 提交角色动作。
   * **终态**：手机 Messages 列表即时跳出晚舟私信，网关日志输出 `muse event completed: batches=['...', '...']`。

---

## 四、 常见问题排查速查表

| 现象 | 排查切入点 | 检查命令 / 解决方法 |
| :--- | :--- | :--- |
| **DCR 报 403 Forbidden** | 客户端/脚本未带合法的 User-Agent | 请求头增加 `User-Agent: Muse-Connector/1.0`（Cloudflare WAF 拦截默认 Python-urllib）。 |
| **页面提示“需先完成配对”** | OAuth 授权时账号尚无活跃的 Agent 模式存档 | 先在 Muse VM 跑一次 `pair.py` 并在 GIGI 确认，使账号进入已配对状态。 |
| **连接器提交工具报 401/403** | Token 过期或 JTI 已撤销 | 重新在 Muse 触发一次 OAuth 连接（或走 refresh_token 刷新）。 |
| **网关没有唤醒旁聊** | WebSocket 断连或 Inbox 为空 | 检查 `systemctl status xphone-gateway` 与 `~/xphone/gateway/gateway.log` 是否打印 `inbox write`。 |
| **手机没有动作，卡在 `queued`** | 前端离线或 receipt 回执未发出 | 保持 GIGI 前端页面激活，检查前端 DevTools Console 中的解密与 action 应用日志。 |
