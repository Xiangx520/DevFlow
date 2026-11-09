# Super-agent 架构迁移

本文件描述 DevFlow Recode 与 super-agent 三层架构的映射、边界和当前落地状态。参考实现为
`dsh-superagent-prototype` 及其设计文章 *Exploring Super-agent Design*。DevFlow 只借鉴领域模型与运行原则，不复制其 DSH/Profile 部署方式。

三层是逻辑分层，不要求每层都是单个 LLM Session：Router、Worker、Watchdog 可以是独立服务、持久会话或确定性处理器。关键是状态持久、职责单一和失败隔离。完整边界见 [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)。

## 原则

1. **确定性门控，模型只做判断。** 谁能发起工作、能发起多强的工作由 `policy.gate` 决定；模型只能在门控允许的范围内 accept / ignore / stop。
2. **提案 ≠ 授权。** 讨论、issue 正文和外部消息都是不可信数据。被动发现只能产生 inferred candidate；只有 `auto` 模式下命中规则才建工单，否则只记录 `would_accept`。
3. **硬编码规则优先。** 已订阅线程上的消息直接投递给对应 Task，不经过模型；PR/Issue 编号直接关联既有 Task。
4. **控制面直达 session。** Watchdog、Router 和用户都可以通过 steer 输入直接切入运行中的 Pi session。
5. **向用户少说话。** 只在需要人行动或结果可用时通知。
6. **监督与交付分离。** Watchdog 只做存活、进展和偏离监督；交付前的最终复核、验证和发布属于 Delivery。Watchdog 停机不阻断正常发布。

## 领域映射

| super agent | DevFlow 实现 | 说明 |
| --- | --- | --- |
| Source | `github_events.py`、`feishu_receiver.py`、`api_request` | GitHub / Feishu / API；Source 只产出 Trigger 和事件，不直接改 Ticket |
| Trigger | `Trigger` 表 | 不可变事件；actor/human/addressed/thread_keys/channel/disposition |
| Thread | `Thread` 表 | `github:issue:<n>`、`github:pr:<n>`、`feishu:chat:<id>`、`api:conversation:<id>` |
| Correlation | Trigger 的 `thread_keys` + Thread 关联 | 只补充上下文，不路由 |
| Channel | Trigger.channel + Effect `notification`/`feishu_reply` | GitHub 评论、Feishu 会话、PR |
| Subscription | `Thread.subscriptions` / `Thread.context` | 订阅消息直接投递给 Task；context-only 不授予权限、不恢复 ended Task |
| Policy | `Project.policy` + `PolicyRevision` + `policy_version` | allowed_actors、bots、rules、admission、source_scopes、accept mode |
| Eval | `triggers.evaluate_due` + `assessment.py` | Thread 级批量、门控 + 模型研判、超时重排、unavailable |
| Decision | `Decision` | accept / ignore / stop / unavailable；手工 route 幂等 |
| Mandate | `Task.mandate` | strength、policy_version、basis、scope、rules；恢复前重新校验 |
| Ticket | `Task` | 只由 accept 创建；follows、ended_by、outcome |
| Assignment | `Attempt` | 一次执行租约；report_state；过期能力失效 |
| Input | `TaskInput` | text / trigger / note / policy-changed / steer / spec_update |
| Report | Pi 协议 + `Task.report_state` | progress / waiting / ready / blocked / failed / dropped |
| Router | `router.py` + `assessment.py` + `router_state.py` | 无编码工具；显式分发、推断研判、项目级记忆、follow-up/worker report 通知 |
| Worker | `worker.py` + `execution.py` + `pi-service` | 每 Task 一个持久 session/目录/容器；可 idle、被订阅消息唤醒、关注/放弃 Thread |
| Watchdog | `watcher.py` + `watchdog_main.py` | 独立进程；存活恢复、进展/偏离判断、纠偏、查询、运行时维护指令 |
| Delivery | `delivery.py` + `reviewer.py` + `effects.py` | 宿主验证、交付前最终复核、发布与人工合并门禁 |

## 核心不变量

