export type ExpenseDescription = {
  expense_content?: string | null;
  expense_content_source?: string | null;
  merchant?: string | null;
};

export function expenseTitle(row: ExpenseDescription) {
  return row.expense_content?.trim() || "用途待补充";
}

export function ExpenseLabel({ row }: { row: ExpenseDescription }) {
  return (
    <>
      <strong>{expenseTitle(row)}</strong>
      <small>{row.merchant || "商户未填写"}</small>
    </>
  );
}

export function contentSource(row: ExpenseDescription) {
  const labels: Record<string, string> = {
    manual: "手动填写",
    screenshot: "截图识别",
    user_note: "用户补充",
    statement: "账单识别",
    schedule: "周期计划",
  };
  return row.expense_content_source
    ? labels[row.expense_content_source] || ""
    : "";
}
