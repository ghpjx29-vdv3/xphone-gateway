# Muse 个人代理的 Agent 共享上下文与文档读取实施计划

状态：Gateway/Worker 核心链路已实现；Muse VM 会话配额、CLI `/clear` 与前端全链路仍待联调。联调步骤见 `muse-front-end-integration.md`。本文针对 XPhone Agent 模式，不改变 GIGI 的一般聊天模式。

## 1. 目标与已确定的边界

1. GIGI 客户端产生聊天、Journal 和 App 事件；Worker 持久化加密事件并通过 Agent WebSocket 通知 Gateway；Gateway 收到通知后立即用 `muse-cli` 唤醒对应旁聊。定时 Hook 只承担故障恢复，不承担正常消息入口。
2. Muse side chat **直接通过 XPhone 连接器提交加密 App action batch**。Gateway 不从自然语言回复猜测动作，也不代替 side chat 生成剧情动作。
3. Gateway 负责事件路由、旁聊会话映射、按角色装配本轮上下文、共享记忆持久化、租约续期，以及核验 batch 的客户端应用结果。
4. GIGI 保持手机状态的权威来源：提供当前状态和目标 surface 历史的按需观察、校验并应用动作、发出 applied/rejected 回执。Agent 模式的共享摘要由 AI 生成，Gateway 保存；不要求客户端生成摘要。
5. 文档正文按角色知识和当前真相边界读取。OS 拿较广的规划视图；subagent 拿目标角色与 surface 所需的局部视图。两者都不直接消费原始事件或原始运行配置的整包 JSON。

## 2. 参考实现中真正需要移植的机制

`pi-agent-service` 不是“每轮把所有文档灌给模型”。其主要步骤是：

```text
事件 / direct App 请求
  -> 选择当前 window 的候选资料和边界（contextBundle.ts）
  -> 读取叙事状态并计算角色真相投影（narrativeTruth.ts）
  -> 按 consumer、truth gate、loadPolicy 投影文档（contextProjection.ts）
  -> 构造 OS 或 subagent 的语义 prompt view（promptViews.ts）
  -> 模型按需调用受限文档工具（contextDocumentTools.ts）
  -> App 命令 + AI 生成的 memoryPatch
  -> 应用命令，写回摘要、角色状态、window memory 和 ledger
```

具体原则：

- OS 编排轮次忽略旧模型消息，也不保存新消息；主要依靠最新状态、window memory、ledger、相关 surface 信息重建上下文。用户直接和 OS 聊天是另一类需求，不应混进后台编排历史。
- 多数 subagent 以 `roleId` 保存私有模型会话；当前 XPhone 只实施已接入的角色和 App，不照搬未启用的业务模块。
- 文档有 `eager`、可见且按需读取、只可见索引、完全隐藏四种结果。角色自己的必要资料可提前加载，其他可读资料以摘要索引呈现。
- 跨角色连续性有两种：跨轮使用 `surfaceSummary`、window memory 和 ledger；同一轮中需要把前一个任务结果传给后一个任务时，使用带接收者和信息来源的临时 `ccNotice`。旁聊历史不会自动互通。
- `memoryPatch` 由 AI 产出。参考实现的前端保存摘要、角色状态和 ledger；服务端还保存叙事状态与部分角色记忆。因此“参考架构的全部上下文都只在前端”并不准确。

对应源码：`E:\pi-agent-service\src\agent\{osTickPlanner,subagentRunner,contextBundle,contextProjection,promptViews,contextDocumentTools}.ts`、`E:\pi-agent-service\src\agent\modelClient.ts`、`E:\CradleOS\src\store\gameStore.ts`。

## 3. 当前 XPhone 链路与缺口

