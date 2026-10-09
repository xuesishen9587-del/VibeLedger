# 实际 iPhone Shortcut 与后端核对

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
