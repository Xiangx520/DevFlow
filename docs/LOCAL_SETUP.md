# 本地配置记录（2026-11-07）

本地后端已启动，API 文档：http://localhost:8000/docs 。公共接口的 Authorization 请求头填写 Bearer 加本地 DEVFLOW_API_TOKEN。凭据只保存在根目录 .env，不包含在本文或项目配置样例中。

## 已接入仓库

- 仓库：Xiangx520/DevFlow-test
- 默认分支：main
- 项目 ID：a448d49f-5867-4ca1-b27e-e94fb867937c
- 授权发起人：Xiangx520
- agent_login 留空，避免把同一账号发出的人工事件当作机器人自身事件忽略。当前使用 agent:run 标签或 API 显式派发。
- 已创建 agent:run 标签。
- 项目配置保存在 PostgreSQL；不含凭据的可复用样例见 ../config/devflow-test.json。
- 远程仓库已获用户授权初始化，提交 77c82829962a2333871c47d7213e9fa9091ed95e 包含最小 Python 示例、3 项 pytest、依赖和说明。未创建 PR 或开发任务。

## 模型

使用用户选定的 deepseek-flash。实际调用发现其默认 thinking 模式不支持强制指定管理工具，因此新增 DEVFLOW_LLM_THINKING=disabled，同时作用于 Router/Reviewer/Watchdog 和编码代理。使用其他兼容提供商时可删除此配置，沿用提供商默认模式。

模型列表和真实结构化工具调用均成功。配置验证产生了少量真实模型调用用量；未派发后台 AI 编码任务。

## 验证与通知

测试仓库在真实 Linux runner 容器中完成克隆、隔离 venv 安装和 3 项 pytest。测试现场已清理。

已获得明确授权发送一次飞书连通性消息，飞书 API 返回成功。项目 notify_feishu=true；当前自定义机器人是单向通知。

本地 API 鉴权、正确/错误 Webhook 签名和重启后的配置持久化已验证。没有通过公网验证 GitHub 的真实投递。

CI 查询改为细粒度 PAT 支持的 Actions API 和 Commit statuses API，按当前 SHA 聚合每个 workflow/event 的最新执行。缺少 CI 返回 missing，不标记为通过。本测试仓库已配置 GitHub Actions：.github/workflows/tests.yml，在 push、pull_request 或手动触发时以 Python 3.11 运行 pytest。首次 push 运行成功：https://github.com/Xiangx520/DevFlow-test/actions/runs/42277319842 。第三方仅通过 Checks API 发布的检查不在此 PAT 查询覆盖范围；这类集成需要 GitHub App 适配后才能完整校验。

## 保留到部署时的配置

域名 devflow.kissxiangx.fun 尚未在本次工作中部署或配置云服务器/隧道。需要打通 HTTPS 反向代理到本地后端（或将后端迁至服务器），然后在 GitHub Recent Deliveries 中重试 ping。

DEVFLOW_PUBLIC_URL 暂时保留 http://localhost:8000，以便本机打开通知链接；公网服务可用后再改为 https://devflow.kissxiangx.fun，并重建 API/Worker 容器。该变量不会自动设置域名、TLS 或隧道。

重载配置：docker compose up -d --build api worker 。
