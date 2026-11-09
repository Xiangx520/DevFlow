# 验收记录

> 历史记录。文中“管理 Agent / 管理回合 / Watcher”是当时的术语，当前对应 Router / Router 回合 / 独立 Watchdog；以 [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) 和当前代码为准。

日期：2026-11-07（Asia/Shanghai）。

## 已执行

| 检查 | 结果 | 使用的真实组件 |
|---|---|---|
| 后端回归 | 31 passed | Python 3.12.6、PostgreSQL 16、Docker Desktop Linux |
| Pi 协议与宿主边界 | 5 passed | 本机 fnm Node 22.23.3 |
| Pi bridge 编译 | passed | TypeScript 与本地 fork 的真实 SDK 类型 |
| Pi 上游完整 npm run check | passed | 2514 个源码文件及上游依赖/入口/类型检查 |
| Pi fork Linux 镜像 | built | Node 24、固定模型目录快照、非 root Runner |
| Python 后端 Linux 镜像 | built | Python 3.12、锁定依赖、Alembic、Docker CLI |
| 隔离部署 smoke | passed | 临时 PostgreSQL、实际 HTTP API、Worker、Runner |
| Compose 配置解析 | passed | Docker Compose |

后端测试有一个来自 Starlette/httpx TestClient 的弃用警告；未隐藏，不影响运行结果。

## 持续开发验证

真实 Pi 在任务容器中调用本地流式模型替身，修改 Git checkout 内的代码。宿主在独立 helper 容器运行验证，生成实际提交 SHA、命令结果和代码 diff。

真实容器测试进一步覆盖：

- 大于原日志截断阈值的代码 diff，正确解析验证报告。
- 同一 Pi Session 的持久恢复。
- 工具仍在执行时中断 Worker、过期租约回收和新执行者接管。
- 未提交代码在接管后仍存在。
- 恢复后的模型请求通过严格 tool-call/tool-result 配对检查。
- 运行长命令时取消，容器被终止，旧 capability 失效。
- 实际模型 usage 结算，预留额度正确退还。

GitHub 写入使用可控制适配器替身，验证创建 PR 查重、CI 修复、人工决策、待审阅与合并收尾。未把 GitHub 替身当作真实账户联调。

## 持续管理验证

脚本化管理模型驱动两项有依赖的任务，改变优先级并暂停下游。独立 Watcher 对偏离目标的报告生成带证据 finding，管理 Agent 替换 Session 并下达纠偏指令；目标、输入、代码目录和预算保留。

另覆盖：依赖循环、并发唯一租约、观察模式、自触发过滤、旧 CI、跨 CI 类型重复事件、当前 CI 不能替代本地验证、复核门禁、模型检查失败限频、人工问题去重、通知去重、用户后续输入拆分新目标，以及发布网络等待期间取消保持响应。

## 部署 smoke 验证

scripts/deployment_smoke.py 使用独立 Docker 网络和临时数据库，验证：

- Alembic 初始化。
- API 健康、Bearer 认证。
- 项目/任务持久化、幂等请求、事件和 OpenAPI。
- Worker 访问 Docker daemon、监督循环和无任务管理回合。
- Runner 非 root 且无 Docker socket。

脚本在结束时自动删除自己的容器、匿名 volumes 和网络。

## 尚未进行的外部验证

没有使用用户的真实 GitHub 仓库、付费模型密钥或飞书渠道；没有自动合并、部署或发送真实外部消息。首次真实接入需配置 .env 和专用测试项目，可使用 scripts/live_smoke.py 显式派发任务。

测试证明工程链路与约束的行为，不证明任意模型在任意仓库都能正确完成开发。语义判断质量应在配置实际模型后继续验证。

## 环境说明

发现 fnm 已安装 Node 22.23.3；原会话 PATH 优先命中 E:/Xiangx/NodeJS 的 18.20.4。构建显式使用 Node 22，没有修改全局 PATH。scripts/check.ps1 会优先使用 fnm 的高版本。

Docker Desktop 已启动用于验收；本项目测试资源在验证结束后清理，构建镜像与缓存保留。


## 2026-11-07 前端工作台

- 浏览器原生 ES Modules + 同源 Starlette 代理，Compose 独立 frontend 服务，本机 3000 端口。
- Node 客户端测试 7 passed；pytest 后端与前端代理测试 48 passed、2 skipped（既有容器集成可选场景）。保留既有 Starlette/httpx 弃用提示。
- 真实后端只读验证：Owner Token 查询、项目会话和人工事项列表、SSE 心跳、未认证 401、配置文件 404。真实开发任务保持 0。
- 隔离临时数据库、无 Worker 的浏览器验证：创建分析任务、暂停/恢复、补充需求、回答 Attention、管理消息持久化、当前提交验证证据。未调用真实模型或外部消息渠道；后台开发闭环不在此次前端验收中重新执行。
- 浏览器检查桌面及 390px 手机布局，无页面级横向溢出；真实工作台浏览器未记录 JS error。
- 飞书平台 21001 限制仍独立存在，页面不会显示为已修复或集成成功。
