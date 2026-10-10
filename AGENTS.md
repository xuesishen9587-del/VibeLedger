# 开发指令

先读 [README.md](README.md)。以当前代码为实现依据，以 [CONTRACTS.md](docs/architecture/CONTRACTS.md) 为业务约束；冲突时查代码、测试与用户最新指令，不将旧目标自动当成已实现功能。

## 项目导航

- `ai-ledger-web/src/pages/`：总览、支出、财富、待处理、账单、设置；`src/lib/api.ts` 处理按身份隔离的命令恢复，`server.mjs` 为同源代理。
- `ai-ledger-backend/app/api/routes/`：API；`auth/`：身份；`services/`：识别、命令、报表与计划；`domain/`：验证；`repositories/`：SQL。
- `ai-ledger-backend/migrations/simplified/`：唯一可执行 lineage；根迁移目录旧 SQL 为历史证据。
- 各应用的 `tests/`：测试；后端分 unit、integration、migration、concurrency。
- `ai-ledger-dashboard/`：备用 REST 界面，仍需维护兼容性；不要恢复数据库直连。
- `docs/api/openapi.json`：生成文件；`docs/deployment/sql/`：一次性 operator SQL；根 `scripts/`：容器检查。

少量历史代码注释仍提到已合并的 TARGET_DOMAIN_MODEL 或 CONTRACTS 旧章节号，业务含义查当前 CONTRACTS；这些注释没有运行时依赖。本次文档整理不改业务文件。

## 不可破坏的约束

- 支出和余额观察独立，投资收益是区间计算。禁止补造 opening balance、转账腿、余额校准、自动收益收入或现金流完整性。
- Decimal／NUMERIC／十进制字符串；保留原币。日期边界用家庭时区，缺余额／汇率不能补零或按 1:1 换算。
- 查询和引用限定家庭，跨家庭 ID 不泄漏存在性。账户 owner 不决定访问；登录只能解析预先配置的成员，不能接受客户端指定家庭。
- 财务写入、证据、审计、终态回执原子提交；版本冲突重新读取。先锁回执（多个按 ID 排序），再锁家庭，随后稳定顺序锁记录；外部请求在数据库锁之外。
- 相同键重放相同命令；结果未知保留原键／参数并恢复，不能自动换键。404 后需 cancel tombstone 排除迟到请求。`failed` 为持久终态，不会因 retryable 标识自动重跑 AI。
- 截图不持久化，PDF／密码只临时处理；不记录 bearer、DSN、图片/base64、原始模型输出、私有金融提示或 SDK 异常正文。审计保存必要变更，设备库只存令牌 hash。
- 清晰消费可自动保存；金额／币种／日期／性质疑点阻止自动入账，未知账户／分类仅补资料，不能猜账户。余额范围／重复／整单确认约束不得为降低操作次数而绕过。
- 已应用迁移不可改写；启动不执行 DDL。测试／迁移保护不能为生产操作放宽。私有 schema 不授予 Supabase Data API 客户端。
- 生产 Shortcut 是仓库外的 `../shortcut/VibeLedger_Production_unsigned.shortcut`，用户已确认两人共用这一份逻辑，各自本地 token 区分身份。按 [实际核对](docs/architecture/SHORTCUT_COMPATIBILITY_BOUNDARY.md) 保持协议兼容，不能凭 API 推定手机交互；导出、token 与解包内容不得入 Git。

## 测试

Python 3.13、Node 24 为 CI 基准；先安装固定依赖。运行与改动相关的检查；文档改动通常核对事实、链接和 `git diff --check` 即可。

在 `ai-ledger-backend/`：

```sh
python scripts/export_openapi.py --check
python -m unittest discover -s tests/unit -p "test_*.py"
```

数据库检查必须显式设 `ENVIRONMENT=test`、**本地可销毁**的 `DATABASE_URL` 和 `DB_SCHEMA=vibeledger_test_<identifier>`，核对目标后执行：

