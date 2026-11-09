# 后续前端接入契约

公共前缀 /api/v1，Bearer 单用户认证。OpenAPI 快照见 openapi.json，运行时以 /openapi.json 为准。创建任务、任务命令、输入及会话消息必须携带 Idempotency-Key。

## 项目示例

POST /api/v1/projects：

~~~json
{
  "repository": "your-login/your-repository",
  "default_branch": "main",
  "policy": {"allowed_actors": ["your-login"], "agent_login": "your-bot-login", "mode": "observe"},
  "config": {
    "setup_commands": ["pip install -r requirements.txt", "pip install pytest"],
    "verify_commands": ["python -m pytest -q"],
    "notify_feishu": true,
    "limits": {"turn_seconds": 1800, "task_seconds": 7200, "tokens": 500000, "ci_repairs": 3}
  }
}
~~~

Node 项目可使用 npm ci 和 npm test。安装及验证命令在同一无凭据 helper 运行；其进程和根文件系统不保留，恢复会重新安装。代码工作目录和 Pi 会话独立持久保留。

## 主要调用

- POST /conversations：project_id。POST /conversations/{id}/messages：text、可选 task_id、scope。立即返回 message_id/job_id，Router/Engine 异步处理。
- POST /tasks：project_id/title/goal/acceptance/scope/issue_number/priority。
- GET /tasks：支持 project_id/status；GET /tasks/{id} 返回详情与依赖。
- POST /tasks/{id}/commands：action 为 pause/resume/cancel/retry/priority/replace_session/authorize。
- POST /tasks/{id}/dependencies：prerequisite_id，拒绝循环和跨项目依赖。
- POST /tasks/{id}/inputs：text，进入 Router/Engine 判断需求变化。
- GET /tasks/{id}/sessions、attempts、findings、attentions、artifacts、effects。
- POST /tasks/{id}/attentions/{attention_id}/answer：text。
- GET /projects/{id}/candidates：观察记录；GET /projects/{id}/decisions：管理动作和拒绝原因。
- GET /events?project_id=...，GET /tasks/{id}/events，GET /conversations/{id}/events。

## SSE

事件接口支持 stream=true、after=<sequence> 与 Last-Event-ID。data 为 Event JSON，id 为全局递增 sequence；重连使用最后 id，客户端按 id 去重。空闲时发注释心跳。

主要类型：conversation.message、task.created/state/priority/dependency/input、execution.progress/question/ready/recovery、router.decision/error、reviewer.review/error、watcher.finding/review、source.effect_error、verification.result、github.pr/ci、project.pause、effect.failed。

前端优先显示未决 Attention 和 ready_for_review，其次 running/queued。普通进度不弹通知。失败、未验证、CI missing 与完成必须区别展示。

/internal 是短期 Runner capability 接口，不供前端调用。Pi 私有内部格式与模型隐藏推理不作为公共契约。


## 工作台补充查询

- GET /conversations?project_id=...：项目会话列表，最近创建的在前，最多 200。
- GET /conversations/{id}/messages?limit=100：返回最新一页，页内按时间正序；limit 为 1–200。
- GET /conversations/{id}/messages?before_id=<message_id>：加载更早一页；同时间戳按消息 ID 稳定排序，拒绝跨会话游标。兼容 before=<timestamp>。
- GET /projects/{id}/attentions：项目未决人工问题，按时间倒序；unresolved=false 包含已回答记录。最多 200。

所有补充查询均需要 Owner Bearer Token。前端通过同源代理转发认证，SSE 使用 fetch 流携带 Authorization；cursor 仅在成功解码事件后前进，重连后按 sequence 去重。

## Super-agent 后端接口

详见 [SUPERAGENT.md](SUPERAGENT.md)。前端已接入暂停/恢复、健康摘要、Router 记忆与 Watchdog 查询/维护指令；旧接口保持兼容。

### 策略与准入

