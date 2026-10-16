# VibeLedger 轻记（Android）

Kotlin + Jetpack Compose 原生截图记账助手，面向 OPPO / ColorOS 的手动使用场景。Android 8.0（API 26）及以上，无 Google Play 服务依赖，可直接安装 APK。应用复用现有设备 API；AI 识别、账户匹配、家庭身份和所有财务判断都由 VibeLedger 后端执行。

## 工程与运行

- `app/`：中文 Compose 界面、单图选择/系统分享、内存 JPEG 处理、Android Keystore 加密凭据和待处理键。
- `capture-core/`：不依赖 Android 的 Kotlin 状态机、HTTPS 客户端、模拟 HTTP 测试。
- `gradle/wrapper/`：官方 Gradle 8.11.1 Wrapper；发行包 SHA-256 固定，Wrapper JAR SHA-256 为 `2db75c40782f5e8ba1fc278a5574bab070adccb2d21ca5a6e5ed840888448046`。
- `app/src/androidTest/`：Keystore 持久化/隔离检查，使用合成凭据，只在可销毁模拟器运行。

安装 JDK 17、Android Studio 及 Android SDK Platform 35 / Build Tools 35.0.0。用 Android Studio 打开本目录；通过 IDE SDK 设置或本机 `local.properties` 指定 `sdk.dir`，不要提交该文件。固定 AGP 8.9.2、Kotlin 2.1.20 和 Compose BOM 2025.04.01，避免工具链随最新版本漂移；版本兼容参考 [AGP 8.9](https://developer.android.com/build/releases/past-releases/agp-8-9-0-release-notes) 和 [Compose BOM](https://developer.android.com/develop/ui/compose/bom)。

```sh
# macOS / Linux，在本目录
./gradlew :capture-core:test :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
# Windows PowerShell，在本目录
.\gradlew.bat :capture-core:test :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
```

APK 位于 `app/build/outputs/apk/debug/app-debug.apk`。可通过 USB 调试安装：

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

也可下载 APK 到手机，用系统文件管理器打开，仅为所用安装来源开启系统要求的安装权限。Debug APK 使用构建机器的调试签名；不同 CI runner 的签名可能不同，覆盖安装可能失败。不要为更新直接卸载带有未确定请求的应用：先用原身份恢复终态并核对网页。长期分发需要另行管理稳定签名，本版不包含生产签名密钥。

## CI 与 APK Artifact

根目录 `.github/workflows/android-ci.yml` 在此分支推送和相关 PR 上运行 Kotlin 单元测试、Android Lint 和 Debug 编译。成功后，在 GitHub PR 的 **Android CI → android → Artifacts** 下载 `vibeledger-android-debug`，解压取得 `app-debug.apk`；保留 14 天。`android-verification-reports` 包含测试及 Lint 报告。不访问生产数据库、真实模型或个人财务数据。

Keystore instrumentation 检查需另行启动可销毁模拟器，再运行 `./gradlew :app:connectedDebugAndroidTest`。默认 CI 不启动模拟器；本机缺 Java/SDK 时不能把未执行的 Android 测试视为通过。最终实际执行结果以 PR 的检查和验证记录为准，编译成功不代表 OPPO 真机已验收。

## 绑定独立设备

1. 各家庭成员使用自己的账号登录 VibeLedger 网页，在「设置 → 记账设备 → 添加设备」选择 **Android App**，填写手机名称。
2. 网页只显示一次设备 Token。首次打开轻记，输入可直接访问的 VibeLedger **HTTPS 后端地址**和这个 Token，点「验证并安全保存」。不要共用 iPhone 的 Token。
3. 地址只接受 HTTPS origin（可带 `/api/v1` 后缀），不接受 URL 用户名、密码、查询参数或任意路径。请求固定追加一次 `/api/v1`，不跟随重定向，包括同源重定向。
4. 设置显示连接是否验证成功。验证读取 `/api/v1/devices`，不写账本；服务端会在每次财务操作时重新鉴权。
5. 「清除本机凭据」只移除当前 Token，保留未确定键；不会在网页撤销设备。网页撤销设备后，App 停止确认并提示凭据失效。若旧 Token 已撤销，旧请求需在网页核对处理；新 Token 不可代替旧 actor 按键恢复。

设备 Token 区分用户/设备，账户归属仍在网页中显式设置，App 不指定家庭或猜测账户。iPhone Shortcut 仍使用原设备和 `ios_shortcuts` 平台；新增设备平台默认仍为 iPhone。

## OPPO 截图与分享

1. 在支付应用用手机系统截图，保留能说明金额、币种、商户、支付方式和消费日期的内容。
2. 打开轻记，点「选择一张截图」，使用系统文件/照片选择器选一张；无需授予整个相册权限。
3. 也可在 OPPO 相册或系统截图预览中点「分享」，选择「VibeLedger 轻记」。ColorOS 不同版本的菜单位置可能不同，本工程尚需 ColorOS 17 真机验证。只接收单张 `content://` 图片，不接收文件路径、远程 URL 或批量图片。
4. 输入可选原始备注后，点「开始 AI 识别」。图片最长边缩至 1600，转为不带 EXIF 的 JPEG（最多 2 MiB，远低于后端 10 MiB / 20 MP 限制），只在内存中处理。`captured_at` 是本次上传时间，带时区偏移；实际消费日期由后端从证据判定，App 不推定历史日期。
5. 自动保存直接显示服务器摘要；需要确认时显示服务器的金额/币种、商户、分类、支付账户、日期、分期信息和全部警告。自然语言补充更新草稿后重新展示摘要，不自动保存，也不覆盖初始备注。
6. 仅当服务端 `confirmation_ready=true`、`next_action=confirm` 且有当前版本时出现确认按钮。所有确认、修订、拒绝都发送 `expected_version`。分期及 `review_in_web` 只引导去网页处理，不在手机选择分期方案。

## 网络失败与待处理恢复

- 上传前将唯一 UUID 键同步写入加密存储；写入失败不上传。应用内单例及互斥锁丢弃重复操作，不把连点排队为下一次上传；HTTP 禁止自动重放与重定向。
- 超时、断网、异常响应、应用被杀均保留原键。下次启动先 GET 原键，不重传原图、不生成新键；也可点「重新查询本次结果」。没有后台重试或后台 AI worker。
- `processing` 保留待处理，可稍后或明确取消；`needs_confirmation` 恢复服务端当前摘要。所有 HTTP 409 会尝试按原键重读，随后必须重新核对并主动操作，不重放刚才的修改/确认。
- 只有 GET 返回 404 且 code 为 `REQUEST_NOT_FOUND` 才发送同键 cancel tombstone，阻止迟到的上传入账；取消结果不明仍保留键。其他 404 不作新键许可。
- 仅 `committed/rejected/failed` 清理该身份的本地待处理键；`next_action=done` 或 `retryable=true` 本身不能清理/重跑。取消不能撤销已保存交易。终态摘要仅保留在进程内，重启后到网页查看历史。
- 一次设备身份只有一个待处理键。切换地址/Token 时按规范化地址与 Token 摘要隔离命名空间，旧键继续加密保留；设置提示有其他身份未完成请求。重新输入**原地址和原 Token**可恢复。跨身份不会复用键或发送取消请求。
- 不保存原截图、Base64、备注、修订内容或财务响应到磁盘。选图后尚未上传就退出进程，需要重新选择。不要清应用数据/卸载来解决网络问题，否则无法恢复本地键。

## 安全与限制

AES-256-GCM 密钥留在 Android Keystore，Token、地址、命名空间和 pending map 位于 `noBackupFilesDir` 的原子加密文件。禁用自动备份，云备份和设备迁移规则排除应用存储；安全存储损坏时停止上传，不悄悄初始化覆盖。界面启用 `FLAG_SECURE` 防系统截图/任务预览泄漏，不将凭据存入 Compose saved state。应用不记录 HTTP/异常正文，不嵌入第三方分析/崩溃上传 SDK。使用平台 TLS 验证，无自定义绕过证书检查。

截图只发给配置的后端；后端仍会使用其已配置的 AI 服务，这是现有后端行为，App 不直连任何第三方识别服务。不申请存储、悬浮窗、无障碍、截屏或后台权限。无 WebView、无自动抓取账单、无完整报表功能。持久幂等保护同身份同键；用户主动在不同设备/新键提交同一笔消费仍需遵从后端重复候选审核，不能承诺跨来源无条件去重。

## 真机验收清单（仅用合成截图和独立测试后端）

- [ ] ColorOS 17 安装、冷启动、返回、横竖屏、大字体与系统栏布局。
- [ ] 手动选图、相册单图分享、首次未配置时分享；不申请相册/截屏等额外权限。
- [ ] 两个家庭成员使用独立设备 Token，服务端保留身份及账户归属。
- [ ] 自动保存摘要正确；待确认六项事实、分期及所有警告完整可读。
- [ ] 未就绪不出现确认；补充后重新核对；分期只能网页/稍后/取消。
- [ ] 上传中连点、旋转、切后台/强杀，恢复只查原键且总上传/入账各一次。
- [ ] 超时、401/403 撤销、409 网页编辑、404 tombstone、取消丢响应均保留/清理正确。
- [ ] 稍后→网页处理→冷启动恢复终态；已保存不能被取消撤销。
- [ ] 清本机凭据、切不同 Token/后端、切回原身份恢复；不在新身份取消旧键。
- [ ] 清理安全存储之前先核对网页；APK 升级签名检查与未确定请求保留。
- [ ] 合成凭据不出现在日志/截图/备份；合成原图及识别正文不写入应用存储。

后端事实核对入口：[路由](../ai-ledger-backend/app/api/routes/expenses.py)、[恢复及草稿动作](../ai-ledger-backend/app/api/routes/ingestion.py)、[生成 OpenAPI](../docs/api/openapi.json)、[Shortcut 边界](../docs/architecture/SHORTCUT_COMPATIBILITY_BOUNDARY.md)。本版未修改后端或 iPhone Shortcut，不执行迁移/部署。
