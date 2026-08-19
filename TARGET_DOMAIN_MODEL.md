# VibeLedger Target Domain Model

> Status: **Frozen Target Domain Model (Final consistency review complete)**
> Purpose: 作为后续 Codex / Antigravity 架构设计与开发的业务约束。
>
> 原则：**保持家庭账本准确到“足够可信”，优先降低日常使用摩擦，不构建复杂家庭 ERP。**

---

## 1. Core Principles

### MUST

- 家庭整体账本，两人可查看全部数据。
- Account、Transaction、Snapshot、Reconciliation 四类对象必须分离。
- 普通消费以**实际交易日期**归属月份；Statement posting date 仅用于匹配。
- Statement 不是强制月度流程，只是高精度 reconciliation 工具。
- Snapshot、Statement、Dashboard 手工余额均可作为账户权威校准来源。
- 小额差异允许自动 `reconciliation_adjustment`。
- 投资市值变化不得计入家庭收入。
- 内部转账和信用卡还款不得计入收入或消费。
- 原始币种金额永久保留。
- 所有修改、删除必须可审计；删除采用 soft delete。
- PDF 解析成功立即删除；失败最长保留 24h；PDF 密码永不持久化。

### SHOULD

- 高频 Expense Shortcut 保持无额外交互。
- 一个入口对应一个明确业务意图，不建立万能 AI 分类入口。
- Transfer / Snapshot 如使用 Shortcut，应为独立专用 Shortcut。
- Statement 重复上传无需检测 PDF 是否重复，但 reconciliation 必须 replay-safe。

### OUT OF SCOPE — Product v1 / MVP

- AA / 夫妻债权债务。
- 投资具体持仓、成本、证券交易解析。
- 信用卡最低还款、逾期、提前结清分期。
- 精确汇兑损益核算。
- 历史旧数据迁移。

---

# 2. Domain Model

## 2.1 Account

表示真实金融账户。

主要类型：

- `cash`
- `savings`
- `credit`
- `investment`

核心属性：

```text
id
name
institution
account_type
currency
owner_user_id
status
billing_day          nullable
due_day              nullable
linked_cash_account_id nullable
created_at
updated_at
```

规则：

- 一个 Account 只对应一个币种。
- 多币种银行账户拆为多个 Account。
- Account 可停用，不删除历史。
- `linked_cash_account_id` 仅作为转账匹配先验，不代表自动发生 transfer。

---

## 2.2 Transaction

表示真实发生的资金或经济事件。

`transaction_type`：

```text
expense
cash_income
refund
transfer
fee
reconciliation_adjustment
opening_balance
```

投资盈亏不使用 Transaction 表示，见 Snapshot / Investment P&L。

核心字段：

```text
id
transaction_type
occurred_on
occurred_at            nullable
posted_on              nullable

from_account_id        nullable
to_account_id          nullable

original_amount
original_currency

from_amount            nullable
from_currency          nullable
to_amount              nullable
to_currency            nullable
effective_fx_rate      nullable

account_leg_status     nullable (estimated / authoritative)

category_id            nullable
merchant               nullable
remarks                nullable

source
status                 (committed / voided)
confidence             nullable

source_request_id      nullable
statement_batch_id     nullable

created_at
updated_at
deleted_at             nullable
delete_reason          nullable
```

### Foreign Currency Credit Card Expense Rule

外币信用卡消费（`original_currency` $\ne$ 账户币种）：
- **Shortcut 录入时**：永久保留 `original_amount` / `original_currency`；使用参考汇率计算预估扣款金额（`from_amount` / `from_currency`），标记 `account_leg_status = estimated`，并以此预估值更新 `account_state` 负债。
- **Statement 出账对账时**：对账原子提交时以银行权威结算金额覆盖 `from_amount`，置 `account_leg_status = authoritative`，将结算投影差额（$$\text{projection\_delta} = \text{projection\_effect}(\text{after}) - \text{projection\_effect}(\text{before}) = -68.20 - (-68.90) = +0.70\text{ USD}$$）原子补偿至 `account_state`（使卡内负债从 $-68.90$ 修正为 $-68.20$），记录 `posted_on`，冻结历史 reporting FX，并记录审计日志。
- **重要约束**：预估仅允许用于外币信用卡消费扣款腿；跨币种内部转账（`transfer`）必须提供双向真实金额，严禁用参考汇率捏造。

