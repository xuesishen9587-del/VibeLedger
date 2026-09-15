import type { StatementDraft, StatementLine, Warning } from "../types";
import { sumDecimal } from "./format";
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
  if (row.action === "skip") return [];
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
    purchases: include.filter((r) => r.transaction_type === "expense").length,
    refunds: include.filter((r) => r.transaction_type === "refund").length,
    repayments: rows.filter(
      (r) => r.kind === "repayment" || r.reason === "Not spending: repayment",
    ).length,
    skipped: rows.filter((r) => r.action === "skip").length,
    linked: rows.filter((r) => r.action === "link_existing").length,
    review: rows.filter(
      (r) =>
        r.action !== "skip" &&
        ((r.requires_review && !confirmed.has(r.row_id)) ||
          lineWarnings(r, draft.warnings, confirmed).length > 0),
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
