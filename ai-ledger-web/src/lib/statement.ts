import type { StatementDraft, StatementLine, Warning } from "../types";
import { sumDecimal } from "./format";
import { errorMessage } from "./errors";
const editable = [
  "action",
  "occurred_on",
  "original_amount",
  "original_currency",
  "merchant",
  "transaction_type",
  "remarks",
  "transaction_id",
  "expected_transaction_version",
  "schedule_id",
  "period_no",
  "expected_schedule_version",
  "reason",
] as const;
export function lineWarnings(
  row: StatementLine,
  warnings: Warning[],
  confirmed: Set<string>,
) {
  return warnings.filter(
    (w) =>
      w.row_id === row.row_id &&
      !(
        (w.code === "STATEMENT_LINE_UNCERTAIN" ||
          w.code === "POSSIBLE_DUPLICATE") &&
        confirmed.has(row.row_id)
      ),
  );
}
export function reviewReason(w: Warning) {
  const reasons: Record<string, string> = {
    INVALID_CATEGORY: "请选择可用的支出分类。",
    INVALID_TRANSACTION_TYPE: "请选择交易性质：消费或退款。",
    IMPORT_CHANGED:
      "请选择有效的已有记录进行关联，或跳过；若已选记录发生变化，请重新选择。",
    INVALID_STATEMENT_LINK: "请选择一笔有效的消费或退款记录。",
    INVALID_SCHEDULE: "请选择有效的月度计划和期数，且交易性质必须为消费。",
    SCHEDULE_OCCURRENCE_CONFLICT:
      "金额、币种和日期必须与所选计划期数一致；请修改或重新选择计划。",
    STATEMENT_PROVIDER_CONFLICT:
      "这些行使用了相同的银行交易编号，但金额、日期、币种、性质或关联记录不一致。请核对并修正，关联同一笔已有记录，或跳过错误行；仅点击已核对不能解除冲突。",
  };
  return reasons[w.code] || errorMessage(w.code, w.message);
}
export function statementGlobalWarnings(draft: StatementDraft) {
  const rowIds = new Set(draft.lines.map((r) => r.row_id));
  return draft.warnings.filter((w) => !w.row_id || !rowIds.has(w.row_id));
}
export function globalRecovery(w: Warning) {
  if (w.code === "STATEMENT_PERIOD_REQUIRED")
    return "请修改页面顶部的账单开始和结束日期，再重新检查。";
  if (w.code === "STATEMENT_ACCOUNT_MISMATCH")
    return "请核对所选账户并勾选账户确认；余额账户也必须一致。";
  if (w.code === "PARTIAL_STATEMENT")
    return "请核对缺页情况并勾选接受本次识别内容，或放弃后上传完整账单。";
  if (/BALANCE|SNAPSHOT/.test(w.code))
    return "请展开下方“同时更新账单余额”，修正或取消本次余额更新，再重新检查。";
  return "请点击“保存修改并重新检查整份账单”获取最新校验结果，当前修改会保留。若问题仍无法解决，可使用下方“放弃账单”后重新上传。";
}
export function statementSummary(
  draft: StatementDraft,
  confirmed = new Set<string>(),
) {
  const rows = draft.lines;
  const include = rows.filter(
    (r) => r.action === "create" || r.action === "use_schedule_period",
  );
  const currencies = [
    ...new Set(
      include.map((r) => r.original_currency).filter((c): c is string => !!c),
    ),
  ];
  return {
    globalReview: statementGlobalWarnings(draft).length,
    purchases: include.filter((r) => r.transaction_type === "expense").length,
    refunds: include.filter((r) => r.transaction_type === "refund").length,
    repayments: rows.filter(
      (r) => r.kind === "repayment" || r.reason === "Not spending: repayment",
    ).length,
    skipped: rows.filter((r) => r.action === "skip").length,
    linked: rows.filter((r) => r.action === "link_existing").length,
    review: rows.filter(
      (r) =>
        (r.action !== "skip" &&
          r.requires_review &&
          !confirmed.has(r.row_id)) ||
        lineWarnings(r, draft.warnings, confirmed).length > 0,
    ).length,
    totals: currencies.map((currency) => ({
      currency,
      amount: sumDecimal(
        include
          .filter((r) => r.original_currency === currency)
          .map(
            (r) =>
              (r.transaction_type === "refund" ? "-" : "") +
              (r.original_amount || "invalid"),
          ),
      ),
    })),
  };
}
export function statementEdit(
  base: StatementDraft,
  draft: StatementDraft,
  version: number,
  confirmed: Set<string>,
  categories: Set<string>,
) {
  const original = new Map(base.lines.map((r) => [r.row_id, r]));
  const lines = draft.lines.map((row) => {
    const before = original.get(row.row_id)!;
    const edit: Record<string, unknown> = {
      row_id: row.row_id,
      action: row.action,
    };
    for (const field of editable)
      if (row[field] !== before[field]) edit[field] = row[field] ?? null;
    if (row.category_id !== before.category_id || categories.has(row.row_id))
      edit.category_id = row.category_id;
    if (confirmed.has(row.row_id)) edit.confirm_facts = true;
    return edit;
  });
  const body: Record<string, unknown> = { expected_version: version, lines };
  for (const field of [
    "period_start",
    "period_end",
    "acknowledge_partial",
    "confirm_account_identity",
  ] as const)
    if (base[field] !== draft[field]) body[field] = draft[field];
  if (
    JSON.stringify(base.balance) !== JSON.stringify(draft.balance) &&
    draft.balance
  ) {
    const b = draft.balance;
    body.balance = {
      row_id: b.row_id,
      selected: b.selected,
      account_id: b.account_id,
      balance: b.balance,
      currency: b.currency,
      as_of: b.as_of,
      time_basis: b.time_basis,
      expected_account_version: b.expected_account_version,
      expected_latest_snapshot_id: b.expected_latest_snapshot_id,
      reuse_snapshot_id: b.reuse_snapshot_id || null,
      exclusion_reason: b.selected ? null : "本次只导入支出",
    };
  }
  return body;
}