| 环节 | 当前实现 | 需要完成的工作 |
| --- | --- | --- |
| 客户端事件 | `E:\GIGI\src\agentruntime\remoteAgentRuntime.ts` 按 route 加密发送；事件包含当前 surface 摘要、近期 ledger，OS 事件另含 window memory | Gateway 只把这些字段当输入资料；新共享记忆以 Gateway 为 Agent 模式权威，避免两处各写一份 |
| 当前状态 | GIGI 已支持 `state`、`list_surfaces`、`read_surface`、`event_log` 观察 | 需要真实状态或完整目标历史时，发定向观察；不在每个事件里复制所有 App 历史 |
| Worker | 已有持久队列、WebSocket、租约和 delivery；新增 Gateway 专用会话及按 event route 的文档授权 | Muse VM 需实测普通连接器权限 |
| Gateway | WebSocket 推送触发旁聊；按角色加载 Worker prompt 和共享摘要，独立续租 | Muse VM 需实测提示词和会话开销 |
| 完成判定 | 预先确定 batchId，查询客户端 `applied` 后才完成 | `queued`、纯文本回复和 `rejected` 都不能作为成功 |

`muse-cli` 的 `session-start` 只创建带标题的旁聊，`send --thread` 发送的是普通会话消息。它没有本轮设置真正 system prompt 或限制工具集的接口。Gateway 发出的角色提示能指导行为，**不能单靠提示词强制隔离文档和动作权限**。

## 4. 目标链路

```text
GIGI 事件（加密，含 route、eventId、surfaceId）
  -> Worker 持久队列 + WebSocket 通知
  -> Gateway 拉取、解密、按 cursor 领取租约
  -> Gateway 读取本轮事实、共享记忆、角色可见文档索引和实时 action schema
  -> Gateway 构造 OS / subagent 的语义输入
  -> muse-cli 发送到指定旁聊
  -> side chat 按需读允许的文档，直接调用连接器提交 action batch
  -> Gateway 用已知 batchId 查询 xphone.get_delivery，核实 applied
  -> Gateway 原子写入共享记忆，finish_event，ack_inbox
```

事件到达由 WebSocket 驱动。`muse-cli --wait` 本身会等待旁聊回复；如改用 `--wait 0`，Gateway 仍需跟踪该事件的旁聊结果。这种事件执行期间的结果等待不等于 Cron 扫描新消息。

## 5. 状态与会话所有权

| 数据 | 权威位置 | 用途 |
| --- | --- | --- |
| 手机消息、帖子、Journal、角色目录、世界变量和当前 revision | GIGI 客户端 | 动作校验及真实状态观察；Gateway 不凭旧摘要臆测当前手机内容 |
| 事件、租约、delivery 状态 | Worker Durable Object | 重连、去重、`applied/rejected` 判定 |
| Muse side chat 消息历史 | Muse | 单角色的语言与互动连续性；不作为跨角色事实来源 |
| `conversationKey -> Muse sessionId`、`eventId -> execution` | Gateway 持久状态 | 重启和同事件重试时找回相同会话、batchIds 和执行阶段 |
| `surfaceSummaries`、`agentLedger` | Gateway 持久状态，按 playthrough 隔离 | 有界 AI 回复摘要与已应用动作；每次按角色可见性裁剪 |
| 原始 prompt/config 与真相配置 | Worker 私有 R2，可信 Gateway 可读取 | 构造角色投影；原始隐藏真相不直接进入 side chat |

共享记忆建议采用单独的原子写入文件，放在 Gateway 现有的 `~/xphone/gateway/` 持久目录中；按 `packId + playthroughId` 分区。每条 ledger 至少记录 `eventId`、roleId、surfaceId、状态、已应用 batchIds、AI 摘要和时间。写入以 `eventId` 幂等：重试不能重复追加。实施时确认该目录在 Muse VM 重启或替换后是否仍持久；若不持久，需先选定可恢复的持久存储，不能把旁聊记录当成共享记忆备份。

## 6. Muse 会话策略

