# XPhone Gateway

把 XPhone 云手机的 Agent WebSocket 接进个人代理所在的可信环境。Muse 用户直接在自己的 Muse VM 运行 Gateway，不需要另购 VPS；其他个人代理仍可选择现有 worker provider。

## 这是什么（给新用户）

Gateway 常驻在 Muse VM，主动连上 XPhone Worker 的 Agent WS。Worker 先持久化客户端事件，再发 WS 通知；Gateway 拉取并解密后，按角色构建 prompt，使用 `muse-cli send --wait 60` 唤醒对应 side chat。side chat 通过任务文件路径交付语义动作，由 Gateway 拼包加密；Gateway 核验客户端 `applied` 后才完成事件。低频 Hook 保留为故障兜底。当前协议与事务边界见 [路径交付与动作拼包架构](../docs/action-batch-architecture.md)，联调步骤见 [Muse 与前端联调指南](../docs/muse-front-end-integration.md)。

```
┌─ XPhone Worker (Cloudflare) ─────────────────┐
│  /mcp (Streamable HTTP)   /agent/ws (WSS)     │
└──────────────┬───────────────────────────────┘
               │ 出站 WSS, 子协议 xphone-agent-ticket.<ticket>
┌──────────────▼───────────────────────────────┐
│  gateway.py (Muse VM 上的 systemd 常驻服务)   │
│   取票 → 连 WS → 收通知 → 拉取 → 解密 → 落盘    │
└──────────────┬───────────────────────────────┘
               │  ~/xphone/inbox/<event-id>.json (明文, 处理完删除)
┌──────────────▼───────────────────────────────┐
│  Muse provider → muse-cli side chat          │
│   角色 prompt → 连接器提交动作 → 客户端 applied │
│   → 共享摘要 → finish_event → ack_inbox       │
└──────────────────────────────────────────────┘
```

## 一键接入（推荐）

用户只需要对自己的个人代理说一句话：

> 帮我部署 github.com/ghpjx29-vdv3/xphone-gateway 并连接 xphone

个人代理读完本节后全自动完成下面所有步骤。**前端配合**：个人代理页提供「个人令牌」
与「内容密钥」两个一键复制按钮；检测到连通后自动显示「已连接」（建议轮询冒泡测试
batch 的 delivery→applied，或检测该用户 agent WS 在线）。

### 个人代理执行清单

0. **平台检查**：`uname -s` 应为 Linux 且 `systemctl` 可用；否则停止并告诉用户
   "一期只支持 Linux+systemd，你的系统是 <xx>，暂不支持"。
1. **取代码**：`git clone <repo> ~/xphone-gateway`（目录已存在则 `git pull` 更新）；
   通读本 README。
2. **装依赖**：把仓库的 `gateway/` 内容同步到 `~/xphone/gateway/`，再在目标目录执行 `python3 -m venv .venv && .venv/bin/pip install cryptography`。Muse provider 还需在 VM 安装并登录 `muse-cli`。
   （Python 3.10+；加解密必须用该 venv 的 python，系统 python 没有 cryptography）。
3. **配对换取凭证（统一 8 位配对码）**：
   在目标目录执行 `.venv/bin/python pair.py`。
   终端将输出 8 位配对码（例如 `3EY64LSN`，有效期 5 分钟）。
   代理将该 8 位配对码告诉用户，让用户在 XPhone / GIGI 手机前端的「个人代理」页输入并点击确认配对。
4. **自动落盘凭证**：
   用户在前端确认后，`pair.py` 自动从 `/pair/redeem` 完成兑换并用本地私钥解密，将凭证写入：
   - `~/.config/xphone/content.key`（内容解密密钥，chmod 600）
   - `~/.config/xphone/account.token`（账号 Bearer 令牌，chmod 600）
   用户和代理全程无需手工复制粘贴长密钥。
5. **配置 MCP 客户端（connector）**：
   - **推荐路径（Muse VM 本地连接器）**：VM 本地连接器脚本直接从 `~/.config/xphone/account.token` 读取 Bearer Token（或由 Gateway `/account/gateway-session` 派发），无需手动配 API Key。
   - **平台路径（第三方平台 / Muse 平台手工添加）**：若平台连接器配置表单必须填入 API Key，使用 `pair.py` 成功输出中打印的 `accountToken` 填入 `Authorization: Bearer <accountToken>` 即可。
