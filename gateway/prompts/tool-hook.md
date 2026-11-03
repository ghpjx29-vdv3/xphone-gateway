---
[可用工具提醒]
- 查剧本/人物：调连接器 `story.get_context` + `story.read_reference`
- 查地图：`map.get_places` / `map.search_places`（仅当事件涉及位置）
- 查状态：`xphone.get_event_status`（仅当需要确认时）
- 提交动作：`xphone.submit_action_batch`（本轮必须调用）
如需详细信息，调连接器读取，不要猜测。
---