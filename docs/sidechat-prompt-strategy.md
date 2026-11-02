# Side Chat Prompt 策略：两个要害

## 要害 1：Tiered Prompt 加载（OS 全量 vs Subagent 极简）

### 问题
如果每个 subagent 每次都灌入全量 prompt（身份 + 世界观 + 人物卡 + 16 种 action），回复会很慢。OS agent 需要全上下文，但 subagent 不需要。

### 方案
**os-agent（读全量 P0）**：
- `runtime.list_assets` → `runtime.read_asset` 读自己的 OS prompt
- `story.get_context` → `story.read_reference` 读世界观、时间线（全量）
- `story.get_context` → `story.read_reference` 读人物卡、关系图（全量）
- 16 种 action 类型全知道（从 `xphone.list_app_actions` 拉）
- 慢一点可接受，它是主脑

**subagent（极简启动）**：
- 只读自己的 role prompt（如 `messages.md`，几百字）
- 不预读 story，不读人物卡
- 只知道自己 role 相关的 1-2 种 action：
  - messages subagent: `messages.append`
  - maps subagent: `map.set_actor_position`, `map.place_item`, etc.
  - socialmedia subagent: `socialmedia.applyBatch`
- 事件来了直接干，prompt 短，回复快

**按需拉取**：
- subagent 如果缺上下文，自己调 `story.get_context` / `story.read_reference` 现查
- 不用 os 推送，不用网关中转

### 网关实现
`build_prompt` 拆两版：
- os 版：完整 P0 + 16 actions
- subagent 版：role 身份 + 1-2 actions + "缺啥自己调 story 工具查"

---

## 要害 2：OS 与 Subagent 当前无共享上下文

### 现状
- os-agent side chat 和各 subagent side chat 是**完全隔离**的 Muse 会话
- 各自有独立 conversation_id：
  - os-agent: `{playthrough}:os-agent:ops`（或 payload 自带的 conversationId）
  - subagent: `{playthrough}:{roleId}`（如 `{playthrough}:messages`）
- 上下文不互通，网关不在中间传话
- 网关只按 `route.role` 路由事件，不做上下文同步

### 影响
- subagent 不知道 os-agent 刚才干了什么
- os-agent 不知道 subagent 的执行结果（除非通过事件回流）
- 靠"各聊各的" + "按需自查" 维持，不是真共享

### 未来可选
- 网关在 dispatch 给 subagent 时，附带一个精简的上下文摘要（需要网关能读到 os 会话内容，目前做不到）
- 或保持现状：subagent 按需调 `story.*` 工具自查，不依赖 os 推送

---

## 验证状态（2026-11-03）
- ✅ 连接器凭证已修复（34 工具，200 OK）
- ✅ 旁聊可 `connector_search` 到 `custom.xphone-agent-worker`
- ✅ 旁聊可用内容密钥（用户已批）
- ✅ `xphone.list_app_actions` 返回 16 种 action（已同步到 prompt）
- ⏳ 网关 prompt 拆两版（待实现）