6. **起常驻网关**：先用 `sudo env HOME=/home/hatch wss_proxy="${https_proxy:-${HTTPS_PROXY:-}}" "$(command -v muse-cli)" status` 确认服务所用的 HOME 和 Muse CLI 可连接，再从 `~/xphone/gateway/` 执行 `sudo cp xphone-gateway.service /etc/systemd/system/ &&
   sudo systemctl daemon-reload && sudo systemctl enable --now xphone-gateway`
   （已安装则为重装+重启）。服务模板选择 Muse provider；如需出站代理，先配好 `proxy.env`。若 systemd 找不到 CLI，在该文件写入 `XPHONE_MUSE_CLI=/绝对路径/muse-cli`。将 Hook 脚本同步到 `~/hooks/scripts/xphone-inbox.sh`，并通过 Muse 的 Hook 管理工具把已注册 Hook 的 `poll_interval_secs` 更新为 600；只复制 JSON 文件不会更新已注册配置。
7. **装 watchdog**：按「运行与自愈」把 `watchdog.sh` 加入定时任务（每 10 分钟）。
   （云主机约每 2 小时被回收一次，watchdog 检查进程与连接健康时间并恢复；重启后重新拉取服务端未确认事件。）
8. **验证**：`~/xphone/gateway/gateway.log` 应出现取票成功、WS 已连接、`inbox write` 和 `muse event completed`。Gateway 通过专用会话负责事件领取、续租与完成。
9. **冒泡测试**（必须做）：用 `submit_actions.py --pack-id <packId> --playthrough-id <playthroughId>` 提交一条 `agent.reply` 测试消息
   （`messageId`=`bubble-<时间戳>` 唯一，`text`=`🫧 冒泡测试：网关链路正常`，
   `messageType`=`text`，`role`=`os-agent`）；用 `xphone.get_delivery` 复核该 batchId
   到 `applied`（最多等 2 分钟）。成功后告诉用户：去 XPhone 前端个人代理对话里看，
   应该能看到这条测试消息——**看到即全链路打通**
   （认证→取票→WS→加密提交→applied→客户端渲染）。
10. **汇报**：成功用 3-5 句话说明做了什么、网关状态、测试消息已发出；
    失败则明确说卡在哪一步、原始错误是什么、需要用户提供什么。
    **绝不编造"成功"，不确定就说不确定。**

### 重新配对/更换代理

需要更换密钥或重置存档时：在 VM 重新运行 `.venv/bin/python pair.py`，让用户在前端确认配对码；完成后执行 `sudo systemctl restart xphone-gateway` 并复核验证。
### 执行中必须遵守

严格遵守「关键协议细节」第 1-8 条，尤其是：
不带 `Origin` 头、UA 用 `XPhone-Gateway/1.0`、ticket 一次性 60 秒有效、
单存档同时只允许一个 Agent WS（报 409 先停旧网关，不要硬连）、
`accepted ≠ applied`（必须 `get_delivery` 复核）。

## 前置条件

- Python 3.10+，`pip install cryptography`
- 运行 `pair.py` 通过 8 位配对码在 XPhone / GIGI 前端完成配对
- 出站网络（本项目在需要 HTTP 代理的环境测试通过，见下方代理说明）

## 安装

```bash
git clone <repo> ~/xphone-gateway
mkdir -p ~/xphone/gateway
cp -a ~/xphone-gateway/gateway/. ~/xphone/gateway/
cd ~/xphone/gateway
python3 -m venv .venv && .venv/bin/pip install cryptography

# 1. 放内容密钥（0600，其他人不可读）
mkdir -p ~/.config/xphone && chmod 700 ~/.config/xphone
# 把 43 字符 base64url 密钥写入 ~/.config/xphone/content.key
chmod 600 ~/.config/xphone/content.key

# 2. 放代理配置（如需出站代理；不需要则留空文件）
cp proxy.env.example proxy.env  # 按环境填写 https_proxy 等

# 3. 安装 systemd 服务（模板在 xphone-gateway.service，按路径改）
sudo cp xphone-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xphone-gateway.service
```

