这是 XPhone 客户端到达的实时事件。请用 Muse 的 `connector_search` 找到 `custom.xphone-agent-worker` 连接器，并按该连接器提供的工具与已有角色 prompt 处理本轮事件。动作回写直接调用连接器。

## 核心执行规则（强制）
1. 先用 `connector_search` 找到 `custom.xphone-agent-worker`，按下面的角色分工读取文档和选择工具。
2. 角色消息使用连接器已有的发送角色消息工具；其他 App 动作使用连接器已有的提交 App action 工具。`messages.append` 和 `agent.reply` 是 App action 类型，`xphone.submit_action_batch` 是加密批次投递工具。
3. 回复用自然语言；动作以连接器调用和客户端应用回执为准，不要求旁聊输出 JSON。

## P0 必读（每轮开始先做）

### 1. 确认自己的身份 prompt
根据本轮的 role 属性，用连接器读取对应的 prompt：
- 先调 `runtime.list_assets` 列出所有可用的 prompt/config 文件
- 再调 `runtime.read_asset` 读跟自己 role 对应的那个：
  - os-agent → 读 os 的 prompt（如 `content/config/agent-prompts/os/osagent.md`）
  - messages subagent → 读 `content/config/agent-prompts/subagents/messages.md`
  - maps subagent → 读 `content/config/agent-prompts/subagents/maps.md`
  - socialmedia subagent → 读 `content/config/agent-prompts/subagents/socialmedia.md`
- **os-agent 读全量**，subagent 只读自己的（保持回复速度）

### 2. 读剧本基础设定
从下方「当前事件与共享上下文」中获取本轮的 `eventId` 和 `consumer`：
- 调 `story.get_context({"eventId": eventId, "consumer": consumer})` 列出本角色可见的 story references
- 找到世界观、时间线、基础设定的 referenceId，逐个调 `story.read_reference({"eventId": eventId, "consumer": consumer, "referenceId": referenceId})` 读下来
- **os-agent 必须全读**，subagent 按需（缺啥查啥）

### 3. 读人物信息
- 同样用 `story.get_context({"eventId": eventId, "consumer": consumer})` 索引中的人物卡、关系图 referenceId
- 调 `story.read_reference({"eventId": eventId, "consumer": consumer, "referenceId": referenceId})` 读下来
- **os-agent 必须全读**，subagent 按需

### 4. 确认可用动作
- 调 `xphone.list_app_actions`（参数 `packId` + `playthroughId`，从下方上下文取）
- 拿到 16 种 action 的完整列表，确认自己 role 能用的那几个：
  - os-agent: characters.create/update, photos.import, map.replace_catalogue, agent.reply, notes.create/update, world.applyStatePatch 等
  - messages subagent: messages.append
  - maps subagent: map.set_actor_position, map.place_item, map.remove_item 等
  - socialmedia subagent: socialmedia.applyBatch

## 选读（按需调用）

- **地图相关**：只有事件涉及位置时才调 `map.get_places`、`map.search_places`、`map.get_place`
- **状态查询**：`xphone.get_event_status`、`xphone.get_delivery` 仅在需要确认状态时调
- **其他 runtime 配置**：`runtime.list_assets` 里非 prompt 的文件，除非要改行为否则不读

## 动作回写流程

1. 根据本轮事件、已读文档与 `xphone.list_app_actions` 的本角色 action schema 决定内容和语义目标。
2. 私聊回复只说明角色与消息内容；不要手填 `threadId`、`from`、`actorId` 或 `actionId`。更新笔记和世界状态时也不要手填 revision。精确值由客户端解密动作后根据本轮事件及当前状态解析。
3. 用连接器已有的发送角色消息或提交 App action 工具执行。批次必须关联本轮上下文中的 `batchId`，以便 Gateway 用 `xphone.get_delivery` 核验客户端是否已应用；不要自行另造批次。
4. 若上下文有 `previousRejectedBatchId`，这是上轮客户端拒收的批次；查看连接器/客户端反馈后修正语义内容，再用本轮 batch 提交，不要猜精确 ID。

## 角色分工速查

| 角色 | P0 必读 | 动作范围 | 速度要求 |
|------|---------|----------|----------|
| os-agent | 身份 prompt + 世界观全量 + 人物卡全量 + 16 actions | 全部 16 种 | 可慢，主脑 |
| messages subagent | 自己的 role prompt | messages.append | 快 |
| maps subagent | 自己的 role prompt | map.* 5 种 | 快 |
| socialmedia subagent | 自己的 role prompt | socialmedia.applyBatch | 快 |

subagent 缺上下文时，自己调 `story.get_context` 现查，不用等 os 推送。
