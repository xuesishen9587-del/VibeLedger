# VibeLedger Target Domain Model

> Status: **Approved target design**
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

### OUT OF SCOPE — Phase 1

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
occurred_at
posted_at              nullable

from_account_id        nullable
to_account_id          nullable

original_amount
original_currency

from_amount            nullable
from_currency          nullable
to_amount              nullable
to_currency            nullable
effective_fx_rate      nullable

category_id            nullable
merchant               nullable
remarks                nullable

source
status
confidence             nullable

idempotency_key        nullable
statement_batch_id     nullable

created_at
updated_at
deleted_at             nullable
```

### Date Rule

普通消费：

```text
occurred_at = 实际交易日期
posted_at   = Statement 入账日期
```

报表使用 `occurred_at`。

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

第一阶段投资 Statement 只解析：

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
matched
new_candidate
ambiguous
ignored
```

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

规则：

```text
首次进入信用卡 Statement 的月份
→ 确认第 1 期 1,000

之后每月
→ 确认 1,000
```

展示：

```text
本期应还
剩余未出账分期
current_outstanding
```

不得在购买当天生成 12 笔正式未来 Transaction 并立即修改全部余额。

最后一期承担 rounding remainder。

Phase 1 不支持提前还清。

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

# 11. Confirmation Rules

以下情况 MUST `needs_confirmation`：

```text
找不到账户
多个账户候选
跨币种交易缺一侧金额
Statement 多候选匹配
Statement residual > 200 CNY
无法区分 refund / income / transfer
修改 Statement-confirmed 历史交易
```

以下情况 MAY 自动处理：

```text
普通消费
唯一账户
金额币种明确
类别明确
Statement 唯一高置信匹配
Statement 明确漏记普通消费
普通账户 residual <= 200 CNY
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
    needs_confirmation
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

下一阶段直接进行：

```text
1. Physical Database Schema
2. API Contract
3. Reconciliation Matching Engine Design
4. Implementation Phases
5. Tests
```

在以上设计完成前，不开始大规模重构。