MCP 令牌走各 Agent 平台自己的密钥机制（如 Muse 的 Secure Vault + connector），**不要**写进本仓库任何文件。

### 已有 Muse VM 升级

仓库更新后，把新代码和服务模板同步到原有的 `/home/hatch/xphone/gateway/`，保留该目录原有的 `proxy.env`、内容密钥和账号令牌：

```bash
cd ~/xphone-gateway && git pull
cp -a gateway/. ~/xphone/gateway/
cp hooks/xphone-inbox.sh ~/hooks/scripts/xphone-inbox.sh
sudo cp ~/xphone/gateway/xphone-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart xphone-gateway
```

如果 `muse-cli` 不在 systemd 的 PATH，在原有 `~/xphone/gateway/proxy.env` 中添加 `XPHONE_MUSE_CLI=` 和 CLI 的绝对路径。通过 Muse 的 Hook 管理工具将已注册的 `xphone-inbox` 轮询间隔改为 600 秒；仅复制仓库中的 Hook JSON 不会修改已注册配置。发一条 XPhone 测试消息后，检查 Gateway 日志中的 `muse session created` 和 `muse event completed`，并确认客户端收到了动作。

## 配置项

| 项 | 位置 | 说明 |
|---|---|---|
| 内容密钥 | `~/.config/xphone/content.key` | base64url 32 字节，0600。网关启动读一次进内存 |
| 代理 | `gateway/proxy.env` | `https_proxy`/`wss_proxy` 等，0600。Muse WSS 优先使用 `wss_proxy`，未设置时沿用 HTTPS 代理 |
| provider | systemd 服务 / `proxy.env` | 服务模板设为 `muse`；其他个人代理可在 `proxy.env` 设 `XPHONE_PROVIDER=worker` 使用现有 worker 路径 |
| Muse CLI | Muse VM | 登录凭证只保存在该 VM；`XPHONE_MUSE_CLI` 可指定可执行文件的绝对路径，不传给公共 Worker 或写入日志 |
| MCP | Worker | Gateway 直连 MCP 使用 gateway session；Muse side chat 使用普通连接器凭证 |
| 收件箱 | `~/xphone/inbox/` | 明文事件落地，worker 处理完删除，避免长期落盘 |

## 运行与自愈

- `systemctl status xphone-gateway` 看状态；日志 `~/xphone/gateway/gateway.log`
- **VM 会被替换**：`/etc` 是易失的，云主机替换后 systemd unit 会消失。配套 `watchdog.sh` + 定时任务（每 10 分钟）检查进程和最近健康时间；成功拉取、WebSocket pong 或普通手机模式检查都会更新健康时间。超过 5 分钟无健康信号时从 `xphone-gateway.service` 模板重装或重启。网关每次从服务端未确认队列的起点重扫，缺失的本地文件会重新生成。
- **Muse 事件分发**：Gateway 按服务端 cursor 串行处理；OS 用户对话与角色私聊各有固定 side chat，OS 后台事件使用临时 side chat。映射、固定 batchId 与重试时间写在 `muse-dispatch.json`，共享摘要写在 `muse-memory.json`。失败按 30 秒起、最长 10 分钟退避；收到 `queued` 继续等待，只有客户端 `applied` 才确认事件。租约由独立线程每 30 秒续期，Hook 每 10 分钟检查一次作为故障兜底。
- **云电脑内容树**：首次连接 pack 时，Gateway 从 MCP 拉取 manifest、全部故事文档、角色 prompt 与动作清单，在 `~/xphone/content/{packId}/` 建立 `docs/`、`prompts/` 和 `{playthroughId}/tasks/delegations/`、`{playthroughId}/tasks/events/`、`{playthroughId}/memory/`。`prompts/` 包含 `tool-reminder.md`、`delegation-inject.md` 和 `app-actions.json`；`memory/` 初始化 `summary.md`、`pins.md`。后续旁聊按本轮给出的绝对路径直接读取这些文件。
- **派工与动作文件**：OS 在 `tasks/delegations/{eventId}/{uuid4}.md` 写封闭词表中的目标与自然语言任务，再调用 `xphone.delegate_task({"taskPath":"文档绝对路径"})`。subagent 在同目录写同名 `.actions.json` 语义动作数组，再调用 `xphone.submit_task_actions({"taskPath":"原任务文档绝对路径"})`。OS 自身回复和 Journal 使用 Gateway 创建的 `tasks/events/{eventId}/{eventId}.md`，同样写同名动作文件并只提交任务路径。Worker 只转发路径；Gateway 查派工或事件状态，生成 header 并加密正文。接收确认不代表完成，客户端 `applied` 后摘要才写入 ledger。
- **事件事务边界**：Gateway 领取事件租约并启动 Muse；Journal Phase 2 的派工记录在父事件状态中。子批次异步 `applied` 时，Gateway 查询该父事件全部派工状态；全部应用后由 Gateway 调用 `finish_event`，再按 cursor 确认 inbox。等待中的派工不计入失败次数；WS 空闲时只在本地重试时间到期后重新拉取，避免无事件轮询。receipt 仍遵循 Worker 的顺序 ack，不能越过未完成的早期事件。
- **worker 卡住告警**：`lease-stuck-monitor.sh` + 定时任务（每 30 分钟）。Gateway 按上面的退避规则重试，Hook 每 10 分钟兜底；若文件仍超过 30 分钟未完成，需要查看 Gateway 和 MCP 错误。监控只告警，不替处理者调用 `finish_event`。
- 手动暂停维护：`touch ~/xphone/gateway/PAUSED`，watchdog 与卡住监控都会跳过

