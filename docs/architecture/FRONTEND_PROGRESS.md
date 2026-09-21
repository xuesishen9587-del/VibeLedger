# 前端当前进度

React + TypeScript + Vite 已作为隔离 staging 的当前网页入口通过用户托管验收。
FastAPI 与 Supabase 数据和认证边界保持不变；旧 Streamlit 服务保留。

已验收范围：Owner 界面、真实 MariBank 88 行 PDF 整单导入、余额与投资流程、
设备生命周期、真实 iPhone Shortcut、Gemini 三类提取、Supabase ES256 JWKS 换钥。
这些是用户提供的验收结论。2026-09-20 用户明确确认只做了单人、一次清晰消费
截图和 PDF 测试，不再增加第二人或 20 张截图验收；这一限制已写入 S5 验收记录。

S5 新增：日常支出和待处理页先尝试补记到期计划，再加载数据；失败明确提示
支出可能不完整并提供重试。恢复不确定请求时复用原请求键；补记日期可见，
跨日后重新检查。后台报表 GET 不产生写入。

浏览器回归覆盖账单检查原因/已核对状态、全局阻塞恢复、PDF 识别进度、当前
登录身份，以及 S5 补记顺序、失败提示和同键恢复。另有真实 JWT/API/PostgreSQL
联合测试；外部模型与登录服务可使用替身，不能据此冒充新的真人验收。

最终提交、CI、隔离服务镜像摘要/版本与操作证据见
[完整 S5 记录](../deployment/S5_ACCEPTANCE.md)。PR #17 的最终状态以该记录为准。
S6/production cutover 和合并仍未获授权。历史前端设计见 FRONTEND_REDESIGN.md，
旧检查点保留在 Git 历史中。
