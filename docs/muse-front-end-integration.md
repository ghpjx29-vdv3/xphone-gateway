# Muse 与 XPhone 前端联调

## 版本与安装

先部署与本指南同一次提交的 Worker，再更新 Muse VM 的 Gateway。Gateway 使用 `/account/gateway-session` 获取短期会话；Muse 连接器使用普通 `/mcp` 凭证。普通连接器不能调用 Gateway 专用的 `runtime.*` 和事件租约写入工具。

在 Muse VM 更新：

```bash
cd ~/xphone-gateway && git pull
cp -a gateway/. ~/xphone/gateway/
cd ~/xphone/gateway
.venv/bin/python -m pip install cryptography
sudo systemctl restart xphone-gateway
sudo systemctl status xphone-gateway --no-pager
tail -n 80 gateway.log
```

确认 `prompts/` 与 `muse_context.py`、`muse_delivery.py`、`muse_memory.py` 都已复制。原有 `proxy.env`、`~/.config/xphone/account.token` 与 `content.key` 留在 VM。`muse-cli status` 应显示可用；服务模板配置的 HOME、CLI 路径和代理要与实际 VM 一致。

## Muse 侧检查

1. 普通 XPhone 连接器能找到提交 App action、发送角色消息所需工具，以及 `xphone.get_delivery`、`story.get_context`、`story.read_reference`。故事工具同时传本轮 `eventId` 和 route 中的 `consumer`。若连接器 schema 未显示 eventId，刷新连接器定义。
2. 普通连接器的 `tools/list` 不应列出 `runtime.read_asset`、`xphone.finish_event`。Gateway 自己的短期会话仍可调用这些工具。
3. 发一条 OS 用户消息。Gateway 日志应出现 `inbox write`、`muse session created`、`muse event completed`。Muse side chat 用本轮固定 batchId 提交加密 App action；客户端 `applied` 后，Gateway 才写入 `muse-memory.json` 并确认事件。
4. 对同一角色连续发两条 Messages 消息，确认复用该角色旁聊；换另一个角色，确认进入另一旁聊。OS 后台事件使用临时旁聊，完成后删除。普通 OS 对话保持固定旁聊。
5. 角色调用 `story.get_context({"eventId":"本轮事件 ID","consumer":"本轮 route.consumer"})` 后按需读取 `story.read_reference({"eventId":"本轮事件 ID","consumer":"本轮 route.consumer","referenceId":"索引中的 ID"})`；伪造 consumer 或读取不属于该 route 的 reference 应被拒绝。
6. 在专用测试帐号连续创建 20 个临时旁聊，记录创建时延、速率限制、会话上限及侧边栏表现，完成后清理。若要改用固定 ops 会话，先在专用测试旁聊验证 CLI 发送 `/clear` 后下一条消息确实不继承旧上下文；UI 有 `/clear` 不等于 CLI 一定执行。

## 前端侧检查

1. 在 Agent 模式打开同一个 pack/playthrough，保持 XPhone 页面在线。前端只发送加密事件和当前事实，不负责生成 Agent 模式共享摘要。
2. 在 OS 对话发消息，确认收到 `agent.reply`；在角色 Messages 私聊发消息，确认收到 `messages.append`。在社媒/地图触发事件，核对动作只应用到目标 surface。
3. 观察 Worker batch 从 `queued` 到 `applied`；Gateway 在 `queued` 时不得 `finish_event`。断开前端再触发事件，恢复连接后应应用同一 batch 一次。前端拒绝动作时应留下 `rejected` 与 Gateway 诊断，不写成功摘要。
4. 检查 `~/xphone/gateway/muse-memory.json`：同一事件只有一条 ledger；下一轮同角色可读其 surface 摘要，OS 可读可见范围内的跨 App 摘要。旁聊无需回复 JSON；已 `applied` 且有普通回复时，摘要来源为 `reply_text`，无回复时只记 delivery ledger。
5. 用超过 90 秒的 Muse 回合检查租约续期与 Worker 状态；等待客户端回执期间也应维持续租。Gateway 断线重连后未确认事件仍在 Worker inbox。

## 排查顺序

| 现象 | 先看 |
| --- | --- |
| 没有唤醒旁聊 | Gateway WS 是否连接、Worker inbox 是否有 event、`gateway.log` 是否有 `inbox write` |
| 旁聊有回复但手机没有动作 | side chat 是否用本轮 batchId 提交、`xphone.get_delivery` 的 `status` 与前端拒绝原因 |
| `queued` 长时间不变 | 前端 WS、客户端 action 应用和 `/client/applied` 回执 |
| 文档读取被拒 | 当前 event 是否 `running`、reference 是否在该 route 的可见索引内 |
| 事件再次执行 | `muse-dispatch.json` 的 batchId、Worker delivery 状态与租约时间；不要人工生成新 batchId |

联调时记录 eventId、batchId、route.roleId、Worker 状态、Gateway 日志时间和 Muse sessionId；不要粘贴内容密钥、账号令牌或私聊明文。
