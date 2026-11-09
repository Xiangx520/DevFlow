# 运维、权限和恢复

## 凭据与网络

API 默认只绑定本机。通过隧道提供 Webhook 时启用 TLS，只开放需要的路由。API token、GitHub PAT、模型密钥、Webhook secret 和数据库密码分别配置。细粒度 PAT 只授权选定仓库及必要 Contents、Issues、PR、Actions/Checks 权限，不需要自动合并或管理权限。

Worker 与独立 Watchdog 进程有 Docker socket，属于高权限可信控制面。Runner 非 root、无 capabilities、禁止权限提升、限制内存/PID，只挂载本任务 volume。它仍可联网安装依赖，不应将普通容器描述为抵御内核漏洞的强隔离。

GitHub 密钥仅在准备和发布 helper 中短时存在；编码/验证 helper 不含长期密钥。人工交接或停止后删除执行容器，保留任务 volume。不要挂载宿主 .env 或用户目录。

模型走任务限定代理，按模型和预算限制。上游缺少 usage 时保守保留预留预算。编码、Reviewer 和 Watchdog 调用共享任务 token 预算。Router 的跨任务判断与 Watchdog 的项目查询通过有界状态、超时和重试上限控制，尚无统一项目费用结算。

## 恢复

重启后过期任务重新排队，保留 checkout 和 Pi 会话。替换 Session 使用原 checkout 加交接。累计预算和 CI 修复次数不重置。

任务暂停/取消使 capability 失效；项目级 `paused` 停止新派发、新 Eval、Input 投递与回复，但保留外部事件记录。Watchdog 在独立进程中继续存活检查和纠偏。已开始的外部写操作可能已完成，不确定时先查 GitHub。不要直接删掉 Effect 再重试，否则丢失查重依据。

预算耗尽需调整项目 limits 并回答 Attention。Effect 连续失败在任务内可见；审阅后用显式效果重试接口保留原 key 重排。安装失败、模型错误、验证失败、CI 缺失各有不同原因。

备份 PostgreSQL 和 devflow-task-* volumes，两者共同恢复。volume 内有私有代码和会话，应采取相同权限控制。初始迁移使用 Alembic，不在启动时修改旧项目数据库。

/health/live 检查进程；/health/ready 检查数据库，不代表模型或 GitHub 已配置。`GET /api/v1/projects/{id}/health` 汇总 pause、运行中工单、待评估 Trigger、失败 Effect、未决 Attention 与监督发现；`GET /api/v1/projects/{id}/watchdog` 提供 Watchdog 的有界状态，`POST /watchdog/ask` 可询问原因。查看任务时间线、Attempt.reason、Finding、Decision 和 Effect.result 排查。

## 真实服务验证

scripts/live_smoke.py 默认只读检查 API 健康。带 --allow-github-write、项目及目标时才派发真实任务，可能产生模型费用并创建/更新 PR；只在专用测试仓库使用。飞书在人工问题、交付或阻塞时验证。

自动化测试中的模型/GitHub 替身不能证明真实账户、模型或通知渠道配置正确。

## DeepSeek 与细粒度 PAT 配置

DeepSeek 默认思考模式可能拒绝 Router/Reviewer 的强制工具选择；可设置 DEVFLOW_LLM_THINKING=disabled，其他提供商默认不发送 thinking 参数。

CI 查询使用 Actions 与 Commit statuses，细粒度 PAT 至少需要 Actions Read、Commit statuses Read；Contents/Issues/Pull requests 使用 Read and write。Checks 不属于细粒度 PAT 可选权限。仅发布到第三方 Checks 的检查仍需 GitHub App 适配，不能据此声称全量 Checks 均已通过。

本机配置记录见 LOCAL_SETUP.md。
