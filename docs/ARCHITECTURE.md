# 架构与状态语义

## 责任

目标分层与边界见 [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)。系统由确定性 Engine、Router（路由与项目记忆）、Worker（每 Ticket 的隔离执行）、独立 Watchdog（存活与偏离监督）以及产品附加的 Delivery（验证与发布）组成。Task/Decision/Artifact/Trigger/Thread 是权威事实；Router 的状态是项目级记忆，不替代这些事实。所有外部副作用先入库，再由对应层执行。

Task 保存目标、验收、授权版本和累计预算；ExecutionSession 保存上下文；Attempt 保存一次执行租约。替换 Session 不清除代码、目标、输入或预算。Pi 会话保存在任务 volume 的 sessions 子目录，checkout 在 repo 子目录。

Router 不执行编码。Pi 可搜索、阅读、编辑、运行命令并提交结构化报告；分析任务不启用编辑和命令。自述的“测试通过”只是 proposal；宿主在另一个无凭据 helper 容器运行验证命令并绑定 SHA。

Watchdog 在独立进程（`watchdog_main.py`）中每五秒检查租约、停滞、重复失败和外部等待；语义复核在有新证据且达到检查条件时运行。负 finding 必须引用本次提供的 Artifact ID，相同证据不重复调用模型；两次自动纠偏后仍有问题转人工决定。负 finding 直接以 `steer` 写入运行中的 session。Watchdog 只做监督，不决定交付。交付前的最终复核由 Delivery 层的 `reviewer.py` 执行，因此 Watchdog 停机不会阻断正常发布。

## Super-agent 分层

详见 [SUPERAGENT.md](SUPERAGENT.md)。当前后端由以下层组成，按确定性从高到低：

- **Engine**（`triggers.py`、`tickets.py`、`policy.py`、`ownership.py`、`effects.py`）：确定性事件底座；Trigger/Thread/Subscription、Gate、Mandate、Ticket 状态、Input、Ownership 与 Effect。没有模型参与。
- **Router**（`router.py`、`assessment.py`、`router_state.py`）：无编码工具的路由层。硬编码规则先处理已订阅线程和编号关联；其余 explicit 请求做 accept/attach/stop/ignore，inferred 候选做研判。`RouterState` 保存项目级记忆、后续消息、Worker 报告与决策；人类入口统一走 Router。
- **Worker**（`worker.py`、`execution.py`、`pi-service`）：每个 Ticket 一个持久 Session、目录与容器；接收 Input、执行、报告、handback、关注/放弃 Thread；可 idle 并被订阅消息唤醒。
- **Watchdog**（`watcher.py`、`watchdog_main.py`）：独立进程与失败域；存活恢复、进展/偏离判断、纠偏、人工升级；可查询状态、记录运行时维护指令；不参与交付放行。
- **Delivery**（`delivery.py`、`reviewer.py`）：宿主验证、交付前最终复核、发布与人工合并门禁；确定性运行，不依赖 Watchdog。
- **Sources**（`github_events.py`、`feishu_receiver.py`、`changes.py`）：Source 只产出 Trigger/事件；CI/PR/Attention 的领域反应由 `changes.py` 消费。

## 生命周期

- queued：等待名额、前置依赖或预算核验。
- running：唯一当前 Attempt，Runner capability 仅在租约有效时生效。
- waiting_external：等待 CI、宿主验证、语义复核或其他外部条件。
- waiting_human：有未回答 Attention。
- ready_for_review：当前 SHA 的本地验证和 CI 均通过。
- paused：暂停，自动事件不能恢复用户暂停。
- completed：方案交付，或实现 PR 合并。
- failed：不可自动继续，需要显式重试。
- cancelled：取消或 PR 关闭未合并，仅人可恢复。

等待和暂停释放名额。输入不自动恢复 failed/cancelled/waiting_human。Attention 回复绑定具体问题。PR 重新打开只记录提醒，必须显式恢复。

项目级 `paused` 停止新派发、新 Eval、Input 投递与回复，但继续记录外部事件；运行中的执行回合不被中断。`GET /projects/{id}/health` 汇总 pause 状态、运行中的工单、待评估 Trigger、失败 Effect、未决 Attention 与监督发现。

## 可靠性

先入库再应答 Webhook；投递 ID、业务消息 ID、CI run/attempt/conclusion 去重。事件带 project/task/attempt/cause 标识与递增 sequence。REST 幂等键复用但改变正文返回 409。

Router/Reviewer 和任务执行分别使用租约。调度器通过 PostgreSQL advisory lock 保证跨 Worker 并发上限；同项目最多一个开发任务运行，跨项目默认最多两个。带未读人类后续消息的工单优先调度，并可占用一个额外 slot，避免人的消息排在批任务之后。

输入先记录投递，再由 Pi 模型边界回报 inputs_consumed；ack 不等于消费。未消费输入在恢复后重送。过期 Attempt 的模型、工具和报告能力失效，接替者启动前移除旧容器。

外部副作用采用至少一次投递。PR 按任务分支查重，评论按稳定 HTML 标记查重。飞书超时后可能重复，不能宣称严格 exactly-once。

## Pi fork

上游 commit 见 vendor/PI-UPSTREAM.json。新增 attachHostedExecution，在模型与工具边界执行 fail-closed 宿主检查，保留原 Pi hooks，并返回明确停止原因。bridge 使用空资源加载器，不扫描仓库可执行扩展。

协议为 LF 分隔 JSONL，不能把 Unicode U+2028/U+2029 当作记录边界。stdout 只传协议，stderr 诊断脱敏。ack/report/inputs_consumed/boundary/settled 含义不同；Session 停止不等于任务完成。

vendor/pi-model-data 保存与固定源码匹配的模型元数据快照；Docker 使用 offline build，避免构建时查询动态模型目录。升级 Pi 时一起审阅和更新快照。