| 任务 | 会话键与生命周期 | 本轮上下文 |
| --- | --- | --- |
| 用户直接与 OS 聊天 | 同一 playthrough 的固定 OS chat 旁聊；保留用户对话 | 当前用户输入、OS 可见事实、window memory、相关 ledger |
| OS 后台编排 / Journal 主记录写作 | 先验证每事件临时旁聊的开销；若可接受，每个 `eventId` 新建旁聊，同一事件重试复用，成功后清理。若开销不可接受且 `/clear` 可通过 CLI 对指定旁聊生效，则复用一个 OS ops 旁聊，在下一事件开始前清空上下文 | 每轮重建当前状态、边界、授权文档索引和共享记忆，不继承其他 ops 事件的聊天历史 |
| Messages / 社交 subagent | 按 `playthroughId + roleId` 固定旁聊，roleId 包含目标 window / surface | 私有聊天历史 + 最新角色知识、目标 surface 历史与摘要、可见跨 App 摘要 |
| 独立任务型角色 | 每个任务独立旁聊；仅在实际接入这类角色时启用 | 当前任务事实，不继承上一轮任务的模型消息 |

Muse UI 已显示 `/clear`（“Start a fresh context on the next message”）；本地 `muse-cli` 没有独立的 clear 子命令，但 `send` 能发送任意文本。**不能直接假定** `muse-cli send "/clear" --thread <sessionId>` 会执行 UI 命令，也不能把发送了 `/clear` 等同于清空成功。先在专用测试旁聊验证命令作用域与下一条消息的实际上下文，再启用单 ops 旁聊方案。清空只发生在上一事件已确认 `applied`、记忆已提交之后；同一事件重试不得清空。

会话方案的落地验证：

1. 在专用测试帐号/环境连续创建 20 个旁聊，记录 `session-start` 耗时、错误/限流、会话数量上限和侧边栏表现；验证后删除这些测试旁聊。该测试不在本计划编写阶段执行。
2. 在一个专用测试旁聊发送可识别的无业务测试内容，再通过 `muse-cli send "/clear" --thread ...` 请求清空，发送下一条消息并确认它不再继承前文。若 CLI 仅把 `/clear` 当普通文本，不能使用该方案。
3. 若逐事件新建会话稳定且开销可接受，采用临时旁聊，并在事件完成、回执确认后删除或归档；清理方式以会话数配额和 UI 实测为准。若该方案不可用而 CLI `/clear` 生效，使用单 ops 旁聊并在相邻事件之间 clear。两种都不可用时，不声称实现了 pi 的无历史 OS 编排语义，先明确接受有历史的单会话偏差再实施。

新建旁聊或 `/clear` 都只处理该线程的历史；Muse 可能仍有平台级记忆，需要在验证中观察。旁聊映射应优先使用 route 中的稳定 roleId；客户端的 `conversationId` 只用于定位客户端会话，不自动等同于 Muse 会话键。

## 7. Prompt 构建与文档读取

### 7.1 共同的本轮输入

Gateway 将事件整理为语义字段：触发原因、当前 App 与 surface、用户可见输入、叙事边界、目标对象、当前真实状态摘要、角色允许知道的事实。`flags`、内部 key、缓存字段和完整原始 payload 不作为 prompt 主体；工具确实需要的 `threadId`、`surfaceId`、`postId` 可保留。

### 7.2 OS 输入

- 加载当前 pack 的 OS 基础提示和必要片段；加载当前 window 的边界、玩家及角色目录、相关 App 摘要、window memory、近期 ledger。
- 读取 OS 可见的故事文档**索引和摘要**；本轮触发资料与标记为 eager 的资料可附正文。其余正文由 OS 按需读取。`OS 全量` 指规划视角较广，不等于每轮读取全部故事文件。
- 从 `xphone.list_app_actions` 取得当前 action schema，再只给 OS 可调用的 action 和相应参数；不复制旧的手写 16 项示例作为事实来源。
- OS 可以规划 subagent 任务；任务必须包含目标角色、目标 surface、意图、边界和允许传播的事实。角色实际 App 内容交由对应 side chat 编写。

### 7.3 Subagent 输入

