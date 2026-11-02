# Muse Side Chat Prompt 与共享上下文

## 当前实现

- OS 读取 Worker pack 的 `osagent.md` 与其 include，叠加 Gateway 的本轮投递协议 `gateway/prompts/agent-turn.md`。输入包含当前用户/系统事件、window memory、角色目录、可见 surface 摘要、最近 ledger 与文档索引。
- Messages、社媒和地图 subagent 各读 Worker pack 中自己的角色 prompt。输入仅包含当前 surface、目标角色、筛选后的共享摘要和本角色 action schema；其他私聊全文不进入本轮 prompt。
- Gateway 从 `xphone.list_app_actions` 取当前 schema 并按 route 过滤，不把全部 action 手册灌给 subagent。角色需要故事正文时使用 `story.get_context(eventId, consumer)` 和 `story.read_reference(eventId, consumer, referenceId)`；Worker 核对 consumer 与事件 route，并结合客户端解锁状态裁决。
- `muse-memory.json` 按 pack/playthrough 保存 AI 摘要和已 `applied` 的 ledger。OS 与 subagent 在各自下一轮读取允许范围内的共享记忆；Muse 旁聊自身的历史仍按角色隔离。
- OS 用户对话使用固定旁聊；OS 后台事件使用逐事件临时旁聊，同一事件重试复用原会话。角色 side chat 按 roleId 复用。临时会话完成后删除。CLI `/clear` 的语义仍待 Muse VM 验证。
- 旁聊直接调用已有连接器工具提交加密 App action 或角色消息；Gateway 使用固定 batchId 查询 `get_delivery`，客户端 `applied` 后才把普通回复的有界片段记入共享摘要并完成事件。旁聊无需回复 JSON。

## 凭证边界

普通 Muse 连接器会话不能调用 Gateway 专用的原始 `runtime.*`、收件箱和事件租约工具。Gateway 从 `/account/gateway-session` 获取专用短期令牌并读取 pack prompt。Worker 不能解密 action，因此动作内容仍由 XPhone 客户端校验。

实施细节见 [Agent 共享上下文计划](muse-agent-context-plan.md)；实测步骤见 [Muse 与前端联调指南](muse-front-end-integration.md)。
