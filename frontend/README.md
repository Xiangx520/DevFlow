# DevFlow 开发工作台

基于现有 `/api/v1` 的独立前端，源文件在 `frontend/src`，使用浏览器原生 ES Modules，无 npm 运行依赖或外部 CDN。UI 为中文，支持桌面与手机。Starlette/Uvicorn 提供静态页面及同源 API/SSE 代理。

## 使用

在项目根目录运行：

```powershell
docker compose up -d --build api frontend
```

打开 <http://127.0.0.1:3000>，输入根目录 `.env` 中的 `DEVFLOW_API_TOKEN`。前端容器不读取 `.env` 或平台密钥，仅转发浏览器提供的 Bearer Token。默认 Token 仅在内存，刷新需重新登录；勾选“当前标签页中记住”后使用 sessionStorage，退出时移除。

本机直接开发（后端已监听 8000）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r frontend/requirements.lock
.\.venv\Scripts\python.exe frontend/server.py
```

修改源文件后刷新浏览器。可通过 `DEVFLOW_BACKEND_URL` 更改后端地址，通过 `DEVFLOW_FRONTEND_PORT` 修改直接启动端口。浏览器只访问当前站点，不需要 CORS，也不会把 Token 放到 URL。Compose 默认仅监听本机。

## 页面与操作

- 项目总览：真实任务数量、Router 摘要、健康摘要、暂停/恢复入口、最近事件、仓库现场。
- 路由会话：会话切换、历史加载、项目或指定任务对话、分析/实现范围；Router/Engine 的异步回复通过事件刷新。
- 开发任务：搜索、状态筛选、新建、暂停/恢复/取消/重试、优先级、前置依赖、补充要求、替换 Session。
- 任务详情：目标、验收条件、人工问题、代码与验证证据、Sessions/Attempts、Watchdog findings、外部操作及失败重投。
- 待处理：未决 Attention 的建议、影响及答复，待审阅结果与 GitHub PR 链接。合并通过 GitHub 完成。
- 活动记录：分页追赶与 SSE 断线续传、事件去重、路由决策、交付复核与监督结果。
- 项目设置：接入仓库、接单身份、执行命令与预算、项目暂停/恢复、健康摘要、Watchdog 状态查询与运行时维护指令、重要事项飞书通知开关。

完整后台闭环需要 `api`、`worker` 与独立 `watchdog` 服务；只查看页面和只读状态可先启动 `api` 与 `frontend`。

新建任务默认仅分析，用户可明确选择实现权限。页面不显示虚构进展，不将历史提交验证视为当前提交通过。引用内容按纯文本渲染，外部交付链接仅接受 HTTP/HTTPS。

边界：后端当前单次任务查询上限 200，页面筛选在返回的任务内执行；时间线最多保留最近 400 条，服务端记录不受影响。没有文档上传、在线配置飞书群绑定或集成连通性检测的后端接口，因此不提供对应表单。飞书平台 21001 限制不会因前端上线而解决。

## 验证

```powershell
node --test frontend/tests/api.test.js
.\.venv\Scripts\python.exe -m pytest backend/tests frontend/tests/test_server.py -q
```

UI 手工验收可启动一次性测试数据库（不启动 Worker，不访问模型、GitHub、飞书）：

```powershell
.\.venv\Scripts\python.exe scripts/frontend_fixture.py
# 另一个终端
$env:DEVFLOW_BACKEND_URL = 'http://127.0.0.1:8101'
$env:DEVFLOW_FRONTEND_PORT = '3001'
.\.venv\Scripts\python.exe frontend/server.py
```

访问 <http://127.0.0.1:3001>，Token 为 `ui-test-token`。测试数据库位于系统临时目录，正常退出时清理。测试页面仅供隔离验收，不含模拟的后台执行者，不应作为真实工作台入口。
