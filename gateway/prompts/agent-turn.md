这是 XPhone 客户端到达的实时事件。请用 Muse 的 `connector_search` 找到 `custom.xphone-agent-worker` 连接器，并按该连接器提供的工具与已有角色 prompt 处理本轮事件。动作回写直接调用连接器。

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

1. 根据用户意图构造 action（参考下方「本角色 action schema」）
2. **重要：必须使用下方上下文中提供的固定 `batchId`，不要自行随机生成！**
3. 从 `~/.config/xphone/content.key` 读取 32 字节密钥（base64url 解码）
4. 用 Python + cryptography 做 AES-256-GCM 加密：
   - `plaintext = JSON({"batchId": batchId, "actions": actions})`
   - `aad = JSON({"v": 1, "packId": packId, "playthroughId": playthroughId, "direction": "client", "kind": "action_batch", "id": batchId})`
   - `nonce = 12 字节随机`
   - `envelope = {"id": batchId, "kind": "action_batch", "version": 1, "nonce": b64u(nonce), "ciphertext": b64u(ct), "aad": b64u(aad)}`
5. 写 `/tmp/xphone_args.json`：`{"envelope": <envelope>}`
6. 执行：`python3 ~/workspace/custom-connectors/custom.xphone-agent-worker/bin/client.py call-tool --name xphone.submit_action_batch --arguments-file /tmp/xphone_args.json`
7. 回复用户自然语言总结，**不要**在回复里输出 JSON 代码块

## 角色分工速查

| 角色 | P0 必读 | 动作范围 | 速度要求 |
|------|---------|----------|----------|
| os-agent | 身份 prompt + 世界观全量 + 人物卡全量 + 16 actions | 全部 16 种 | 可慢，主脑 |
| messages subagent | 自己的 role prompt | messages.append | 快 |
| maps subagent | 自己的 role prompt | map.* 5 种 | 快 |
| socialmedia subagent | 自己的 role prompt | socialmedia.applyBatch | 快 |

subagent 缺上下文时，自己调 `story.get_context` 现查，不用等 os 推送。
