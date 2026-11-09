# Super Agent 改造执行记录

本文件记录实际落地范围，不代表四阶段验收完成。

## 第一阶段：消息归属基础

新增 `message_ownership`：以 Trigger、Task 为联合主键，分别记录投递、读取、处理和释放时间及释放理由。输入消费回执只推进读取时间，不推断处理完成。

handback 释放当前任务对消息的归属，保留订阅。最后一个 owner 释放后才重新排队；已释放的 owner 不接受同一条消息。Worker 不再消费已释放输入。任务结束时释放未读且未处理的归属。context 输入不建立归属，且不能恢复已结束任务。

GitHub webhook 和轮询采用共同的业务消息身份：项目、消息类别、对象 ID、更新时间和正文。编辑产生新修订，投递 ID 不再作为业务去重依据。

数据库结构通过 Alembic 0005 增加；本轮尚未执行开发数据库重建，不删除任何任务 volume。

## 验证与剩余工作

全量领域/API 回归：69 passed、2 skipped（SQLite）。包含 GitHub 修订身份、多 owner handback、任务结束释放和读取/处理隔离测试。真实 PostgreSQL 全量领域/API 回归：70 passed、1 skipped。HTTP 替身、Pi bridge 和任务容器模拟验收尚未执行。

Docker CLI 已定位到 `C:/Users/30107/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe`，引擎可用。`E:/Xiangx/Docker` 为数据目录。隔离测试 PostgreSQL 已使用 `compose.test.yaml` 启动，端口为 55439。

第一阶段仍需统一线程键、完善并发所有权争用处理、延迟 PR 关闭结算并补充端到端恢复测试。第二至四阶段仍待实施，包括移除 Manager 活动路径、持久控制租约、不可变规范/Policy、确定性交付协调、来源补齐及完整模拟验收入口与报告。


## 第二轮实施

第一阶段补入统一 `thread_keys.make/parse/normalize`：项目身份为键的一部分，订阅、follows 与 intake 校验项目；消息去重也按项目隔离。PR 关闭事实不再成为工作输入，执行中的恢复轮次或待处理归属会延迟结束。ready 报告只处理实际读取的消息。新任务直接获得原始请求、来源与相关讨论，Router 产物不再作为实施目标或验收标准。

第二阶段移除 worker 的 Manager 调度；兼容模块不运行模型也不执行任何活动。API 对话和补充输入改为 Trigger，`job_id`/`input_id` 为 `trigger_id` 的兼容别名。新增持久 Control，停止/暂停即时撤销执行能力，普通纠偏进入边界输入。Router 使用自己的 owner/lease_until，不再由 Manager 租约回收。Assessment 独立持有评估 owner/deadline，失败计数保留至第三次 unavailable。模型结果落地前重新校验 Policy 版本、归属和状态。


## 第三阶段

新增不可变 PolicyRevision、SpecRevision、逐任务 SpecReceipt 及 ReviewRecord。项目规范覆盖现有与未来任务；任务级规范不更新项目要求。Worker 获得规范正文，输入消费只记录 read，ready 报告记录应用，独立监督记录验证。监督快照包含原始输入、Mandate、授权版本、Policy 版本、适用规范、SHA 与证据；任一变化会使旧结论失效。

确定性 delivery.coordinate 替代审批 Agent，核对当前宿主验证、有效监督、规范和 finding 后生成发布 Effect；分析结果直接交付。finding 只能由后续宿主验证证据加重新监督收敛。外部写入加授权 fence；push 重试先 ls-remote，PR 与评论按远端标记查询。当前后端回归 76 passed、2 skipped；Pi 编译与 5 项协议/边界测试通过。

## 第三轮：Router / Worker / Watchdog 三层落地

- **Phase 0**：新增 [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)；删除退役 Manager 模块、列和配置；把 `Decision` 默认 kind 改为 `router`；增加架构边界测试。
- **Phase 1**：交付前最终复核拆到 `reviewer.py`（Delivery 层），Watchdog 只做存活/进展/偏离监督并输出 finding；`watcher.py` 不再 import `delivery`，`delivery.py` 不再 import `watcher`；新增独立 `devflow.watchdog_main` 进程与 compose 服务；Watchdog 提供有界状态查询和运行时维护指令。
- **Phase 2**：新增 `RouterState` 项目级记忆，记录决策、follow-up、Worker 报告、Attention、handback、结束与重开；候选 payload 注入 Router 记忆；owner API 建任务与手工 route 收敛到 Router；手工 route 幂等。
- **Phase 3**：Pi turn 结束无终态报告时进入 waiting 而非 Attention；新增 `follow_thread_for_context` / `mark_thread_unrelated` runner 接口和工具；调度器优先处理带未读人类归属的工单，并允许一个额外 slot。
- **Phase 4**：GitHub Source 只产出 Trigger/事件；CI、PR head/close/reopen、Attention answer 的领域反应移到 `changes.py`；API 的建任务与 route 只保留薄适配。
- **Phase 5**：`Project.paused` 项目级暂停；暂停停止派发、新 Eval、Input 投递与回复；新增 `/projects/{id}/health` 健康摘要；前端加入暂停/恢复、健康摘要、Watchdog 查询与维护指令入口。
- **验证**：后端全量回归、Pi 编译与协议测试、前端 `npm run check` / `npm test`、Alembic 从空库升级到 head。

仍未完成：Router 跨 trigger 单回合批处理、完整持久 LLM Router Session、Watchdog 的完整工具集 Agent Session、Worker 后台 job/subagent 宽限、Outbox 严格顺序、可插拔 Source/Channel、Prompt/Skills 版本化。
