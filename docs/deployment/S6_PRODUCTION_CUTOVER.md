# 持续运维与环境边界

文件保留旧名称，供 `sql/s6_fresh_schema.sql` 的现有注释引用；旧 S6 阶段计划／云资源快照已移入 Git 历史。用户确认项目已上线使用，以下是代码可确认的操作边界，**不是当前线上配置清单，也不授权执行生产操作**。

## 配置与身份

后端使用私有环境／secret 配置 DATABASE_URL、DB_SCHEMA、ENVIRONMENT、GEMINI_API_KEY；默认 Gemini 模型为 gemini-3.5-flash-lite，可显式覆盖。检查实际平台的资源和密钥绑定时只记录版本引用，不打印 payload／DSN。

生产浏览器认证必须固定 Supabase issuer／audience／HTTPS JWKS 与实际非对称算法，例如：

```dotenv
ENVIRONMENT=production
AUTH_ISSUER=https://<project-ref>.supabase.co/auth/v1
AUTH_AUDIENCE=authenticated
AUTH_ALGORITHMS=["ES256"]
AUTH_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
```

只在项目确实使用 RS256 时改相应算法。不能套用后端 .env.example 的 staging HMAC；production 拒绝静态／HMAC 替代。JWKS 使用固定源、有界请求和缓存，不采信 token 提供的 URL，不在验证失败时降级。

operator 应在 Supabase 关闭公开注册，预先建立用户、以已验证 Auth UUID 映射 users.auth_subject、建立成员关系；当前托管配置未由本轮核实。密码恢复由 operator 协助；代码没有公开注册／自建家庭／发重置邮件接口。变更签名配置须考虑同项目现有消费者。

网页配置 BACKEND_URL、SUPABASE_URL、SUPABASE_PUBLISHABLE_KEY（仅 publishable）、DASHBOARD_TIMEZONE；只公开 Auth URL、publishable key、显示时区。Node 代理固定后端 origin，不转发 Cookie、不任意代理 URL。财务 DB 和 Gemini secret 不属于前端。显示时区应与实际家庭一致。

React 使用 SDK 的浏览器持久登录，Streamlit 使用服务端会话；本地 logout 不等于已经签发 JWT 即刻失效。用户禁用、设备撤销由每次 API 请求重新检查。

## PostgreSQL 与初始化

| 角色 | 约束 |
| --- | --- |
| operator / schema owner | 经授权执行 DDL、初始化、身份映射；不能用作普通请求身份 |
| runtime | 仅所需读取／插入／更新；无 schema CREATE、DDL、TRUNCATE、所有权或广泛角色继承 |
| audit_events | runtime 只读／追加，数据库 trigger 拒绝 UPDATE／DELETE |
| schema_migrations | runtime 只读，operator 维护 |
| PUBLIC / anon / authenticated | 不得通过 Supabase Data API 或继承权限访问私有财务 schema |

现存 fresh SQL 不给 runtime DELETE，旧角色文档中的通用 DELETE 授权不适合作为当前配置依据。有效权限还需检查 PUBLIC、角色继承、其他 schema 和 Supabase exposed schemas；SQL 文件不能证明线上权限已生效。

当前只有 simplified lineage。已应用 SQL checksum 不可改；旧 0001–0009 SQL 不执行也不混入当前 schema。所需扩展为 pgcrypto、pg_trgm、citext，位于明确的扩展命名空间。FastAPI 启动不执行迁移；测试／runner 拒绝 production，不能改 ENVIRONMENT 绕过。

一次性 [fresh schema](sql/s6_fresh_schema.sql) 与 [identity bootstrap](sql/s6_bootstrap.sql) 保留，用 psql operator 执行：

- **G0：** 先取得这次新建环境的明确授权，核实数据库、operator、目标不存在、扩展、baseline 字节 checksum、私有身份输入及凭据范围。
- **G1：** fresh SQL 需要授权值 CREATE_NEW_VIBELEDGER_PROD_V1、expected_database、expected_operator；bootstrap 使用 BOOTSTRAP_NEW_VIBELEDGER_PROD_V1。不要放入启动脚本或普通 CI。
- SQL 的 schema／角色名固定为 vibeledger_prod_v1 等，**只是脚本目标，不是本次确认的线上名称**；schema／角色已存在即拒绝，不能用于维护中重建或覆盖。
- bootstrap 要求应用表为空、两位不同且邮件已验证的 Auth 用户，创建家庭／成员／18 分类；不创建账户、设备、余额、计划或交易。该脚本家庭时区固定 Asia/Singapore，须先确认符合新环境。
- runtime 创建后仍 NOLOGIN，凭据／LOGIN 与 secret 绑定另行授权；账号配置和初次余额走正常业务入口，缺余额不能填零。
- 隔离验证脚本 smoke_s6_operator_sql.py 只接受自建可销毁 Docker，不接受托管 DSN。已有线上库的未来迁移应另行审查，不能套用 fresh SQL。

