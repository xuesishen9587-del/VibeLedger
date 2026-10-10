# 真实 Gemini 诊断

CI 替换外部 Gemini 调用；通过不能证明当前部署的模型接受结构化 schema。此检查为显式选择的外部调用，会发送图片，但不写账本。不把 API key 写进命令、日志或截图。

## 无账本写入检查

在安装后端依赖的 ai-ledger-backend 环境执行 scripts/smoke_expense_gemini.py。通过私有环境加载 GEMINI_API_KEY，并核对 GEMINI_MODEL；默认 gemini-3.5-flash-lite，部署覆盖需另查。

使用获准、脱敏、可读且包含消费金额／币种／完整日期／商户的 JPEG 或 PNG。以下仅为虚构样例，实际事实须与图一致：

```sh
python scripts/smoke_expense_gemini.py --image /private/demo.png --amount 12.50 --currency SGD --date 2026-09-20 --merchant "Demo Cafe" --revision
```

脚本直接使用当前 GeminiService，核对提取事实、性质与日期证据，并检查合成修订只改变金额。不连接 DB、不创建／确认回执；仅输出诊断 UUID 与成功／失败摘要。参数若含真实财务事实也应置于私有非记录会话。通过只证明该图片的 live transport 和本地解析，不代表所有银行／截图均可用。

若要模拟实际部署，需使用相同依赖／配置／网络。默认后端 Dockerfile 不复制 scripts，因此不能假设运行容器内已有此脚本；使用对应 commit 的私有诊断环境，不能临时改线上服务来凑环境。

## 可选手机端验证

手机测试会写真实记录，须有本次明确范围／授权，或使用独立测试家庭。生产 Shortcut 已由用户确认，两人共用同一逻辑；按 [实际动作](../architecture/SHORTCUT_COMPATIBILITY_BOUNDARY.md) 判断它能展示哪些字段，待确认事实应在网页核对。

只保留必要的版本／模型、request_id、结果与是否保存证据，不记录 bearer、截图、原始响应或提示。网络未知按原键 GET 恢复；修订保持待确认，确认／重放不得新增第二笔。failed 为持久终态，升级后同键不会重新识别；核实无财务写入后，才可有意新建一次测试。

不要求重做历史两人／20 张截图验收，也不自动部署或触发真实消费。

## 安全诊断

以 gemini_failure 和诊断／回执 request_id 过滤日志。允许字段只有 operation、request_id、category、phase、exception class、数字 upstream status。

类别包括 upstream、timeout、response_parse、response_validation、domain_validation、configuration。上游 400 本身不能证明 schema 错误；通过直接诊断缩小范围。禁止启用泄漏请求／SDK 内容的 debug 日志或记录异常正文、traceback、headers、模型响应、密码与 prompts。

公开 API 返回通用 CAPTURE_DEPENDENCY_UNAVAILABLE。失败必须保留回执恢复和草稿修订原子性，不靠打印私有内容排查。