### Fee & Category Rules

- **`fee`（手续费）**：独立的 `transaction_type`，属于家庭现金支出，必须关联支出分类（`category_id`），计入家庭总支出。转账手续费作为同一操作下的独立 `fee` 交易，不计入转账本金。
- **分类规则**：
  - `expense` $\to$ 必须关联支出分类
  - `fee` $\to$ 必须关联支出分类
  - `cash_income` $\to$ 必须关联收入分类
  - `refund` $\to$ 可继承原消费分类
  - `transfer` / `opening_balance` / `reconciliation_adjustment` $\to$ 分类必须为 NULL

### Soft Delete & Void Rules

- `status = committed` $\iff$ `deleted_at IS NULL`
- `status = voided` $\iff$ `deleted_at IS NOT NULL` 且必须提供 `delete_reason`
- 软删除与作废（void）为同一生命周期操作，原子反向回退 `account_state` 投影并记录不可变审计事件。

### Date Rule

普通消费：

```text
occurred_on = 实际交易业务日期 (DATE, 必需, 报表归属口径)
occurred_at = 实际交易精确时间 (TIMESTAMPTZ, 可选)
posted_on   = Statement/银行入账日期 (DATE, 可选, 仅用于匹配与对账)
```

报表使用 `occurred_on`。

分期例外：见 Installment。

---

## 2.3 Transaction Link

用于表达交易间关系。

```text
id
source_transaction_id
target_transaction_id
relation_type
```

`relation_type`：

```text
refund_of
reversal_of
installment_of
```

退款必须作为独立 `refund` 流水，不删除原消费。

---

## 2.4 Account Snapshot

表示某时间点账户的权威状态。

```text
id
account_id
as_of
balance
currency

snapshot_type
source

created_at
```

`snapshot_type`：

```text
balance
investment_valuation
```

`source`：

```text
shortcut
statement
dashboard_manual
```

Snapshot 不是 Transaction。

---

## 2.5 Credit Card Snapshot

信用卡独立保存账单状态。

```text
id
account_id
as_of

statement_period_start
statement_period_end

statement_balance
remaining_statement_due
unbilled_balance
current_outstanding

currency
source
created_at
```

三个重要概念必须分离：

```text
statement_balance
unbilled_balance
current_outstanding
```

---

## 2.6 Investment Snapshot

可直接复用 `account_snapshots`，但业务语义如下：

```text
account_id
as_of
total_asset_value
currency
```

Product v1 投资 Statement 只解析：

```text
总资产
明确入金
明确出金
```

不解析：

```text
具体持仓
交易数量
成本
证券买卖
```

投资期间收益：

```text
investment_pnl
=
ending_value
- beginning_value
- net_contributions
```

投资 P&L：

- 进入投资收益与净资产曲线；
- 不进入 `cash_income`；
- 不进入家庭日常收入。

---

# 3. Supporting Tables

## 3.1 Users

```text
id
display_name
default_currency
created_at
```

两人均可查看全部家庭数据。

---

## 3.2 Devices

```text
id
user_id
device_name
api_token_hash
status
created_at
last_seen_at
```

两部 iPhone 使用不同 token。

---

## 3.3 Categories

```text
id
name
type
status
```

`type`：

```text
expense
income
```

允许 Dashboard 自定义。

---

## 3.4 Account Aliases

用于 AI 和 Statement 匹配。

```text
id
account_id
alias
```

账户识别规则不得继续硬编码在 Python Literal 中。

---

# 4. Statement / Reconciliation Model

## 4.1 Reconciliation Batch

每次 Statement 上传形成一个独立批次。

```text
id
account_id

period_start
period_end

status

authoritative_balance      nullable
statement_balance          nullable
current_outstanding        nullable
unbilled_balance           nullable

matched_count
created_count
pending_count
adjustment_amount

created_at
committed_at               nullable
```