- 只有 Router 的 accept 或 Assessment 的 accept 能创建 Task；owner API 也走 `router.accept_owner_request`。
- 门控失败、非 `auto` 的 inferred、规则未命中的 accept 一律拒绝。
- 线程订阅投递只授予上下文，不授予授权；人的消息能让 ended Task 在 Mandate 仍有效时恢复。
- 每个 Task 的 `Mandate.policy_version` 在恢复前必须通过当前 Policy 重新校验。
- `spec_version` 落后的 Task 不能进入 `ready_for_review` 或发布。
- `Reviewer` 的最终复核必须绑定当前输入、Policy、Spec、SHA 与证据；任一变化使旧结论失效。
- Watchdog 不写 `ReviewRecord`，不参与 `delivery.eligible`；Watchdog 的 finding 只用于纠偏和人工升级。
- 所有外部副作用走 Effect，至少一次；PR 按分支、评论按稳定标记去重。

## 运行形态

- **Engine**：确定性事件底座；Source → Trigger → Thread → Subscription、Gate、Mandate、Ticket、Ownership、Effect。
- **Router**：目前是持久状态服务 + 每事件模型调用；`RouterState` 保存项目摘要、最近决策、后续消息、Worker 报告和通知。尚未实现持久 LLM Session 与跨 trigger 单回合批处理。
- **Worker**：每 Ticket 一个持久 Pi session；turn 结束无终态报告时进入 waiting，订阅消息可唤醒；支持 context follow 和 mark unrelated。
- **Watchdog**：独立 `devflow.watchdog_main` 进程；共享数据库，不共享进程。提供有界状态查询和运行时维护指令；目前是固定 schema 的模型调用，不是完整工具集 Agent Session。
- **Delivery**：确定性验证 + Reviewer 最终复核 + 发布 Effect；可在没有 Watchdog 的情况下完成正常交付。

## 后端接口

- `GET /api/v1/projects/{id}/triggers`、`GET /api/v1/triggers/{id}`
- `POST /api/v1/triggers/{id}/requeue`、`POST /api/v1/triggers/{id}/route`（幂等）
- `GET /api/v1/projects/{id}/threads`、`GET /api/v1/threads/{key:path}`
- `GET /api/v1/projects/{id}/evals`
- `GET /api/v1/projects/{id}/router`（Router 记忆）
- `GET /api/v1/projects/{id}/health`（运行与监督摘要）
- `POST /api/v1/projects/{id}/pause`（项目级暂停/恢复）
- `GET /api/v1/projects/{id}/watchdog`、`POST /api/v1/projects/{id}/watchdog/ask`
- `POST /api/v1/projects/{id}/watchdog/instructions`、`POST /api/v1/projects/{id}/watchdog/instructions/{id}/retire`
- `POST /api/v1/projects/{id}/policy`、`POST /api/v1/projects/{id}/spec`
- `POST /api/v1/tasks/{id}/steer`、`GET /api/v1/tasks/{id}/stage`
- `POST /api/v1/feishu/bind`
- Runner: `POST /internal/attempts/{id}/threads/follow`、`POST /internal/attempts/{id}/threads/unrelated`

## 已落地

- Trigger / Thread / Subscription / Decision / Mandate / Ticket / Attempt / Input / Report。
- Policy 版本、admission 表、门控、Mandate 重新校验、Policy 变更投递与 Eval 重排。
- Router 显式分发 + inferred 研判；RouterState 项目级记忆；follow-up、Worker report、Attention、结束和 handback 通知。
- 每 Ticket 隔离 Session/目录/容器；持久 Pi session；turn 结束 → waiting；follow/mark-unrelated。
- 独立 Watchdog 进程；存活恢复、进展/偏离监督、steer、状态查询、维护指令。
- Delivery 最终复核与 Watchdog 解耦；宿主验证、发布 fence、PR/评论去重。
- 项目级 pause、健康摘要、人类后续消息优先调度与额外 slot。
- Source 只产出 Trigger；CI/PR/Attention 领域反应移到 `changes.py`。

## 尚未完成

- Router 仍是每事件模型调用，没有跨 trigger 单回合批处理；`RouterState` 有记忆，但不是完整持久 LLM Session。
- Watchdog 是固定 schema 的模型调用，不是带完整工具的 Agent Session；自然语言查询和维护指令已可用，但自动修复动作仍局限于 steer/暂停/人工升级。
- Worker 尚无后台 job/subagent 宽限、slot 保持或人在 Session 内直接操作的完整体验。
- Outbox 尚无按 Ticket/会话的严格发送顺序与 dropped 状态。
- Source/Channel 仍是内置 GitHub/Feishu/API，不是可插拔插件体系。
- Prompt/Skills 尚无版本化配置面。

## 明确不做

- 跨仓库/跨项目协调。
- 自动 merge。
- 把 Router 变成执行具体开发任务的万能主 Agent。
- 复制 dsh-superagent 的 DSH/Profile 部署方式。