- 使用目标 App/角色的简短基础提示、角色知识投影、当前任务、当前 surface 摘要和近期原文历史。
- 提供当前角色可见的故事文档索引；必要人物卡可以 eager，其他正文按需读取。不得自动给其他角色私聊全文、OS 隐藏资料或所有 App 历史。
- action schema 只保留该角色可调用的类型，例如 Messages 的 `messages.append`、社交的 `socialmedia.applyBatch`。
- 跨 App 只传按可见性筛选的摘要。若同一 OS 编排轮次要求后续任务理解上一任务的结果，用临时 `ccNotice` 标明 `recipientSubject`、`knowledgeMode`、`inUniverseSource`、`storyPurpose`；它不是永久记忆，也不赋予角色额外权限。

### 7.4 文档权限的实际约束

参考实现由 `contextProjection.ts` 与 `contextDocumentTools.ts` 在**服务端工具层**执行可见性判定。实施后的 XPhone Worker 要求故事工具同时提供 `eventId` 与 `consumer`，核对该 consumer 是否等于正在运行事件的 route；原始 `runtime.*` 工具仅允许 Gateway 专用会话使用。Muse 连接器当前使用共享的普通凭证，Worker 尚不能鉴别具体是哪一个旁聊，因此这属于**事件 route 级授权**，不是旁聊身份级强隔离。

Gateway 从 `/account/gateway-session` 取得专用短期凭证；Muse 连接器使用普通凭证。Worker 按凭证隐藏原始配置工具，并按事件 route 和客户端解锁状态过滤故事文档。Worker 无法解密 action 内容，实际 App 动作仍由客户端校验。

## 8. 连接器动作与共享记忆

Gateway 唤醒旁聊时只提示发现并使用 Muse 的 XPhone 连接器，同时提供本轮事件、角色上下文和确定的 batchId。旁聊直接调用连接器，不需要向 Gateway 回复 JSON。Gateway 按 `packId + playthroughId + eventId + step` 确定 batchId，查询 `xphone.get_delivery`；只有客户端回执 `applied` 才完成事件。

`queued` 时继续等待并续租；`rejected` 时保留诊断状态，在下一修正步骤使用新的确定性 batchId。Muse 回复的有界文本片段仅在 `applied` 后记作 AI 产生的共享摘要，来源标记为 `reply_text`；无回复时只记 delivery ledger。普通文本从不作为动作成功依据，也不自动推断角色认知或世界真相。

共享记忆按 pack/playthrough 隔离。OS 可读较广的 surface 摘要与近期 ledger；subagent 只读目标 surface 摘要与对应 ledger。当前实现不要求 Muse 输出结构化 `memoryPatch`，后续若要维护独立角色认知和 window memory，需通过明确的连接器数据合同新增写回能力。

## 9. 事件执行、重试与回执

1. Gateway 按 Worker cursor 顺序处理事件；`start_event` 领取 90 秒租约。领取成功后立即启动独立的续租任务，每 30 秒调用 `xphone.renew_event(eventId, leaseId)`，持续覆盖同步阻塞的 Muse CLI 调用及等待客户端 `applied` 的阶段。续租任务使用本事件固定的 pack/session 凭证与租约；完成、失败、pack 切换或续租失败时停止，续租失败后不得继续提交完成状态。当前阻塞式 `muse-cli send --wait 60` 不能妨碍续租。
2. 为每个事件记录 `eventId -> Muse sessionId、发送状态、batchIds、摘要草稿、租约状态`。进程重启后先查 `get_event_status` 与 `get_delivery`，再决定恢复发送或只补完成步骤。
3. Side chat 对同一事件使用稳定 batchId 与 actionId；已 `queued/applied` 的 batch 不重新生成。若客户端拒绝，应记录拒绝原因，再以明确的新修正步骤提交，不覆盖旧 batch 的身份。
4. Gateway 对每个 batch 调用 `xphone.get_delivery`；只有全部 `applied` 才提交记忆并 `finish_event(completed)`、`ack_inbox`。单纯的 `muse-cli` 文本回复、连接器返回 `queued`、或 Gateway 已发出通知都不足以完成事件。
5. `receipt` 与 `observation_result` 是非工作事件，分别用于更新执行结果和满足正在等待的观察请求；不能作为新的 OS/subagent 工作事件唤醒旁聊。
6. Gateway 继续在 WebSocket 重连时重扫未确认 inbox。Hook 和 watchdog 只负责恢复挂掉的 Gateway；不会代替正常实时入口或另开一套 Agent 处理路径。

