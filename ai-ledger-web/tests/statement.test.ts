import { describe, it, expect } from "vitest";
import {
  statementEdit,
  statementSummary,
  lineWarnings,
  reviewReason,
  statementGlobalWarnings,
} from "../src/lib/statement";
import { sumDecimal, money } from "../src/lib/format";
import type { StatementDraft, StatementLine } from "../src/types";
export const line = (
  i: number,
  extra: Partial<StatementLine> = {},
): StatementLine => ({
  row_id: "row-" + i,
  row_no: i,
  action: "create",
  occurred_on: "2026-09-01",
  original_amount: "12.30",
  original_currency: "CNY",
  transaction_type: "expense",
  category_id: "other",
  category_uncertain: false,
  merchant: "Cafe " + i,
  remarks: null,
  requires_review: false,
  duplicate_ids: [],
  ...extra,
});
export const draft = (lines: StatementLine[]): StatementDraft => ({
  account_id: "account",
  lines,
  balance: null,
  period_start: "2026-09-01",
  period_end: "2026-09-30",
  partial: false,
  identity_ok: true,
  acknowledge_partial: false,
  confirm_account_identity: false,
  warnings: [],
});
describe("statement decisions", () => {
  it("keeps global and orphaned blockers distinct from actionable rows", () => {
    const d = draft([line(1), line(2, { action: "skip" })]);
    d.warnings = [
      { code: "STATEMENT_PROVIDER_CONFLICT", row_id: "row-1" },
      { code: "IMPORT_CHANGED", row_id: "row-2" },
      { code: "BALANCE_CHANGED" },
      { code: "IMPORT_CHANGED", row_id: "missing-row" },
    ];
    expect(statementSummary(d, new Set(["row-1"]))).toMatchObject({
      review: 2,
      globalReview: 2,
    });
    expect(statementGlobalWarnings(d)).toEqual(d.warnings.slice(2));
  });
  it("acknowledgement cannot hide invalid facts or a missing skip reason", () => {
    const d = draft([line(1), line(2, { action: "skip", reason: "" })]);
    d.warnings = [
      { row_id: "row-1", code: "INVALID_TRANSACTION_TYPE" },
      { row_id: "row-1", code: "INVALID_CATEGORY" },
      { row_id: "row-2", code: "SKIP_REASON_REQUIRED" },
    ];
    const confirmed = new Set(["row-1", "row-2"]);
    expect(statementSummary(d, confirmed).review).toBe(2);
    expect(lineWarnings(d.lines[0], d.warnings, confirmed)).toHaveLength(2);
    expect(reviewReason(d.warnings[0])).toContain("消费或退款");
    expect(
      reviewReason({
        code: "NEW_VALIDATION",
        message: "Select a valid target.",
      }),
    ).toContain("Select a valid target.");
  });
  it("100 normal rows need no per-row confirmation or category assertion", () => {
    const d = draft(Array.from({ length: 100 }, (_, i) => line(i)));
    const edit = statementEdit(d, d, 7, new Set(), new Set());
    expect(edit.lines).toHaveLength(100);
    expect(edit.lines).toEqual(
      d.lines.map((r) => ({ row_id: r.row_id, action: "create" })),
    );
    expect(statementSummary(d).review).toBe(0);
    expect(statementSummary(d).totals[0].amount).toBe("1230.00");
  });
  it("only an explicitly inspected row is confirmed; unseen uncertain rows survive", () => {
    const d = draft([
      line(1, { requires_review: true }),
      line(2, { requires_review: true }),
    ]);
    const rows = statementEdit(d, d, 3, new Set(["row-1"]), new Set())
      .lines as Record<string, unknown>[];
    expect(rows[0].confirm_facts).toBe(true);
    expect(rows[1]).not.toHaveProperty("confirm_facts");
    expect(statementSummary(d, new Set(["row-1"])).review).toBe(1);
  });
  it("unchanged fallback classification is not silently confirmed", () => {
    const d = draft([line(1, { category_uncertain: true })]);
    expect(
      (
        statementEdit(d, d, 1, new Set(), new Set()).lines as Record<
          string,
          unknown
        >[]
      )[0],
    ).not.toHaveProperty("category_id");
    expect(
      (
        statementEdit(d, d, 1, new Set(), new Set(["row-1"])).lines as Record<
          string,
          unknown
        >[]
      )[0].category_id,
    ).toBe("other");
  });
  it("refunds reduce spending, repayment and existing links do not increase it, currencies stay separate", () => {
    const d = draft([
      line(1, { original_amount: "100.00" }),
      line(2, { transaction_type: "refund", original_amount: "20.00" }),
      line(3, { action: "skip", kind: "repayment", original_amount: "500" }),
      line(4, { action: "link_existing" }),
      line(5, { original_currency: "SGD", original_amount: "9.90" }),
    ]);
    expect(statementSummary(d)).toMatchObject({
      purchases: 2,
      refunds: 1,
      repayments: 1,
      skipped: 1,
      linked: 1,
      totals: [
        { currency: "CNY", amount: "80.00" },
        { currency: "SGD", amount: "9.90" },
      ],
    });
  });
  it("all row identities and explicit skips survive filtering", () => {
    const base = draft([line(1), line(2), line(3)]);
    const changed = structuredClone(base);
    changed.lines[1].action = "skip";
    changed.lines[1].reason = "不是支出";
    expect(statementEdit(base, changed, 9, new Set(), new Set()).lines).toEqual(
      [
        { row_id: "row-1", action: "create" },
        { row_id: "row-2", action: "skip", reason: "不是支出" },
        { row_id: "row-3", action: "create" },
      ],
    );
  });
  it("exact decimal addition and missing versus zero", () => {
    expect(sumDecimal(["0.10", "0.20", "-0.30"])).toBe("0.00");
    expect(sumDecimal(["invalid"])).toBeNull();
    expect(money(null)).toBe("—");
    expect(money("0")).not.toBe("—");
  });
});
