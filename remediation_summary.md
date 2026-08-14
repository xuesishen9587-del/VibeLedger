# VibeLedger 整改与功能升级说明 (问题 01 - 05)

本文件详细记录了针对代码审计指南中 **P0 级别问题 01 至 05** 的修改执行情况。包含数据库结构更新、后端业务重构、前端 UI 优化以及验证测试。

---

## 🛠️ 修改执行情况摘要

### 问题 01：请求幂等性 (Idempotency Key) 
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-backend/main.py`
* **整改方案**：
  * 在 `transactions` 表中新增 `idempotency_key` (TEXT UNIQUE) 与 `parent_idempotency_key` (TEXT) 字段。
  * 后端 API 接口 `RecordRequest` 开始接收客户端提供的唯一键 `idempotency_key`。
  * **双层防重校验**：
    * **第一层（系统级幂等）**：API 收到请求后首先查库，若存在相同 `idempotency_key` 则瞬间返回成功（no-op 成功），无需再次调用 Gemini 或操作数据库，防止网络抖动导致的重复入账。
    * **第二层（业务级去重）**：针对不同幂等键的手动重复录入，如果当天、同金额、同账户且**剥离分期前缀后备注相同**，仍会判定为 `DUPLICATE_TRANSACTION` 并予以拦截，同时兼容了正常的同额多笔消费。
  * 对分期交易和多账户对账使用子幂等键（`{key}_inst_{i}` / `{key}_adj_{acc_name}`）和父键配合，防范分期重试漏洞。

### 问题 02：并发余额更新与 Lost Update 防御
* **涉及文件**：`ai-ledger-backend/database.py`
* **整改方案**：
  * 废除了原有“在 Python 中读取余额 -> 计算 -> 写入”的非原子操作。
  * 改造为**数据库原生原子更新**：`UPDATE accounts SET current_balance = current_balance + :delta WHERE ...`，利用关系型数据库行级锁保护余额。
  * 对于转账（同时更新两个账户），为彻底防范交叉并发更新时的数据库死锁，在代码中对两个账户名进行**字母排序锁定**，强制规定固定顺序更新。

### 问题 03：跨币种转账安全校验与智能推导
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-backend/main.py`
* **整改方案**：
  * `transactions` 表新增记录明细字段：`from_amount`, `from_currency`, `to_amount`, `to_currency`, `fx_rate`。
  * **杜绝静默 1:1 Fallback**：若两个账户币种不同，系统强制校验两个方向的金额。
  * **智能推导**：当还款至美元信用卡且备注含“还清”等词时，系统会自动结合 **Issue 05** 查询该卡的**本期剩余应还**作为美元数额。
  * **安全拦截**：若仍然无法凑齐双向金额，拒绝写入并向客户端抛出 `CROSS_CURRENCY_MISSING_INFO` (HTTP 400)，促使快捷指令弹出输入框提示。同时废除了以前硬编码的 `7.2` 汇率损益逻辑，以真实交易汇率入库。

### 问题 04：投资账户对账盈亏语义拆分
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-dashboard/database.py`, `ai-ledger-dashboard/app.py`
* **整改方案**：
  * 投资账户对账（Reconciliation）产生的差额，在数据库中写入的交易类型由原来的 `income` 改为 **`adjustment`**，防止干扰现金流量指标。
  * **Dashboard 统计图表兼容**：在 Dashboard 加载数据时，于 Pandas 数据层加了映射，将投资收益分类的 `adjustment` 在统计口径下合并进 `income` 中，从而使现有的所有月度图表、趋势图和占比分析依然完整包含投资盈亏。

### 问题 05：信用卡账单周期与还款计划配置
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-dashboard/database.py`, `ai-ledger-dashboard/app.py`
* **整改方案**：
  * `accounts` 表新增 `billing_day` (账单日) 与 `due_day` (还款日) 字段。
  * 实现了标准信用卡期数核算算法，自动推导最近已出账单区间并计算：
    * **已出账单 (Statement Balance)**: 账单区间内的所有消费。
    * **本期已还 (Repaid Amount)**: 还款窗口期内的还入金额。
    * **剩余应还 (Remaining Due)**: 已出账单 - 本期已还（现金预备的精准参考）。
    * **未出账单 (Unbilled Balance)**: 账单日后的新增消费。
  * 引入了 `safe_replace_day` 函数，当账单日为 31 号且身处小月/2月时自动安全限制到当月最后一天，防止替换报错。

### 🎨 Dashboard 界面优化与年度统计 (新增)
* **添加了“年度统计中心”**：可按年份检索，直观显示年度累计收支占比饼图及月度收支双柱趋势图。
* **精炼移动端排版**：
  * 表格简化为只显示关键的五列，账单日/还款日中的 `.0` 小数已剔除并转为整型。
  * 采用分列布局，在信用卡表格右侧列出了全局的 **“信用卡总本期应还”** 和 **“信用卡总未出账单”** Metric 卡片。
  * 移除了多余的“校准于”中文字符，缩短横向间距，并注入了自定义 CSS 紧凑页面 Padding 和行距，极大提升了手机浏览器浏览体验。

---

## 💾 数据库升级数据
您所有信用卡的账单日与还款日已成功批量初始化升级完毕：
* **花呗 (Huabei)**：账单日 31 号，还款日 8 号
* **农行银联 (ABC_CUP_Credit)**：账单日 16 号，还款日 4 号
* **工行银联 (ICBC_CUP_Credit)**：账单日 18 号，还款日 6 号
* **中行银联 (BOC_CUP_Credit)**：账单日 7 号，还款日 27 号
* **工行Visa (ICBC_Visa_Credit)**：账单日 31 号，还款日 25 号
* **建行Visa (CCB_Visa_Credit)**：账单日 23 号，还款日 13 号

---

## 🧪 回家后在另一台电脑上运行与测试

当您在另一台电脑的 Antigravity 或本地环境上继续开发时，请按如下指引进行同步和测试：

### 1. 同步代码
```bash
git pull
```

### 2. 自动升级数据库结构
当您运行本地后端或测试用例时，代码中的 `database.init_db()` 会在启动时自动执行，运行 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，**无损自动将您家里的开发/生产环境数据库结构升级**。

### 3. 运行本地自动化测试
在 backend 的虚拟环境下运行以下命令，即可执行全套回归测试：
```bash
ai-ledger-backend/venv_backend/bin/python ai-ledger-backend/test_idempotency.py
```
*(注：测试会自动在您的数据库中建立 'Test_CNY_Debit' 和 'Test_USD_Credit' 账户进行校验，并在测试结束前自动清理测试数据。)*