- `PATCH /projects/{id}` 的 `policy` 现可包含 `mode`（off/observe/auto）、`rules`、`admission`、`source_scopes`、`source_allowed`、`bots`。变更会提升 policy_version，并向受影响的活动 Task 投递 `policy-changed` Input。
- `POST /projects/{id}/policy`：同样的策略字段，返回 `policy_version`、受影响的 `tickets` 数、重新排队的 `requeued_evals` 数。
- `POST /projects/{id}/spec`：`text`、可选 `version`、可选 `task_ids`。提升 `spec_version`，并向在线 session 投递 `spec_update` steer；规范落后的 Task 不能进入 `ready_for_review` 或发布。
- `POST /feishu/bind`：`project_id`、`chat_id`、`bot_open_id`、`allowed_open_ids`。写入 Feishu 自己的身份列表，不映射到 GitHub login。

### 触发器与线程

- `GET /projects/{id}/triggers?status=decided|undecided|filtered`：不可变事件记录。
- `GET /triggers/{id}`：单条 Trigger，含 `disposition`、`thread_keys`、`channel`。
- `POST /triggers/{id}/requeue`：把已记录但未进入 Eval 的 Trigger 重新排队。
- `POST /triggers/{id}/route`：人工路由。body：`kind`（accept/attach/stop/ignore）、可选 `task_id`、`title`、`goal`、`acceptance`、`scope`、`reason`。只接受门控为 explicit 的 Trigger。
- `GET /projects/{id}/threads`：线程列表，含 `pending`、`evaluating`、`subscriptions`、`context`。
- `GET /threads/{key}`：单线程详情和待评估 Trigger。key 形如 `github:issue:7`、`github:pr:12`、`feishu:chat:oc_xxx`。
- `GET /projects/{id}/evals`：accept/ignore/stop/unavailable 的 Decision，含 strength、mandate、audit。

### 任务控制面

- `GET /tasks/{id}/stage`：派生阶段（person/answer、person/review、person/blocked、working/queued、working、idle/waiting、ended）。
- `POST /tasks/{id}/steer`：`kind` 为 correction/constraint/spec_update/note/stop/status，`text`，可选 `version`。直接写入运行中的 session，不经过 Router 回合。

### Router、Watchdog 与运行控制

- `GET /projects/{id}/router`：项目级 Router 记忆（summary、memory、notices）。
- `GET /projects/{id}/health`：pause 状态、运行/等待工单、待评估 Trigger、pending/failed Effect、未决 Attention、未解决 finding、unavailable Decision。
- `POST /projects/{id}/pause`：body `{paused: bool}`。暂停停止新派发、新 Eval、Input 投递与回复，外部事件继续记录。
- `GET /projects/{id}/watchdog`：独立 Watchdog 的有界状态（open tickets、findings、recent reviews、effects、instructions）。
- `POST /projects/{id}/watchdog/ask`：body `{text}`，用当前有界状态回答维护者问题。
- `POST /projects/{id}/watchdog/instructions`：body `{text}`，记录运行时维护指令，Watchdog 下一次复核/查询会读取。
- `POST /projects/{id}/watchdog/instructions/{instruction_id}/retire`：停用一条维护指令。

### Runner thread control

- `POST /internal/attempts/{attempt_id}/threads/follow`：body `{thread_key, reason}`，以 context-only 关注相关 Thread，不授予权限。
- `POST /internal/attempts/{attempt_id}/threads/unrelated`：body `{thread_key, reason}`，放弃该 Ticket 的订阅并把手上的消息交回路由。不能放弃工单起始 Thread 或自己打开的 PR。

### 事件类型新增

`trigger.received`、`eval.started`、`decision.recorded`、`ticket.created/resumed/ended/steered`、`ticket.spec_ack`、`project.spec_changed`、`policy.changed/effect`、`assessment.error`、`router.error`。

### 语义要点

- `explicit_candidate` Inbox 只由 Router 处理；Engine 和 Worker 不处理它。
- 手工 `POST /triggers/{id}/route` 幂等：已有 Decision 的 Trigger 复用原工单，不重复建票。
- 只有 gate 判定为 explicit 的 Trigger 才进入 router；inferred Trigger 进入 Eval。
- 已订阅线程上的消息直接投递给 Task，不经过模型，也不授予新权限。
- CI 事件记录为 Trigger 和上下文，不产生新工作。
- 没有启用规则时，inferred 候选只记录 Decision，不创建 Task；`observe` 模式记录 `audit.would_accept`。
