# 实际 iPhone Shortcut 与后端核对

本文件区分仍在使用的生产导出与 V2 候选，候选尚未部署或完成 iPhone 验收。生产只有一份 Shortcut 逻辑，下面的本地验收文件只是同一逻辑的测试配置，不是额外生产入口。

生产导出为 `E:\VibeLedger_V2\shortcut\VibeLedger_Production_unsigned.shortcut`（从仓库根目录为 `../shortcut/VibeLedger_Production_unsigned.shortcut`）。用户已确认这是当前生产版本，两位家庭成员使用完全相同的逻辑，只维护这一份；各自手机本地保存不同设备 token，以区分设备身份。2026-10-09 的解析针对该文件，没有读取 token 文件，也没有将导出／解包内容／私有 endpoint／凭据提交到仓库。

## 解析依据

文件不是直接可读 plist，而是 AEA 签名容器。使用本地 AEA 解码器及 Python plistlib，在内存中完成嵌入公钥签名、内部完整性校验、解包和 plist 解析，得到 **176 个动作**。这是文件结构与完整性检查，不是 Apple 签名证书信任链认证，也不代替实际 iOS 运行。

下列为动作表直接证实的行为，不根据后端反推手机操作：

1. 从配置的本地 File Provider 文件读取 `device-token.txt` 为 DEVICE_TOKEN；BASE_URL 为导出中的固定 HTTPS origin。没有输出两者的值。
2. 检查本地 pending-key 文件。存在时先 GET 按键回执：needs_confirmation 取 request_id；committed／rejected／failed 展示 display_summary 并删除 pending 文件。
3. 查回执得到 REQUEST_NOT_FOUND 时，POST 按键 cancel；只有收到上述终态才删文件，否则展示响应并退出。其他恢复错误同样保留文件。
4. 无 pending 时，用 `yyyyMMdd-HHmmss` 时间和六位随机数构造新键，**先写 pending 文件**，再截当前屏幕。不是从分享输入选图，也不是 UUID 键。
5. captured_at 用 `yyyy-MM-dd'T'HH:mm:ssXXX`，包含时区偏移；图缩至宽 640，转 JPEG（质量约 0.655，关闭元数据），base64 无换行。
6. 新请求 POST /expenses，提交 idempotency_key、captured_at、client_version、image.mime_type=image/jpeg、image.base64。**没有初始备注提问，也没有 note 字段。**
7. needs_confirmation 仅取 request_id，然后进入确认／自然语言修订／拒绝菜单。**在菜单之前没有展示 draft、金额、币种、日期、账户、分类或 warnings 的动作。**
8. 确认／拒绝为无 JSON body 的 POST；符合目标终态才展示摘要并删 pending 文件，否则展示响应、保留键退出。
9. 修订菜单询问 correction_note，POST /revise，直接 Quick Look 展示返回字典并退出；没有当次循环确认。pending 保留，下次运行 GET 恢复该草稿。

## 协议对照

所有请求使用 Bearer DEVICE_TOKEN；以下路径均相对 `/api/v1`。

| 动作 | 仓库后端 | 核对结果 |
| --- | --- | --- |
| POST /expenses | 设备消费截图捕获 | 字段形状、JPEG/base64、带偏移 captured_at 匹配；note 可选但手机未发送 |
| GET /ingestion-requests/by-key/{key} | 原 actor 范围恢复 | 匹配 |
| POST /ingestion-requests/by-key/{key}/cancel | 原键取消／not-found tombstone | 导出已有此动作，旧文档“手机没有取消”不准确 |
| POST /ingestion-requests/{id}/confirm | 兼容设备无 body | 同设备为最后编辑者时匹配；浏览器改过草稿会 DRAFT_CHANGED |
| POST /ingestion-requests/{id}/revise | correction_note | 字段匹配；设备不带 expected_version，服务端只检查识别期间草稿是否再变化 |
| POST /ingestion-requests/{id}/reject | 兼容设备无 body | 匹配；不撤销已 committed 结果 |

动作直接取用的是 `status`、`request_id`、`display_summary`、`error.code`；其余主要通过 Quick Look 显示整个返回字典。不能将旧文档中的 transaction_id／warnings／预填金融字段对话框写成实际消费行为。

后端的 8–200 字符键接受此时间加随机数格式，不要求 UUID。正常无 pending 捕获只有一次 POST，没有额外预检。GET 恢复和 404 后 cancel 是恢复分支。

## 已知限制