`status`：

```text
processing
ready
needs_review
committed
rejected
failed
```

原 PDF 不保存。

---

## 4.2 Statement Line

保存 PDF 解析后的标准化流水，不保存 PDF 本体。

```text
id
batch_id

transaction_date
posted_date

description

amount
currency

line_type
match_status

matched_transaction_id nullable
confidence
```

`line_type`：

```text
expense
income
transfer
refund
fee
unknown
```

`match_status`：

```text
unmatched
matched
new_candidate
ambiguous
ignored
```

> 注：`line_type` 的 `income` 仅存在于 Statement 流水解析层；入账生成正式 Transaction 时统一转换为 `cash_income`。

---

## 4.3 Reconciliation Candidate

解析期间的候选修改，不立即影响正式账本。

```text
id
batch_id
candidate_type
payload
status
reason
```

`candidate_type`：

```text
match
create_transaction
create_transfer
refund
adjustment
snapshot
investment_pnl
recognize_installment
```

Statement 必须：

```text
parse
→ candidates
→ reconcile
→ preview
→ atomic commit
```

任何需人工确认项存在时：

```text
整个 Batch = needs_review
```

不得部分修改正式账本。

---

# 5. Matching Rules

Statement 与已有交易匹配默认：

1. 必须属于同一 Account。
2. 金额和币种优先精确匹配。
3. 日期默认允许 ±5 天。
4. Merchant / Description 使用模糊匹配。
5. 唯一明确候选可自动匹配。
6. 多候选必须人工确认。
7. Statement 明确漏记交易可自动生成 candidate。
8. 类型不明确则进入待确认。

外币信用卡：

```text
优先匹配原币金额
↓
若只有结算币金额
↓
日期 + 商户 + settlement amount 辅助匹配
```

---

# 6. Internal Transfer

内部转账：

```text
一笔 Transaction
transaction_type = transfer
```

同币种：

```text
from_account
to_account
amount
currency
```

跨币种：

```text
from_amount
from_currency

to_amount
to_currency

effective_fx_rate
```

手续费：

```text
独立 fee Transaction
```

同一 transfer 同时出现在两个 Statement 时：

```text
合并为同一 transfer
```

不得产生两笔收入/支出。

Statement 只有：

```text
“转账支出 5000”
```

且无法确定目标账户时：

```text
创建 candidate
→ 搜索家庭账户对应流入
→ 唯一候选自动匹配
→ 否则 needs_review
```

禁止 AI 猜测账户。

---

# 7. Foreign Currency

永久保存：

```text
original_amount
original_currency
```

当前资产：

```text
使用 current / T-1 FX
```

未出账信用卡消费：

```text
使用 current / T-1 FX 作为临时报表值
```

Statement 出账后：

```text
消费历史折算值冻结
```

后续信用卡还款：

```text
只是 asset → liability transfer
```

实际还款汇率不得重新修改历史消费金额。

---

# 8. Installment

例如：

```text
12,000 CNY / 12 months
```

分期计划建立流程：
- **统一入口**：Expense Shortcut 仍为统一消费入口，通过 AI 结构化提取识别 `payment_mode`（`one_off` / `installment`）。
- **字段提取**：对于分期消费，提取 `total_amount`、`currency`、`total_periods`、`merchant`、`from_account`。
- **初次录入**：创建 `installment_plans` 与各期 `installment_periods` 计划排期表，返回 `installment_plan_id` 与提示摘要（如“12,000 CNY 12期分期计划已建立”）。
- **零提前记账**：不得在购买当天生成 12 笔正式未来 Transaction，不得立即确认消费支出，不得将全部本金扣减当前账户余额。
- **按期确认**：首期及后续各期费用在信用卡 Statement 首次出账时，通过 `recognize_installment` 候选在对账提交时确认当期实际 `expense` 流水并计入当期负债与支出。
- **尾期调整**：最后一期自动承担 rounding remainder。

展示口径：

```text
本期应还 (statement_balance / remaining_due)
剩余未出账分期 (unbilled installment schedules)
current_outstanding (已出账未还 + 未出账已消费)
```