### 个人代理定时维护

`agent_maintenance.py` 通过 `muse-cli` 唤醒同一 pack/playthrough 的个人代理旁聊。体验检查每 30 分钟读本地 ledger 与角色文档，更新带过期时间的 `memory/health.md`；素材采集每天 3 次按 manifest 的 `inspirationCity` 搜索公开事件并更新 `memory/inspiration.md`；记忆整理每天 04:00 生成摘要修改提案，脚本在与 Gateway 共用的文件锁下备份并应用，仅替换摘要字段。网关下一轮把有效提醒注入 prompt。

在 Muse VM 的运行时定时任务中加入以下三项（台北时间）：

```cron
CRON_TZ=Asia/Taipei
*/30 * * * * HOME=/home/hatch /home/hatch/xphone/gateway/.venv/bin/python /home/hatch/xphone/gateway/agent_maintenance.py experience >> /home/hatch/xphone/gateway/maintenance.log 2>&1
10 4,12,20 * * * HOME=/home/hatch /home/hatch/xphone/gateway/.venv/bin/python /home/hatch/xphone/gateway/agent_maintenance.py inspiration >> /home/hatch/xphone/gateway/maintenance.log 2>&1
5 4 * * * HOME=/home/hatch /home/hatch/xphone/gateway/.venv/bin/python /home/hatch/xphone/gateway/agent_maintenance.py memory >> /home/hatch/xphone/gateway/maintenance.log 2>&1
```

定时任务要继承 Muse CLI 登录环境及出站代理。`agent_maintenance.py` 使用 `XPHONE_MUSE_CLI` 指定的 CLI 路径；未设置时使用 `/home/hatch/.local/bin/muse-cli`。

## 关键协议细节（实测所得）