- **接口路径和字段匹配不等于完整交互匹配。** 待确认金融事实未在确认菜单前展示，用户不能在该步骤核对低置信金额／日期；自然语言修订也不是初始交易备注。
- 没有显式网络 try/catch、HTTP 状态读取、自动轮询或重试。processing 返回字典时展示后退出，保留键；没有给 processing 的主动取消菜单，现有 cancel 只在 not-found 分支。
- 4xx／5xx 是否被 iOS “获取 URL 内容”动作作为字典交给后续步骤，仍需实际设备证据。如果系统动作先中止，404 后的 cancel 分支可能不可达；文件结构本身不能证明它能运行。
- 浏览器改过草稿后的无版本 confirm／reject 会返回 409 并保留键；设备 revise 可在当前草稿上修订并重新成为最后编辑者，随后无 body confirm 可以通过。这是既有兼容策略，不是已证明的授权漏洞；实际手机菜单缺少修订后金融事实的结构化核对，仍有误确认风险。复现依据见 [约束复核](CONTRACTS.md#10-复核依据与最小复现)。
- 一个本地 pending 文件，无显式运行锁；同时启动或手动删除／改写文件可能破坏手机恢复纪律。后台幂等只保护同 actor 同键，不能证明不同新键绝不重复。
- 640 像素压缩是实测导出配置，复杂小字截图的识别效果未由解析验证。也未向导出中的 URL 发请求，不能证明线上版本和仓库一致。

## 验证边界与恢复复核

生产版本与两人共用逻辑已由用户确认，无需再次导出。仍未验证 iOS 的 HTTP 错误行为，也未读取线上 commit／镜像；字段匹配仅针对仓库后端。

最小设备复核可在独立测试环境使用虚构键：保留 pending 文件 → GET 返回 404 JSON／REQUEST_NOT_FOUND → 观察是否进入 cancel 并在 rejected 后清除。若“获取 URL 内容”直接中止，该分支才可判定不可达；目前仅解析到动作，不能认定已有网络恢复故障。记录 iOS 版本和脱敏动作结果即可，无需令牌或真实截图。

另一条路径是 POST 发出后中断 → 重跑按原键 GET：committed 展示并清除，needs_confirmation 恢复菜单，processing 保留文件退出。后端 tombstone、迟到识别与重复确认已有集成测试；本轮未运行数据库测试，且这些测试不覆盖 iOS 动作。

pending 文件仅保存键，没有原始图像／请求正文；不能据此重构同一 POST。换环境时 endpoint、token 和 pending-state 文件命名空间必须成组迁移；先解决原环境未确定键，不能只改 URL。

## V2 候选与验收

[转换器](../../scripts/shortcut_v2.py) 在本地将上述真实导出动作转换为候选（235 动作）。输入／输出必须在仓库外；不提交导出、解包文件、endpoint、token 或 File Provider 元数据。转换器拒绝不匹配的原始动作布局，自动化测试使用合成布局及模拟执行器，不是 Apple 运行时。原生产导出不覆盖。

首次 iOS 27.0.1 验收已成功签名并导入，但上传时 Get Contents of URL 未识别生成的 URL 输入。修订后的所有 HTTP 动作用原生产文件的 WFTextTokenString 参数包裹 URL 动作输出（OutputName=URL）；生成器及测试拒绝旧的裸 WFTextTokenAttachment。生产 origin 追加 /api/v1，验收输入已含 /api/v1，两者统一为 API_BASE_URL，所有请求只保留一次 API 前缀。结构核对不等于修订后已在手机运行成功，需重新签名／导入复核。

- 截图发生在备注输入之前；备注可留空，POST 明确发送 note=""，有内容保存到 remarks。取消输入表单会由 iOS 中止，不上传；没有 WithNote 入口。
- 正常路径一次 POST 图片与一次 AI；committed 展示六项摘要后清理 pending，不增加确认步骤。
- needs_confirmation 用 display_summary 作菜单提示，显示金额／币种、商户、分类、支付账户、日期、分期。后端 next_action 和 confirmation_ready 控制操作；Shortcut 不解析金融事实或自行猜账户。非就绪不显示确认保存，取消始终存在。
- 补充输入展示 supplement_prompt，发送 correction_note 与 expected_version；空输入不发请求。返回更新摘要后在本地重复菜单，不额外 GET、不重传图片。confirm／reject 同样提交当前版本。单次最多 20 次菜单循环，达到上限保留 pending，下次继续。
- 分期只显示“检测到分期，请在网页端处理。”和稍后／取消，不在手机选择方案。选择稍后保持 pending，网页处理后下次运行可恢复终态。
- 网络／版本结果不明保留原键，可明确选择重新查询／稍后／取消；重跑先 GET 原键，无新图片／新键。404 cancel tombstone 保留；只有 committed／rejected／failed 清理文件，next_action=done 本身不作为清理依据。

候选是 unsigned binary plist，修改后原签名失效。需在 Mac 用 [Apple 官方命令](https://support.apple.com/guide/shortcuts-mac/run-shortcuts-from-the-command-line-apd455c82f02/mac) 签名；anyone 模式会发送文件给 Apple 验证，签名前本地检查配置，签名后不要编辑文件字节。没有 Mac 远程访问，因此本轮不能代签名或宣称已导入成功。

```sh
shortcuts sign --mode anyone --input VibeLedger_V2_acceptance_unsigned.shortcut --output VibeLedger_V2_acceptance.shortcut
```

先签名 **acceptance 文件**，AirDrop 到 iPhone，命名为 VibeLedger V2 本地验收。它不读取生产 token，使用合成测试 token；pending 为 vibeledger-v2-acceptance-key.txt，与生产 pending-key.txt 分开。保留生产 Shortcut 作为现用版本。服务支持 Windows 和 Mac（Python 3.9+，无需后端依赖）。电脑与手机使用同一可信局域网，在此开发分支仓库根目录运行：

```sh
python3 scripts/shortcut_acceptance_server.py --bind 0.0.0.0
```

Windows PowerShell 使用已安装的真实 Python 路径启动同一脚本（例如 `& "C:\Users\Illidan\miniconda3\python.exe" -u scripts/shortcut_acceptance_server.py --bind 0.0.0.0 --port 8089`）；签名仍在 Mac 完成。

电脑浏览器打开 http://127.0.0.1:8089，选择场景；手机浏览器打开 http://电脑局域网IP:8089/sample 作为合成账单，每次启动验收 Shortcut 时填写 http://电脑局域网IP:8089/api/v1（允许本地网络访问）。仅用合成截图／备注。服务不连接数据库或 Gemini、不持久化图片／回执、不记录请求正文，退出即丢失回执；测试期间保持服务运行，结束后 Ctrl-C。默认仅绑定本机，显式 bind 为手机开放局域网；不要公网转发。场景只影响新请求，切换前先结束旧 pending。

| iOS 27.0.1 场景 | 操作 | 预期与计数增量 |
| --- | --- | --- |
| 正常自动保存 | 电脑选 auto，手机运行、备注留空 | 直接展示六项摘要；无确认菜单；uploads+1、transactions+1 |
| 空备注 | auto，备注空白继续 | 正常保存，摘要无备注；不报必填错误，无 revise／confirm |
| 有备注 | auto，输入“测试备注” | 摘要含原备注；一次上传，无补充请求 |
| 缺字段补充 | missing，选择补充信息，输入“38.50元，2026年10月1日，测试银行卡1234” | 提示缺金额／日期及账户线索；初始不出现确认；补充后摘要变为38.50并出现确认；明确确认才保存 |
| revise 后重新确认 | revise，选择补充、输入“金额38.50元” | 新摘要返回原菜单；原备注不变；uploads+1、revisions+1、confirms+1、transactions+1 |
| 网络异常恢复 | lost_response，手机上传后等待网络动作报错，再运行相同 Shortcut | 本地 pending 保留；第二次 GET 原键得到已保存摘要、清理；总 uploads+1、transactions+1，recoveries+1，无第二次上传 |

补充核对：installment 只展示指定提醒和网页／取消；processing 可稍后或取消，不清理未知状态。查看 /metrics（网页链接），仅计数，不含图片、备注或密钥。回传场景名、成功／失败、脱敏提示和计数增量即可，不发送 token／原始 JSON。

附加 HTTP 404 复核：选择 processing，手机选稍后恢复，退出并重启测试服务（清空合成回执），再运行手机 Shortcut。GET 返回真实 404／REQUEST_NOT_FOUND；若 iOS 将 JSON 交给后续动作，应发送原键 cancel tombstone 并清理；若 URL 动作中止，应保留 pending，回报该行为，不手动删除或换键。

**验收边界：** 此本地 API 固定生成合成识别／修订结果，不解析自然语言；错误按场景返回 401／404／409／422 等状态，便于实际 iOS 复核。真实 Gemini／数据库由后端测试覆盖。首次导入与 Windows 服务启动已有用户／本机证据；修订后 URL 输入、空输入、布尔条件、动态菜单、文件权限及断网／HTTP 错误行为仍需设备实测。若动作中止，原键应仍存在。文件解析／模拟执行和 CI 不能替代真实手机验收。

生产候选 VibeLedger_V2_unsigned.shortcut 保留原设备文件与 endpoint；仅在后端合入并获得部署授权、手机验收通过后再签名替换现用 Shortcut。当前不指向生产测试、不部署；避免用新版 Shortcut 调用尚不提供展示字段的旧后端。
