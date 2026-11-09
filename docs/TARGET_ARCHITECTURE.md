# 目标架构：三层 Super-agent

本文是 DevFlow Recode 的目标架构与边界规则。它把系统分成三层 Agent 角色、一个确定性事件底座，以及一个产品附加的交付层。三层是逻辑分层，不要求每层都是单个 LLM Session；可以是独立服务、持久会话，或确定性处理器。

## 分层

```
Sources ──Trigger──▶ Engine（事件底座）
                         │
                         ▼
                  Router（路由层）
                         │  Ticket / Mandate / Input
                         ▼
                  Worker（执行层）
                         │  Report / Handback
                         ▼
                  Delivery（交付层，产品附加）
                         ▲
                  Watchdog（监督层，独立失败域）
```

### Engine（事件底座，确定性）

- 负责 Source → Trigger → Thread → Subscription 的接收、去重、作用域过滤、fan-out 与 Eval 排队。
- 负责 Policy 版本、Gate、Mandate、Ticket 状态、Input、Ownership、Effect 队列。
- 不调用模型；不决定“是否接单”。它只执行确定性规则和已记录的事实。

### Router（路由层）

- 负责显式请求的分发（accept / attach / stop / ignore）与推断候选的研判。
- 持有项目级记忆：活跃 Ticket、最近 Decision、人类后续消息、Worker 报告、待决问题。
- 处理所有人类入口，并把人的消息、Worker 报告、系统通知汇入下一次判断。
- 不执行具体开发任务；可以没有 shell/编辑工具。
- 可以由“持久会话 + 批量模型调用”实现，也可以是“持久状态服务 + 分步模型调用”；关键是状态持久、入口统一、决策可审计。

### Worker（执行层）

- 每个 Ticket 一个持久 Session、目录与容器；上下文隔离。
- 接收 Router/Engine 交付的 Input；执行、报告、handback、关注相关 Thread。
- 行为遵循 Mandate；不能自行扩大授权。
- 可以 idle 并被订阅消息唤醒；Worker 之间不直接通信。

### Watchdog（监督层）

- 独立进程、独立失败域、独立 Session/工具，由定时消息驱动。
- 负责存活与恢复、模型判断进展/偏离、纠偏、重启、升级人工。
- 可以被人用自然语言查询，也可以在运行时调整 prompt/trigger 规则（有审计）。
- 不在正常交付的主路径上；Watchdog 不可用不应阻断正常交付。

### Delivery（交付层，产品附加）

- 确定性验证、发布与人工合并门禁。
- 依赖 Worker 的证据、宿主验证和 CI；不依赖 Watchdog 的语义复核。
- 语义复核若作为产品要求，应是独立的 Reviewer 能力，属于 Delivery，不属于 Watchdog。

## 依赖规则

1. Source 适配器只能产出 Trigger/Channel 事件；不得直接修改 Ticket 状态或决定纠偏。
2. API 是薄适配器；人发起的显式工作必须进入 Router 的显式入口，不得直接写领域状态。
3. Router 不执行开发；Worker 不决定准入；Watchdog 不决定交付。
4. Delivery 不 import Watchdog；Watchdog 不 import Delivery。
5. Engine 不调用模型；模型判断只出现在 Router、Worker、Watchdog、Reviewer。
6. 每类状态只有一个写者；跨层通过命令与事实交互，不共享可变对象。
7. 所有外部副作用走 Effect；先入库再执行。

## 运行与失败隔离

- Router、Worker、Watchdog 可以是不同进程/服务，通过数据库队列协作。
- Watchdog 停止时，正常任务仍应完成；Router 停止时，Watchdog 仍能发现并报告异常。
- 暂停（pause）属于项目级运行控制：停止新 dispatch、新 Eval、Input 投递与回复，但继续记录外部事件。
