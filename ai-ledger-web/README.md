# VibeLedger Web

React/TypeScript 家庭账本前端，继续使用现有 FastAPI `/api/v1/*`。已通过用户托管验收，当前运行于隔离 S34 staging；原 Streamlit Dashboard 保留。

完整设计见 [前端重设计方案](../docs/architecture/FRONTEND_REDESIGN.md)，已完成范围、验证与剩余问题见 [本次进度](../docs/architecture/FRONTEND_PROGRESS.md)。

## 本地运行

需要 Node.js 22.12+（本次使用 24.19.0）。

```powershell
cd ai-ledger-web
npm.cmd ci
$env:BACKEND_URL = 'http://127.0.0.1:8000'
$env:SUPABASE_URL = 'https://YOUR_PROJECT.supabase.co'
$env:SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_YOUR_PUBLIC_KEY'
$env:DASHBOARD_TIMEZONE = 'Asia/Singapore'
npm.cmd run build
npm.cmd start
```

打开 `http://127.0.0.1:3000`。开发时使用 `npm.cmd run dev`。配置只用发布密钥，不使用服务角色密钥。后端必须继续验证同一 Supabase 项目的 JWT 和家庭成员权限。

`/config` 只返回登录服务 URL、发布密钥和时区。Node 服务将固定 `/api/v1/*` 路径转发到 `BACKEND_URL`，没有数据库权限。默认只监听 127.0.0.1；容器内设置 `HOST=0.0.0.0` 和 `PORT`。

登录令牌由 Supabase SDK 存在本地浏览器存储中，刷新/重新打开页面可恢复。财务操作恢复资料使用按用户分隔的 **sessionStorage**，支持同一标签页刷新；关闭标签页可能丢失尚未完成的命令恢复资料。PDF、密码和截图内容不写入浏览器持久存储。显式退出会清除登录与本地操作资料。服务器仍是权限和财务规则的唯一裁决者。

## 验证

```powershell
npm.cmd run build
npm.cmd test
npm.cmd run test:e2e
```

Windows 浏览器测试使用已安装的 Chrome。CI 使用 `npx playwright install --with-deps chromium` 安装 Chromium。浏览器测试提供虚构家庭数据与 Supabase HTTP 响应，运行真实浏览器、真实 SDK 和生产构建；它们不访问实际家庭或模型服务，也不替代托管验收。

浏览器截图保存在 `test-results`（Git 忽略）。无登录配置时显示配置说明，正式前端没有演示模式或绕过鉴权开关。

```powershell
docker build -t vibeledger-web .
docker run --rm -p 8080:8080 --env-file YOUR_PRIVATE_ENV_FILE vibeledger-web
```

保留既有后端与 Dashboard 服务，先在独立候选服务验证，再由操作方决定入口切换。生产发布不是本次已执行的工作。

## 浏览器连接真实临时数据库

先启动独立 PostgreSQL，严禁使用真实家庭库。以下只连接本机测试库：

```powershell
$env:ENVIRONMENT = 'test'
$env:DATABASE_URL = 'postgresql://vibeledger_test:vibeledger_test_local@127.0.0.1:55439/vibeledger_test'
$env:DB_SCHEMA = 'vibeledger_test_runner'
npm.cmd run build
npm.cmd run test:live
```

依赖后端 requirements.txt；Windows 默认使用仓库的 venv_backend，其他环境使用 python，可由 VIBELEDGER_PYTHON 覆盖。测试脚本只允许本地测试数据库，建立随机隔离 schema，运行实际 API/JWT/SQL；Supabase HTTP 和 PDF 提取器使用固定虚构数据，不调用真实模型。退出时清理 schema；强制终止后由临时数据库容器整体清理。生成的 live-fixture.local.json 仅含测试签名和虚构用户，不进入 Git 或镜像。
