# Muse Side Chat Prompt 与共享上下文

## 当前实现

- Gateway 连接 pack 时通过 MCP 拉取 manifest、故事文档、角色 prompt 和动作清单，在 `~/xphone/content/{packId}/` 建立本地文件树。OS 每轮从云电脑读取自己的角色说明、已解锁故事文档与动作清单；Gateway 只注入当前事件、共享摘要和这些文件的路径。
- Messages、社媒和地图 subagent 各读云电脑上自己的角色 prompt、任务文档与本轮允许的故事资料。输入仅包含目标 surface 和筛选后的共享摘要；其他私聊全文不进入本轮 prompt。
- `xphone.delegate_task` 只接受任务文档绝对路径。Worker 经已有 WS 转发路径，不读取任务正文；Gateway 在本地校验并读取文档，再唤醒对应 subagent。
- `muse-memory.json` 按 pack/playthrough 保存 AI 摘要和已 `applied` 的 ledger。OS 与 subagent 在各自下一轮读取允许范围内的共享记忆；Muse 旁聊自身的历史仍按角色隔离。
- OS 用户对话使用固定旁聊；OS 后台事件使用逐事件临时旁聊，同一事件重试复用原会话。角色 side chat 按 roleId 复用。临时会话完成后删除。CLI `/clear` 的语义仍待 Muse VM 验证。
- subagent 直接调用已有连接器工具提交加密 App action；OS 只直接提交 Journal 正文和自身回复。Gateway 使用固定 batchId 查询 `get_delivery`，客户端 `applied` 后才把普通回复的有界片段记入共享摘要并完成事件。旁聊无需回复 JSON。

## 凭证边界

普通 Muse 连接器会话不能调用 Gateway 专用的原始 `runtime.*`、收件箱和事件租约工具。Gateway 从 `/account/gateway-session` 获取专用短期令牌并读取 pack prompt。Worker 不能解密 action，因此动作内容仍由 XPhone 客户端校验。

实施细节见 [Agent 共享上下文计划](muse-agent-context-plan.md)；实测步骤见 [Muse 与前端联调指南](muse-front-end-integration.md)。