1. **Cloudflare 边缘拦截**：默认 Python UA 会被 1010 按浏览器签名拦。客户端必须发正常 UA（如 `XPhone-Gateway/1.0`），**不要**伪装成浏览器。
2. **Origin 头**：Worker 只校验*已存在*的 Origin；不发送则跳过检查。WS 握手**绝不**带 Origin。
3. **非标准 101**：Cloudflare 回的 101 带 `Connection: close`，`websockets`/`websocket-client` 两库都判非法。本项目自研最小 WS 客户端（`wsclient.py`），手动 CONNECT 隧道 + TLS + 握手。
4. **ticket 一次性**：`open_event_stream` 的票 60 秒有效，一次一用；断线重取。单存档同时只允许一个 Agent WS（409 冲突）。
5. **加密**：AES-256-GCM，key=base64url 解码 32 字节，nonce 12 字节随机，AAD=base64url 解码后的 aad 字节；ciphertext 含 16 字节 tag。事件 AAD 含明文 `route`（role/roleId/windowId/consumer）。
6. **动作提交**：`action_batch` 只接受 v2。envelope 明文 header 含 `batchId`、`parentEventId`、`step`、`targetApp`、`surfaceId`、`origin`，并整体进入 AAD；正文只含语义动作，仍由 AES-256-GCM 加密。`envelope.id=header.batchId`。模型不填写批次或路由字段。
7. **顺序 ack**：服务端要求按 cursor 顺序 `ack_inbox`；Gateway 每次只分发最早的未完成事件，Hook 兜底时也只唤醒最早的本地文件。
8. **receipt 非工作事件**：`start_event` 会被拒，直接 `get_delivery` 核验后 ack。
9. **accepted ≠ applied**：`submit_task_actions` 返回的 accepted 只代表服务端入队，不代表客户端落地。Gateway 用 `xphone.get_delivery` 复核最终状态（applied / rejected / queued）。
10. **actor 强绑定**：客户端按 `policy.threadActors[threadId] === action.from` 严格校验。`messages.append` 的 `from` 必须与目标 threadId 绑定的角色名**精确一致**（如在晚舟的 threadId 里填 `from: "顾骁"` 会被安全策略以 `Invalid Messages actor or content` 拒绝）。提交前务必核对 threadId↔角色映射。
11. **手动与 hook 互斥**：人工手动处理 inbox 文件时，hook 轮询可能同时处理同一批事件，导致重复提交。手动介入前先 `touch ~/xphone/gateway/PAUSED` 暂停自动处理，完事再删掉。
12. **社媒主帖 vs 评论**：`socialmedia.applyBatch` 中 `operation: 'appendPost'` 发独立主帖（Timeline 展示）；要在帖子详情评论区回复必须用 `operation: 'appendComment'`（字段：`platform`、`postId`、`author`、`text`），前端才会存入评论区并在详情页展开。
13. **三平台作者字段不一致**（实测踩坑）：X 平台要求 `authorName`；Instagram 要求 `displayName` 或 `username`；Reddit 校验的是 `item.author`（用 `authorName` 会取到 undefined 而被拒 `Unknown post author`）。跨平台提交时按平台填对应字段，不要统一用一个。

## 文件一览

- `gateway.py` — 主进程
- `wsclient.py` — 自研 WS 客户端（见上第 3 条）
- `submit_actions.py` — 主动提交加密动作的辅助脚本
- `test_crypto.py` — 离线解密验证
- `watchdog.sh` — 存活检查与自愈
- `lease-stuck-monitor.sh` — worker 卡住告警（只告警不自动恢复）
- `avatar_sync.py` — 推送 Muse 头像（静态图+状态动画）给 Worker，客户端加载为个人代理形象（需 Worker 实现 `xphone.set_agent_profile`）
- `xphone-gateway.service` — systemd unit 模板
- `proxy.env` — 出站代理（示例值按环境改）

## 打包说明（给维护者）

- npm 包形态建议：Node 包装层 + 预编译/内嵌 Python 网关，或纯 Node 重写 `wsclient` 逻辑（握手细节见上）。Python 依赖只有 `cryptography`（`websocket-client` 已不再需要）。
- 密钥与令牌永远不进包、不进日志、不进错误信息。
- 对外文档只讲"如何连上自己的 XPhone"，不涉及任何具体用户的 packId/playthroughId/密钥。

## 地图 MCP 接口规划（设计中，Worker 侧待实现）

XPhone 的 OSM 用真实世界数据，网关侧**不读全量**，只按需查询。虚构地名与真实 OSM 之间有一层映射，由接口返回（`fictionName` + `realAnchor`）。

### 读接口

