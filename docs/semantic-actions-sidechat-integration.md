# Muse 旁聊语义 Action 联调（2026-11-03）

## 本轮改动

- Gateway 首次唤醒一个固定旁聊时发送 `prompts/agent-turn.md` 的角色阅读与工具导览；后续事件只发本轮上下文，并在末尾加 `prompts/tool-hook.md`。OS 后台临时旁聊每次都是新会话，仍会收到完整导览。
- Gateway 不再预读并注入 Worker 的 OS/角色 prompt、reference 正文或 16 种 action schema。旁聊按 `agent-turn.md` 的必读/选读要求，通过 Muse 内置 XPhone 连接器自己读取。
- Worker 的 `xphone.list_app_actions` 改为描述语义参数。Worker 仍只转发加密批次，不解析动作正文。
- XPhone 客户端收到并解密 action 后，根据本轮事件、角色目录与当前状态补齐 Messages 的 `threadId/from`、新 action ID、笔记 ID/revision、世界状态 revision，以及部分地图/社媒目标。

## 更新顺序

1. 部署本次 Worker 代码：在 `E:\NewProject\xphone-agent-worker` 执行 `npm run deploy`，确认 `/health` 返回 `ok`。
2. 让 XPhone 前端运行包含 `semanticActionTargets.ts` 与 `remoteAgentRuntime.ts` 改动的版本。旧前端不认识语义 action，会拒收。
3. 在 Muse VM 更新 Gateway，并确认 `gateway/prompts/agent-turn.md`、`gateway/prompts/tool-hook.md` 已复制：

   ```bash
   cd ~/xphone-gateway && git pull
   cp -a gateway/. ~/xphone/gateway/
   sudo systemctl restart xphone-gateway
   sudo systemctl status xphone-gateway --no-pager
   ```

4. Muse 旁聊中刷新 XPhone 连接器的工具说明，确认 `xphone.list_app_actions` 返回语义字段；角色 prompt 的读取继续走内置连接器已有能力。

## 三个立即可测的回合

### 1. 晚舟私聊

在 XPhone Agent 模式给晚舟发一条新消息。旁聊先按 `agent-turn.md` 读取 Messages 角色 prompt 和必要的角色资料，再用现有连接器发送角色消息/提交 App action。`messages.append` 的语义 action 只需内容，例如 `{"type":"messages.append","text":"今晚我会到。"}`。**不要让模型填写** `threadId`、`from`、`actorId`、`actionId` 或 revision。

客户端根据该 batch 对应的原始 Messages 事件，填入真实线程与注册名（即使角色在剧情里叫“晚舟”，客户端注册名为 `Wade`）。检查手机只出现一条消息，Worker delivery 为 `applied`，Gateway 日志出现 `muse event completed`。

**批次关联是现有协议要求**：连接器投递时须沿用唤醒上下文中的 `batchId`。Gateway 用这个 ID 查询 `xphone.get_delivery`，客户端也用它找回本轮事件。若连接器自行生成其他 batchId，客户端会拒绝无事件关联的语义私聊，Gateway 也不会将事件标记完成。`batchId` 是传输关联值，不属于角色消息的语义字段。

### 2. 同一私聊连续两轮

再发一条消息。第二轮应复用同一 Muse 旁聊：唤醒正文不再含完整角色 prompt 或 `agent-turn.md`，末尾仍有原样的 `[可用工具提醒]` hook。旁聊按已有角色指令读取必要文档；hook 中的地图工具注明仅当事件涉及位置才调用。记录两轮 `muse-cli send` 文本长度，并确认第二轮明显短于首轮。

### 3. OS 与其他动作

在 OS 对话发送请求，确认首次能按原有 OS prompt 读取世界观、时间线和人物资料，之后按需查询；连续对话仍能提交 `agent.reply`。笔记更新可以用笔记标题指代目标，由客户端填 `noteId/baseRevision`；世界状态更新由客户端填 `expectedStateRevision`。地图地点名称只在客户端现有地图目录可找到该地点时解析；社媒评论可用本轮帖子作为目标。找不到或目标不唯一时应拒绝并反馈，不让模型猜 ID。

## 排查记录

联调时记下 `eventId`、`batchId`、Muse sessionId、Gateway 日志时间和 Worker delivery 状态；不要记录密钥或私聊明文。若 delivery 为 `rejected`，客户端加密 receipt 已带拒收原因，Gateway 下一轮会把它放入 `previousRejectedReason`。Muse 侧如看不到该原因，先核对 `receipt.<batchId>` 是否已到 Gateway inbox。

若旁聊能读故事文档但读不到角色 prompt，检查 **Muse 内置连接器的角色文档读取调用**。原始 Worker `/mcp` 的普通凭证与 Muse 内置连接器的应用能力不是同一份工具清单；不要仅凭 Worker 的 `tools/list` 推断 Muse 旁聊能力。

## 本轮边界

- 客户端可稳定解析**当前 Messages 事件**对应的角色和线程；OS 主动给另一个角色发消息时，需要在语义 action 中给出其显示名。
- 世界变量目前仅能用已声明变量 ID 或唯一的末段名称匹配；故事包若只给中文别名、没有变量映射，客户端会拒绝未知变量。
- 地图位置优先使用当前地图事件与本地地点目录；未加载到客户端的地点不会靠模型猜 ID。
- 前端仓库本来有大量其他未提交改动，本轮只涉及 `src/agentruntime/remoteAgentRuntime.ts` 与新增的 `semanticActionTargets.ts`。联调时使用当前本地前端构建即可。
