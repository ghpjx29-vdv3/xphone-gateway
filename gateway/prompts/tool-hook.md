---
[可用工具提醒]
- 查剧本/人物：读本轮“可读故事资料”列出的云电脑文件
- 查共享记忆：读本轮“共享摘要”与“重点事实”指向的云电脑文件
- 查地图：`map.get_places` / `map.search_places`（仅当事件涉及位置）
- 查状态：`xphone.get_event_status`（仅当需要确认时）
- OS 交办具体 App 操作：DM、地图、社媒、笔记、世界、角色、照片等动作，先在本轮“交办任务目录”写 <uuid4>.md 文档，再调用 `xphone.delegate_task({"taskPath":"该文档绝对路径"})`。返回任务编号表示已受理。需要确认结果时用 `xphone.get_delegation`，状态为 `applied` 后才能告知用户操作已完成。回复用户消息时，把最终回复写成 `agent.reply` 并提交本轮事件任务文档。
- 动作提交：在任务文档同目录写同名 `.actions.json` 纯语义动作数组，再调用 `xphone.submit_task_actions`，只传原任务文档路径。OS 的 Journal 正文和自身回复使用下方“本轮动作任务文件”。
如需详细信息，读本地文档或调连接器查询状态，不要猜测。
---
