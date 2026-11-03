---
[可用工具提醒]
- 查剧本/人物：读本轮 `localContent.readableDocuments` 列出的云电脑文件
- 查地图：`map.get_places` / `map.search_places`（仅当事件涉及位置）
- 查状态：`xphone.get_event_status`（仅当需要确认时）
- OS 派工：DM、地图、社媒、笔记、世界、角色、照片等动作先写本地任务文件，再调 `xphone.delegate_task`；`taskFile` 必填。派工 ack 后继续，结果下轮读 ledger。
- subagent 提交动作：`xphone.submit_action_batch`，使用本轮 batchId；OS 仅直接提交 Journal 正文和自身回复。
如需详细信息，读本地文档或调连接器查询状态，不要猜测。
---
