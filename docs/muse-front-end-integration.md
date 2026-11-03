# Muse 与 XPhone 前端联调

本轮语义 Action、按需读文档和每轮 hook 的实测步骤见 [semantic-actions-sidechat-integration.md](semantic-actions-sidechat-integration.md)。以下保留 Gateway 与 Worker 的基础部署、租约和 delivery 检查。

## 版本与安装

先部署与本指南同一次提交的 Worker，再更新 Muse VM 的 Gateway。Gateway 使用 `/account/gateway-session` 获取短期会话并在连接时缓存故事文档；Muse 旁聊从云电脑本地文件读取角色资料，通过 XPhone 连接器执行动作。

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

本次同步更新了仓库的 `watchdog.sh`：它检查进程和最近 5 分钟的健康信号；Gateway 收到 WebSocket pong、成功拉取 inbox 或在普通手机模式检查 scope 时更新该信号。活跃 pack 的 scope 变化和账号令牌更换由 Worker 主动关闭旧 WebSocket 触发 Gateway 重连；连接仍按会话过期时间刷新。

## Muse 侧检查

1. 确认 Gateway 已在 `~/xphone/content/{packId}/` 建立 `docs/`、`prompts/` 与 `{playthroughId}/tasks/delegations/`、`{playthroughId}/memory/`。旁聊读取提示词给出的本地文件路径，连接器只用于提交 App action、查询交付状态与提交任务文档路径。
2. 普通 XPhone 连接器应显示 `xphone.delegate_task({"taskPath":"绝对路径"})` 和动作提交工具；`xphone.finish_event` 是 Gateway 的事件管理动作，旁聊不用调用。
3. 发一条 OS 用户消息。Gateway 日志应出现 `inbox write`、`muse session created`、`muse event completed`。Muse side chat 用本轮固定 batchId 提交加密 App action；客户端 `applied` 后，Gateway 才写入 `muse-memory.json` 并确认事件。
4. 对同一角色连续发两条 Messages 消息，确认复用该角色旁聊；换另一个角色，确认进入另一旁聊。OS 后台事件使用临时旁聊，完成后删除。普通 OS 对话保持固定旁聊。
5. OS 在本轮 `localContent.taskDirectory` 建立 `<uuid4>.md`，调用 `delegate_task` 只传绝对路径；检查 Worker WS 消息不含任务正文，subagent 从云电脑打开该文档后提交本轮 batch。当前共享连接器凭证不区分具体旁聊身份，角色边界还依赖提示词与本地可读路径。
   `目标:` 必须逐字使用 `localContent.delegationTargets` 的一项，例如 `messages / 晚舟` 或 `notes / 备忘录`。用 `messages / 晚舟 DM` 做一次失败用例：MCP 应返回可用目标并要求修改文档后重试；同一路径改正后应成功。已成功派出的文档再次调用应提示新建任务文件，不应声称再次唤醒。Worker 响应中的 `requestId` 可与 Gateway 的 `delegation request received`、`delegation acknowledged` 日志对应；若 Muse 只显示 accepted 而 Gateway 无该 requestId，需继续检查 Muse 连接器到 Worker 的调用记录。
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
| `delegate_task` 显示 accepted，但 subagent 没唤醒 | 查看 MCP 完整返回是否含 `status: delegated` 和 `requestId`；用 requestId 对照 Worker 与 Gateway 日志，确认是否收到 Gateway 的 `delegate-ack` |

联调时记录 eventId、batchId、route.roleId、Worker 状态、Gateway 日志时间和 Muse sessionId；不要粘贴内容密钥、账号令牌或私聊明文。

## 本轮尚未完成的调用边界

主事件提示已移除重复的 playthroughId、eventId、consumer、appId、surfaceId，以及历史消息的 id 和毫秒时间；事件仍可由 `localContent.taskDirectory` 定位。`batchId` 和 subagent 的 parentEventId、step 目前仍出现在提示中：Muse 旁聊直接向 Worker 连接器提交端到端加密 envelope，而 Worker 看不到动作明文，无法在收件时补入这些字段。要让模型只写语义意图，需要在云电脑侧的动作构造入口注入这些环境值，再调整连接器调用链和前端批次关联；不能只删提示词。该项没有部署。