Product v1 不支持提前还清。

---

# 9. Opening Balance

系统使用统一：

```text
ledger_start_date
```

之前历史不迁移。

首次账户状态来自：

```text
Statement
Snapshot
Dashboard manual input
```

生成：

```text
opening_balance
```

Opening Balance：

- 不属于收入；
- 不属于消费；
- 不属于投资收益。

---

# 10. Reconciliation Threshold

普通账户：

```text
abs(residual in CNY) <= 200
```

可自动：

```text
reconciliation_adjustment
```

该 adjustment：

- 修正余额；
- 不进入消费统计。

超过：

```text
200 CNY
```

必须：

```text
needs_review
```

Investment Account：

```text
禁止套用 ±200 自动 adjustment
```

投资差额必须区分：

```text
资金流入/流出
investment_pnl
```

---

# 11. Workflow Confirmation & Review Rules

系统严格区分 **Ingestion 阶段的确认（`needs_confirmation`）** 与 **Reconciliation 阶段的审核（`needs_review`）**：

### 11.1 Ingestion / Shortcut 阶段 MUST `needs_confirmation`

发生在客户端提交消费截图或草稿处理期间：

```text
找不到唯一扣款账户 / 多个账户候选
金额、币种置信度不足或存在确定性校验冲突
分类置信度不足（若需人工确认分类）
用户在正式提交前发起自然语言修正
```

草稿处于 `needs_confirmation` 期间，**绝不生成正式 Transaction，绝不修改账户余额**。

### 11.2 Reconciliation / 对账阶段 MUST `needs_review`

发生在 Statement 对账批次、Snapshot 对账或投资对账计算期间：

```text
Statement 与已有交易存在多候选匹配 / 候选冲突
普通账户对账 residual > 200 CNY
对账流水类型歧义（无法明确区分 refund / cash_income / transfer）
投资账户对账存在未明确的资金进出（capital movement）
内部转账对账找不到唯一对方账户
Statement 权威数据与历史已确认记录冲突
```

批次处于 `needs_review` 期间，**绝不部分修改正式账本，等待人工裁决后原子提交**。

### 11.3 历史已确认交易变更（Correction）

修改 `verification_status = statement_confirmed` 的历史交易：
- 严禁直接隐式修改或重用 ingestion requests；
- 必须通过专用的变更预览（correction preview）与用户显式提交（correction commit）流程；
- 原子重算并补偿 `account_state` 投影，记录完整的变更前/后不可变审计事件。

### 11.4 自动处理场景

以下情况 MAY 自动提交入账：

```text
普通消费 Shortcut：唯一账户、金额币种明确、分类明确、校验通过
Statement 对账：唯一高置信匹配、明确漏记普通消费、普通账户 residual <= 200 CNY
投资对账：资金进出完全明确且无歧义
```

交易金额大小本身不触发强制确认。

---

# 12. Entry Points

## Expense Shortcut

高频入口。

```text
Screenshot
→ Expense-only AI extraction
→ validation
→ committed / needs_confirmation
```

Shortcut 不判断业务类型。

必须支持：

```text
client-generated idempotency_key
device token
captured_at
client_version
```

---

## Transfer Entry

低频入口。

优先来源：

```text
Statement
Dashboard manual / dedicated capture
```

如未来使用 Shortcut：

```text
必须为独立 Transfer Shortcut
```

不得与 Expense Shortcut 混合。

---

## Account Snapshot Entry

来源：

```text
Dashboard
Dedicated Snapshot Shortcut
Statement
```

用于：

```text
cash balance
savings balance
investment valuation
```

---

# 13. Core Flow A — Expense

```text
Expense Shortcut
↓
Generate idempotency_key
↓
Screenshot + note
↓
POST expense request
↓
AI extracts expense fields only
↓
Deterministic validation
↓
┌─ High confidence + valid
│      ↓
│   Create Transaction
│      ↓
│   Update calculated account state
│      ↓
│   committed
│
└─ Ambiguous
       ↓
   needs_confirmation
       ↓
   User confirm / edit / reject
       ↓
   Commit only after approval
```

