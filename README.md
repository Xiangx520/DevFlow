# DevFlow Recode

面向个人 GitHub 项目的长期开发助手。用户交付目标，系统持续管理执行 Sessions、跟进 PR/CI、检查方向，并在需要决策或结果可审阅时通知用户。

本目录包含重构后端与独立开发工作台，旧 DevFlow-AI 保持不变。

## 架构

- Python/FastAPI：API、Router（路由与项目记忆）、Worker、独立 Watchdog、调度、GitHub 与通知。
- PostgreSQL：Trigger/Thread/Ticket/Mandate、Router 记忆、Decision、Session/Attempt、验证证据与 Effect。
- 本地 Pi fork：编码 Session、宿主控制的模型/工具边界、持久会话、结构化报告。
- Docker：每任务独立持久 checkout 和容器；只有 Worker 访问 Docker daemon。

Router 不执行编码，持有项目级记忆并统一人类入口；Worker 每个 Ticket 一个持久 Session；Watchdog 在独立进程中做存活与偏离监督，可被查询、可记录运行时维护指令，但不参与正常交付放行。Delivery 负责确定性宿主验证、最终复核与发布。

## 启动

需要 Docker Desktop Linux 引擎。本地后端需要 Python 3.12，Pi 需要 Node >=22.19，容器使用 Node 24；默认任务镜像提供 Debian Python 3.11，可按目标项目修改镜像。

1. 复制 .env.example 为 .env，填写独立随机 API/Webhook/数据库密码及 GitHub PAT、模型配置。不要复用示例字符串；数据库密码使用 URL 安全字符。
2. 构建编码镜像：docker compose --profile build build runner-image
3. 启动：docker compose up -d --build postgres migrate api worker watchdog frontend（飞书接入时再加 feishu）
4. 前端工作台：<http://localhost:3000>，使用 DEVFLOW_API_TOKEN 登录。详见 [前端运行与验收](frontend/README.md)。API 文档：<http://localhost:8000/docs>。公共 API 使用 Authorization: Bearer <DEVFLOW_API_TOKEN>。
5. GitHub Webhook 配置同一 secret，地址为可公开访问的 /api/v1/webhooks/github。订阅 Issues、Issue comments、PR、Reviews、Review comments、Workflow runs、Check runs/suites。localhost 需要自行配置隧道。

通过 POST /api/v1/projects 配置仓库、允许发起人、Agent 登录名、安装和验证命令，再添加 Issue 标签 agent:run 或在对话中派发目标。非允许发起人的评论只能提供上下文，不能授予新权限。系统创建/更新 PR，由人合并；未配置 CI 不会标记为全部通过。

## 本地测试

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
.\.venv\Scripts\python.exe -m pytest backend/tests -q

docker compose -p devflow-tests -f compose.test.yaml up -d --wait
$env:DEVFLOW_TEST_DATABASE_URL = 'postgresql+psycopg://devflow:devflow_test@127.0.0.1:55439/devflow_test'
.\.venv\Scripts\python.exe -m pytest backend/tests -q
~~~

默认测试使用隔离 SQLite；配置测试数据库后每个用例创建独立 PostgreSQL schema。真实容器测试额外设置 DEVFLOW_RUN_DOCKER_TESTS=1，先构建 runner 镜像。模型和 GitHub 默认为可控制替身，不消耗模型费用。

本机 Node 18 优先命中 PATH 时，scripts/check.ps1 会寻找 fnm 的 Node 22+，只调整本进程 PATH，不修改全局配置。

镜像级验收：

~~~powershell
docker build -t devflow-backend:local -f backend/Dockerfile .
docker build -t devflow-runner:local -f pi-service/Dockerfile .
.\.venv\Scripts\python.exe scripts/deployment_smoke.py
~~~

该脚本建立独立网络和临时数据库，结束后自动清理，不调用真实外部服务。


## 文档

- [目标架构：三层 Super-agent](docs/TARGET_ARCHITECTURE.md)
- [架构与状态语义](docs/ARCHITECTURE.md)
- [Super-agent 架构迁移](docs/SUPERAGENT.md)
- [API 与前端接入](docs/API.md)
- [运维和恢复](docs/OPERATIONS.md)
- [验收记录](docs/VALIDATION.md)
- [Pi fork 来源](vendor/PI-UPSTREAM.json)

不包含旧数据迁移、向量库、团队工作区、周报、自动合并、部署或跨仓库协调。多任务在同仓库默认保守串行，不宣称能自动发现所有代码冲突。