## 10. 实施顺序与受影响文件

按调用关系做最小范围修改，不迁移未接入的 Email/Holdings 等旧功能。

1. **固定接口合同与会话能力验证**：使用确定的 batchId、Gateway 记忆记录和会话键；OS ops 先采用逐事件临时会话，20 会话与 CLI `/clear` 仍需 Muse VM 联调。受影响：`gateway/muse_dispatch.py`、Gateway 记忆模块、`gateway/test_muse_dispatch.py`。
2. **分层上下文**：Gateway 按 route 构建 OS/subagent 语义视图，读取并缓存 pack prompt 版本、授权文档索引和实时 action schema；目标 surface 需要更多事实时使用现有观察链路。受影响：`gateway/muse_dispatch.py`、`gateway/account_api.py`（仅在需要专门的工具调用封装时）。
3. **会话与共享记忆**：OS chat/ops 和 subagent 按第 6 节拆分；AI 摘要按 playthrough 持久化并裁剪注入。受影响：`gateway/muse_dispatch.py`、Gateway 记忆模块、`gateway/test_muse_dispatch.py`。
4. **可核验完成**：实现租约续期、batch `applied` 查询、重试恢复和成功后记忆提交。受影响：`gateway/gateway.py`、`gateway/muse_dispatch.py`、`gateway/test_recovery.py`。
5. **文档强隔离**：在明确 Muse 连接器凭证能力后，修改 Worker 的角色文档授权；Gateway 原始配置读取与 side chat 文档读取分权。受影响：`E:\NewProject\xphone-agent-worker\src\index.ts`、相邻鉴权模块及相应测试。此步是达到 `pi-agent-service` 文档隔离强度的必要条件。
6. **同步文档**：更新 `gateway/README.md` 和现有 `docs/sidechat-prompt-strategy.md`，删去“subagent 无共享上下文”“所有角色灌入 16 action”等过时结论，不保留并行旧方案。本轮不覆盖该文档已有的未提交改动。

现有 GIGI `remoteAgentRuntime.ts` 已能发目标事件、回答按需观察并回传应用结果；本计划初始实施不要求改它。只有在实际链路发现缺少某个必要的客户端事实或回执字段时，才扩大修改范围。

## 11. 验收条件

- 客户端发送 OS 消息或角色消息后，Worker WebSocket 通知能直接拉起正确 Muse 旁聊；不等待 Cron 周期。
- 同一个角色连续两次消息进入同一私有旁聊；不同角色不会混入彼此原文。OS 后台编排经实测选择临时旁聊或可用的 `/clear`，不同事件不继承上一事件的 ops 聊天上下文；同一事件重试保留其执行上下文。
- OS 能看当前 window 允许的广域摘要和文档索引；subagent 只收到自己的身份、目标 surface 与允许的共享摘要。锁定真相、原始 flags 和其他私聊全文不进入角色 prompt。
- Subagent 完成后有界的 AI 回复摘要在下一次该角色回合可见；OS 可看到适合规划的摘要；其他角色只能按可见性或明确 `ccNotice` 得知。
- Side chat 提交 batch 后，`queued` 不完成事件；客户端 `applied` 后才写入记忆并 `finish_event/ack_inbox`；`rejected` 不产生成功记忆。旁聊无需回复 JSON；同一事件重试不重复应用动作或追加 ledger。
- 长于 90 秒的 Muse 回合及后续 delivery 等待仍能每 30 秒续租；Gateway 重启后能根据持久会话映射与 Worker 状态继续处理未完成事件。
- 若要求旁聊身份级文档隔离，还需为每个旁聊提供独立作用域凭证；目前仅保证普通凭证不可读取原始 runtime asset，并按所提交的运行事件 route 过滤故事文档。只检查 prompt 内容不能证明身份级隔离。