Requirements:

- retry with same idempotency key must be no-op;
- failed response must not create duplicate transaction;
- low-confidence item must not affect balance before approval.

---

# 14. Core Flow B — Reconciliation

```text
Upload Statement
or
Submit Snapshot
or
Manual Balance
↓
Create Reconciliation Batch
↓
Parse authoritative observation
↓
Match existing Transactions
↓
Generate missing transaction candidates
↓
Generate transfer/refund candidates
↓
Calculate ledger state
↓
Compare authoritative state
↓
Residual check
↓
┌─ No ambiguity
│   + residual within rule
│        ↓
│    Atomic commit
│
└─ Any ambiguity
         ↓
     needs_review
         ↓
     User resolves
         ↓
     Recalculate
         ↓
     Atomic commit
```

Statement PDF：

```text
parse success → delete immediately
parse failure → maximum retention 24h
password → memory only
```

重复上传同一 Statement：

```text
重新执行 reconciliation
```

但已有正式流水必须重新匹配，不得重复补录。

---

# 15. Core Flow C — Investment

```text
Investment Snapshot / Statement
↓
Read total asset value
↓
Find previous investment snapshot
↓
Collect known contributions / withdrawals
↓
Calculate:

P&L =
Ending Value
- Beginning Value
- Net Contributions
↓
┌─ Contributions known
│       ↓
│   Save snapshot
│   Save investment P&L
│
└─ Unknown capital movement
        ↓
    needs_review
        ↓
    User supplies transfer / capital movement
        ↓
    Recalculate P&L
```

Investment P&L：

```text
影响净资产
进入投资分析
不进入家庭收入
```

---

# 16. Dashboard Reporting

核心指标：

```text
Total Assets
Total Liabilities
Net Worth

Cash Income
Expense
Net Cash Flow

Investment P&L

Credit Card:
- statement balance
- remaining due
- unbilled balance
- current outstanding
```

同时展示 Account 数据新鲜度：

```text
last_authoritative_snapshot_at
```

不要求所有账户每月完成 Statement。

---

# 17. Source of Truth Rules

优先级：

```text
Statement / authoritative Snapshot
        >
confirmed Transaction
        >
AI estimated value
```

Statement / Snapshot 可以校准账户状态。

任何 reconciliation 都不得静默改写：

```text
原始 transaction evidence
original currency amount
historical audit trail
```

---

# 18. Implementation Constraint

后续 Agent 开发前必须遵守：

1. **先设计新 schema，再改业务代码。**
2. 不继续扩展现有 `accounts + transactions` 两表模型来勉强承载全部语义。
3. 不为兼容旧数据牺牲新模型；旧生产数据无需迁移。
4. Backend 是唯一业务规则层。
5. Dashboard 不应继续复制核心 accounting logic。
6. 所有余额修改必须事务安全、并发安全。
7. 所有正式写入必须支持审计和可追溯。
8. 新功能必须有对应 deterministic tests。
9. 未在本文明确的工程细节，由实现 Agent 给出推荐方案，不继续向产品用户逐项询问。

---

## Next Step

目标架构设计文档已全部制定并冻结于 `docs/architecture/`：
1. [`PHYSICAL_SCHEMA.md`](./docs/architecture/PHYSICAL_SCHEMA.md) — 目标 PostgreSQL 持久化规范
2. [`API_CONTRACT.md`](./docs/architecture/API_CONTRACT.md) — 目标 REST API 契约
3. [`RECONCILIATION_ENGINE.md`](./docs/architecture/RECONCILIATION_ENGINE.md) — 对账与匹配引擎规范
4. [`IMPLEMENTATION_PLAN.md`](./docs/architecture/IMPLEMENTATION_PLAN.md) — 实施路径与分阶段计划
5. [`TEST_PLAN.md`](./docs/architecture/TEST_PLAN.md) — 验证与测试规范

后续开发请严格按照 [`IMPLEMENTATION_PLAN.md`](./docs/architecture/IMPLEMENTATION_PLAN.md) 从 **Implementation Phase 0** 及 **Implementation Phase 1** 开始分阶段推进。