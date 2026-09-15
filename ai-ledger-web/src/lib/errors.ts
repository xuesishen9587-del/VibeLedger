const messages: Record<string, string> = {
  UNAUTHORIZED: "登录已过期，请重新登录后继续。",
  FORBIDDEN: "这个账号暂时无法访问家庭账本。",
  BACKEND_UNAVAILABLE: "暂时连不上账本，请稍后重试。",
  NETWORK: "网络中断，保存结果还不确定。请先查询或重试原操作。",
  INVALID_REQUEST: "请检查填写的内容后再试。",
  INVALID_AMOUNT: "请填写大于零的金额，并检查小数位。",
  INVALID_CURRENCY: "请选择支持的币种。",
  INVALID_DATE: "请检查日期，不能晚于今天。",
  DRAFT_CHANGED: "这份草稿已更新，请重新载入后核对。",
  ROW_VERSION_CONFLICT: "这条记录刚刚被修改了，请重新载入后核对。",
  BALANCE_CHANGED: "这个账户有了新的余额，请重新载入后核对。",
  SNAPSHOT_TIME_CONFLICT: "同一时间已有余额，请选择复用或修改已有记录。",
  STATEMENT_ACCOUNT_MISMATCH: "请确认账单属于所选账户。",
  PARTIAL_STATEMENT: "部分页面可能没有识别完整，请核对账单。",
  STATEMENT_PERIOD_REQUIRED: "请补充正确的账单起止日期。",
  STATEMENT_LINE_UNCERTAIN: "请核对这笔的金额、日期和性质。",
  STATEMENT_DATE_OUTSIDE_PERIOD: "交易日期不在账单期间内，请核对。",
  POSSIBLE_DUPLICATE: "可能已经记过，请核对已有记录。",
  REFUND_NOTE_REQUIRED: "请补充这笔退款的说明。",
  INVALID_CATEGORY: "请选择可用的分类。",
  SKIP_REASON_REQUIRED: "请说明为什么跳过。",
  STATEMENT_PASSWORD_REQUIRED: "这份 PDF 有密码，请填写后重试。",
  STATEMENT_PASSWORD_INVALID: "PDF 密码不正确，请重新填写。",
  STATEMENT_LIMIT_EXCEEDED: "请使用 20 MiB 以内、最多 50 页的 PDF。",
  STATEMENT_PARSE_FAILED: "这次没能读懂账单，请稍后重试。",
  STATEMENT_ALREADY_IMPORTED: "这份账单已经上传过，请查看已有结果。",
  STATEMENT_ALREADY_UPLOADED: "这份账单已经上传过，请查看已有结果。",
  STATEMENT_IMPORT_DISABLED: "请在账户设置中开启账单导入。",
  BALANCE_EVIDENCE_UNCERTAIN: "请核对余额金额、日期和包含范围。",
  ACCOUNT_SCOPE_MISMATCH: "请检查这份余额是否包含了其他账户。",
  IMPORT_CHANGED: "相关记录已发生变化，请重新载入后核对。",
  INVESTMENT_PAIR_CHANGED: "这段时间的余额已改变，请重新载入。",
  INVALID_BALANCE: "请检查余额金额和小数位，零余额也可以保存。",
  EMPTY_IMPORT: "请至少选择一笔交易或一条余额。",
  REQUEST_NOT_FOUND: "暂时还查不到结果，请稍后再查，或取消本次上传。",
  STORAGE_UNAVAILABLE: "浏览器无法保存登录或重试信息，请允许本站使用本地存储。",
};
export function errorMessage(code: string) {
  return messages[code] || "这项操作尚未完成，请检查相关内容或稍后重试。";
}
export class ApiError extends Error {
  constructor(
    public code: string,
    public status = 0,
    public details: Record<string, unknown> = {},
  ) {
    super(errorMessage(code));
  }
}