## 构建与发布

三个应用有各自 Dockerfile；后端 cloudbuild.yaml 接受 _IMAGE。使用与目标一致的上下文构建，固定 commit 与不可变 digest，再绑定现有私有配置。后端容器端口 7860，网页 8080；平台服务端口须匹配。

仅构建本地镜像时，从仓库根目录使用各应用目录作为 context：

```sh
docker build -t vibeledger-backend:local ai-ledger-backend
docker build -t vibeledger-web:local ai-ledger-web
docker build -t vibeledger-dashboard:local ai-ledger-dashboard
```

Streamlit 容器端口为 8501。构建不初始化数据库、不部署，也不自动带入私有运行配置。

发布前核对当前 service／region、两端镜像、私有 schema 与 checksum、运行角色、secret 版本、浏览器 Auth 和 Scheduler audience。保留前一兼容镜像及配置引用，不能凭旧 staging 名称部署。旧记录中的 concurrency 8／max instances 2 是历史容量设置，不是当前事实；容量须按实际数据库连接预算配置。

只读验证区分：

- /health：进程。
- /ready：数据库连接与 simplified checksum；Gemini 只查密钥是否配置。Gemini 不可用时 degraded 仍可提供手动功能，不证明模型调用成功。
- 财务路由无凭据拒绝；内部任务拒绝普通浏览器／设备 token；网页 /config 不含私有密钥。
- OpenAPI、实际 UI／API 指向、线上身份映射和权限。真实模型诊断见 [说明](../testing/EXPENSE_GEMINI_LIVE_SMOKE.md)。

CI 的容器和 synthetic dump/restore 检查证明测试环境行为，不表示做过生产备份或检查过当前线上资源。

## 月度计划任务

内部 POST /internal/spending-schedules/run 使用 Google OIDC，固定 SCHEDULER_AUDIENCE 与专用 SCHEDULER_SERVICE_ACCOUNT。Google issuer／audience／签名／expiry／email 等检查通过才运行；浏览器／设备认证不授权此路由。

若使用 Cloud Scheduler，专用身份只需目标服务 run.invoker；用 canonical 后端 URL 作 audience，按家庭使用时区选择日常触发，设置有界超时／重试。家庭自身时区决定到期日；历史 Singapore 00:15 配置不是当前在线任务证明。

至少一次调用／中断后再次执行使用已有 period identity，已完成不重复入账。故障修复后运行同一任务恢复，不重建 schema；Spending／Review 的补记同样可恢复。报表 GET 不创建交易。不能只看任务 HTTP 200 就认定家庭已完整补记，应检查 current_through 和待处理结果。

内部任务不接受调用方指定家庭／业务日，遍历有效家庭并使用各家庭时区。system 回执和计划月份身份保证重放；计划创建者是历史来源，即使其登录后来禁用，计划仍是家庭的持续指令，停用需显式暂停／取消计划。

## 请求恢复、客户端迁移与回滚

- 未确定命令保留原键，查询持久状态；404 后 cancel 并验证终态。已 committed 的请求不能被取消当作撤销。
- 设备令牌只显示一次，响应丢失先查设备列表；令牌直接保存至私人存储，不进文档／Git／诊断。未经授权不撤销旧设备。
- 切换后端／家庭时，先处理原环境未确定键，endpoint、token、pending 文件命名空间一同切换，避免跨环境重放。不要重播旧截图／PDF，或将测试历史复制成真实初始状态。
- 生产故障先暂停写入来源／定时任务，保留现有财务数据、回执、键和审计；系统没有全局 write-disable 开关。仅切换应用兼容镜像，不回退为 legacy 模型。
- 若需数据库写入围栏，必须单独授权且只针对专用生产角色；NOLOGIN 本身不能停止现有／池化连接，还需处理现存会话。不得对共享角色盲目操作。
- 已有任何余额／交易写入或结果未知时，不能简单将客户端切到旧库并丢弃新数据。跨 schema 恢复、写围栏、secret 切换、旧环境停用／凭据撤销都须有具体授权；不做 destructive down migration。

## 已保留的历史决策

2026-09-20 用户接受的真人抽样范围为一位用户、一次清晰消费 Shortcut、一个 88 行 MariBank PDF；旧两人／20 样本目标已免除。此范围不能写成双人全面验证，也不能自动要求补齐旧计划。旧 CI、镜像、云资源与逐次验收证据在 Git 历史中可查，不代表今天状态。

用户明确将备份、PITR、灾难恢复、备份保留与恢复演练排除项目要求，不设置 RPO/RTO 或服务商资格检查为阻塞。正常回执恢复、原子提交、保留已有线上数据和应用回滚边界仍有效。