```sh
python scripts/run_integration_tests.py
python -m unittest discover -s tests/migration -p "test_*.py"
python -m unittest discover -s tests/concurrency -p "test_*.py"
```

集成 runner 为串行测试复用 schema／连接，默认 pool 16；`--pool-size 0 --isolated-schema-per-class` 可诊断隔离差异。共享 schema 不用于并行运行，不能替代独立迁移／并发测试。不要指向托管 Supabase 或真实家庭库。

在 `ai-ledger-web/`：

```sh
npm ci
npm run build
npm test
npm run test:e2e
```

e2e 非 CI 默认 Chrome；`CI=1` 使用 Playwright Chromium，需 `npx playwright install chromium`。`npm run test:live` 使用本地真实 API／PostgreSQL，先满足上述隔离条件；Auth／Gemini 替身不等于线上验收。Windows 的 live 配置默认寻找根目录 `venv_backend/Scripts/python.exe`；环境不同用 `VIBELEDGER_PYTHON` 指定已安装后端依赖的 Python。

在 `ai-ledger-dashboard/`：`python -m unittest discover -s tests -p "test_*.py"`。

Docker 可用时，从根目录运行 `python scripts/container_smoke.py`；operator SQL 隔离验证为 `python ai-ledger-backend/scripts/smoke_s6_operator_sql.py`，仅使用可销毁容器。真实 Gemini 检查见 [说明](docs/testing/EXPENSE_GEMINI_LIVE_SMOKE.md)，会向外部模型发送图片，须显式选择。

API 改动核对旧客户端并重新生成 OpenAPI；排序／版本漂移也需查清，不能凭检查失败推断协议不兼容。测试聚焦授权、事务、重复和恢复，不为低影响改动机械加测。

2026-10-09 审阅发现：[Streamlit 计划转换测试](ai-ledger-dashboard/tests/test_spending_schedule_conversion.py) 固定期望 2026-09-01，而 [表单](ai-ledger-dashboard/spending_page.py) 取当前月份，已出现日期相关失败；本机 Python 3.14 下 OpenAPI 检查仅 ReviseRequestPayload.original_currency 的 enum 顺序不同，固定 Pydantic 版本后仍可复现，未验证 CI Python 3.13 的结果。两项均未在文档任务中改代码，后续修复后移除此说明。[package-lock](ai-ledger-web/package-lock.json) 还锁定开发依赖 source-map-js 1.2.1，属于 [已知拒绝服务问题](https://github.com/advisories/GHSA-68fv-2mgg-jv7q) 的受影响范围；[最终 Docker 镜像](ai-ledger-web/Dockerfile) 不复制 node_modules，未证明线上服务可达该问题，本轮不升级依赖。

## 工作与文档维护

- 开始检查本地修改与远程，保留用户工作。线上状态须实际核实，不能引用某次 staging、S5/S6 或旧 SHA 作为现状。
- 按用户当前任务范围工作；审阅／文档任务不自动扩展成业务修复、功能规划或生产操作。
- 中文 UI 简洁具体；技术键／版本留在程序内部。展示日期、覆盖、原币、估算和确认差异，已知小计不能冒充完整结果。
- README 和 AGENTS 为主要入口。其他文档只保留独立规则／操作价值；字段／路由／表优先链接代码或生成契约。完成的阶段计划、进度和重复交接留在 Git。
- 实现变更同步相关解释；区分已实现、限制、用户报告和线上实测。不要把本机测试数量或旧云资源写成长期事实。
- 历史用户决策已免除额外两人／20 张截图验收；备份、PITR、灾难恢复、备份保留期和恢复演练不属于项目要求，不能重新设为阻塞。正常回执恢复、保留现有数据与应用回滚边界仍必须遵守。
- 部署、生产写入、凭据撤销和旧环境停用依据用户明确授权；先形成可审查结果。操作说明本身不构成执行授权。