| 接口 | 输入 | 输出 |
|---|---|---|
| `map.get_actor_positions` | — | `[{actorId, name, placeId, placeName, lat, lon, updatedAt}]` |
| `map.get_place` | `{placeId}` 或 `{name}` | `{placeId, fictionName, realName, type, tags[], lat, lon, description, realAnchor, zoneId}` |
| `map.search_places` | `{query?, type?, tags[], near?, radiusKm?, limit?}` | `[{placeId, fictionName, type, distanceKm}]`（支持按意图搜，如"安静的海边咖啡厅"） |
| `map.route` | `{from, to}` | `{distanceKm, durationMin, waypoints[]}` |
| `map.nearby_actors` | `{lat, lon, radiusKm}` | `[{actorId, name, distanceKm}]` |
| `map.get_zone` | `{lat, lon}` | `{zoneId, name, controllingFaction, dangerLevel}` |
| `map.optimize_route` | `{places[], start?}` | `{orderedPlaces[], totalDistanceKm, totalDurationMin}` |

### 写接口

| 接口 | 输入 |
|---|---|
| `map.set_actor_position` | `{actorId, placeId, reason?}` |
| `map.place_item` / `map.get_items_at` / `map.remove_item` | `{placeId, itemId, description}`（死信箱/藏物玩法） |

### 素材接口（生图管线用）

| 接口 | 输入 | 输出 |
|---|---|---|
| `assets.get_character_reference` | `{actorId}` | `{imageUrl, description}`（角色立绘） |
| `assets.get_style_reference` | `{styleId}` | `[{imageUrl, description}]`（视觉风格） |
| `assets.get_place_reference` | `{placeId}` | `[{imageUrl, source}]`（实拍/街景/概念图） |

### 事件订阅（二期）

- `map.watch_place({placeId, condition})` → 地点变化推事件（geofence）

### 业务场景

1. **按意图邀约**：角色搜 POI（type+tags）→ 查虚构改名 → `messages.append` 附定位卡片
2. **地图互动**：用户点地点 → 取详情 → 有 realAnchor 才联网搜质感信息
3. **A→B 移动**：算路线 → 叙事化路程 → waypoints 可挂事件钩子
4. **proximity 警戒**：`nearby_actors` 做跟踪/反跟踪
5. **地盘策略**：`get_zone` 查控制势力+危险等级
6. **地点骰子事件**：到达 → 事件 → 骰子判定 → `world.applyStatePatch` 结算金钱/声望（随机数建议服务端掷）
7. **角色日常生活**（ambient life）：定时唤醒 → 按日程模板移动位置 → 偶尔发帖/私信（发帖低成本，DM 高成本，慎用）

## 图片协议规划（设计中）

复用的是**协议约定**，不新增 MCP tool。生图在我侧（云电脑），上传走我侧存储，MCP worker 只透传 URL。

### 统一图片附件（所有 surface 共用，字段名全局统一）

```json
{ "imageUrl": "https://... (48h有效)", "altText": "描述", "width": 1024, "height": 1024 }
```

### 各 surface 字段改动

- **`messages.append`**：新增 `messageType`（`"text"`默认 | `"image"` | `"location"`）；`messageType: "image"` 时带 `imageUrl` + 可选配文 `text`
- **`messages.append` 地点卡片**：`messageType: "location"`，`location: {lat, lon, name, address, imageUrl?}`
- **`socialmedia.applyBatch`**：X 补 `imageUrl`（现缺失）；IG 已有不动；Reddit 补 `imageUrl`；`appendComment` 同样支持可选 `imageUrl`
- **`messages.user_sent`**（用户发图）：复用，加 `messageType: "image"` + `imageUrl`，不新增事件类型
- **新事件**：`map.image_requested {placeId}`（用户在地图 App 点"看看这里"，异步生图，先回"生成中"）

### 生图管线

```
触发 → 取参考（assets.* 三个接口）→ 补参考（MCP 无街景时用 image_search/places 按 realAnchor 搜）
→ 参考图下载到本地 → media.generate_image（按 角色→风格→地点 顺序传参考图）
→ remote-storage upload-file（48h URL）→ gateway submit_actions 带 imageUrl → 前端渲染
```

### URL 生命周期（硬规则）

1. URL 48h 有效，生成后尽快提交不攒
2. 客户端必须本地缓存，不依赖 URL 长期有效
3. 图片 ≤2MB，长边 ≤2048px，加载失败不阻塞文字
4. ambient 动态里图片一天最多 1-2 张，深夜不发
