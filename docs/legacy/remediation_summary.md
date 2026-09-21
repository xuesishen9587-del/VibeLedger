# [LEGACY / HISTORICAL] VibeLedger 早期整改与功能升级说明 (问题 01 - 05)

> ⚠️ **历史文档归档说明 (LEGACY ARCHIVE)**  
> 本文件记录的是旧版系统针对早期 P0 级别问题 01 至 05 的修复历史记录。  
> **本文件仅作为历史背景与遗留问题排查参考，不是目标架构规范。**  
> 目标架构与开发规范请参见：
> - [`TARGET_DOMAIN_MODEL.md`](../../TARGET_DOMAIN_MODEL.md)
> - [`docs/architecture/`](../architecture/README.md)

---

## 🛠️ 历史修改执行情况摘要

### 问题 01：请求幂等性 (Idempotency Key) [历史实现]
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-backend/main.py`
* **历史整改方案**：
  * 在旧版 `transactions` 表中新增 `idempotency_key` (TEXT UNIQUE) 与 `parent_idempotency_key` (TEXT) 字段。
  * 后端 API 接口 `RecordRequest` 开始接收客户端提供的唯一键 `idempotency_key`。
  * **双层防重校验**：
    * **第一层（系统级幂等）**：API 收到请求后首先查库，若存在相同 `idempotency_key` 则返回成功，无需再次调用 Gemini 或操作数据库。
    * **第二层（业务级去重）**：针对不同幂等键的手动重复录入，如果当天、同金额、同账户且**剥离分期前缀后备注相同**，判定为 `DUPLICATE_TRANSACTION` 并拦截。
  * *[目标架构升级]*：目标架构将幂等所有权由 `transactions` 移至专用的 `ingestion_requests` 请求生命周期表。

### 问题 02：并发余额更新与 Lost Update 防御 [历史实现]
* **涉及文件**：`ai-ledger-backend/database.py`
* **历史整改方案**：
  * 废除了原有“在 Python 中读取余额 -> 计算 -> 写入”的非原子操作。
  * 改造为**数据库原生原子更新**：`UPDATE accounts SET current_balance = current_balance + :delta WHERE ...`，利用关系型数据库行级锁保护余额。
  * 对于转账（同时更新两个账户），为防范交叉并发更新时的数据库死锁，在代码中对两个账户名进行**字母排序锁定**。
  * *[目标架构升级]*：目标架构建立了专用的 `account_state` 行级锁，并按 UUID 排序加锁。

### 问题 03：跨币种转账安全校验与智能推导 [历史实现]
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-backend/main.py`
* **历史整改方案**：
  * `transactions` 表新增明细字段：`from_amount`, `from_currency`, `to_amount`, `to_currency`, `fx_rate`。
  * **杜绝静默 1:1 Fallback**：若两个账户币种不同，系统强制校验两个方向的金额。
  * **安全拦截**：若无法凑齐双向金额，拒绝写入并向客户端抛出 `CROSS_CURRENCY_MISSING_INFO` (HTTP 400)。废除了以前硬编码的 `7.2` 汇率损益逻辑，以真实交易汇率入库。

### 问题 04：投资账户对账盈亏语义拆分 [历史实现]
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-dashboard/database.py`, `ai-ledger-dashboard/app.py`
* **历史整改方案**：
  * 投资账户对账产生的差额，在数据库中写入的交易类型由原来的 `income` 改为 **`adjustment`**。
  * **Dashboard 兼容（已废弃设计）**：旧版在 Dashboard 加载数据时于 Pandas 层将 `adjustment` 合并进 `income`。目标架构已彻底将 `investment_pnl_periods` 与家庭日常 `cash_income` 隔离。

### 问题 05：信用卡账单周期与还款计划配置 [历史实现]
* **涉及文件**：`ai-ledger-backend/database.py`, `ai-ledger-dashboard/database.py`, `ai-ledger-dashboard/app.py`
* **历史整改方案**：
  * `accounts` 表新增 `billing_day` (账单日) 与 `due_day` (还款日) 字段。
  * 实现了基于日期区间的推导算法，估算：
    * **已出账单 (Statement Balance)**: 账单区间内的消费。
    * **本期已还 (Repaid Amount)**: 还款窗口期内的还入金额。
    * **剩余应还 (Remaining Due)**: 已出账单 - 本期已还。
    * **未出账单 (Unbilled Balance)**: 账单日后的新增消费。
  * *[目标架构升级]*：目标架构引入了独立的权威 `credit_card_snapshots` 表与 Statement 真实账单校准，不再单纯依赖日期区间推导。

---

## 💾 历史初始数据参考
历史配置的信用卡账单日与还款日示例（仅供参考，不作为生产硬编码）：
* **花呗**：账单日 31 号，还款日 8 号
* **农行银联**：账单日 16 号，还款日 4 号
* **工行银联**：账单日 18 号，还款日 6 号
* **中行银联**：账单日 7 号，还款日 27 号
* **工行Visa**：账单日 31 号，还款日 25 号
* **建行Visa**：账单日 23 号，还款日 13 号

---

## ⚠️ 历史测试与运行警告 (NEUTRALISED)

> 🛑 **重要安全提示**：  
> 以下为历史记录中的测试说明。**切勿直接运行旧脚本或对远端/生产数据库执行测试**。当前测试与验收规范见 [`IMPLEMENTATION_PLAN.md`](../architecture/IMPLEMENTATION_PLAN.md)，使用独立隔离的本地/测试数据库。

### 历史测试说明 (仅作档案留存，请勿直接执行)
- 历史代码曾在 `ai-ledger-backend/test_idempotency.py` 中直接向配置的数据库写入 `Test_CNY_Debit` 账户。
- 历史启动逻辑依赖 `database.init_db()` 执行运行时 `ALTER TABLE` DDL。目标架构已将 DDL 统一收归到版本化迁移脚本中。
