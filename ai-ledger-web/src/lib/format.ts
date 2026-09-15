export function money(
  value: string | number | null | undefined,
  currency = "CNY",
) {
  if (
    value === null ||
    value === undefined ||
    value === "" ||
    !Number.isFinite(Number(value))
  )
    return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    maximumFractionDigits: currency === "JPY" ? 0 : 2,
    minimumFractionDigits: currency === "JPY" ? 0 : 2,
  }).format(Number(value));
}
export function day(value?: string | null) {
  return value ? value.slice(0, 10).replaceAll("-", ".") : "尚未记录";
}
export function today(timezone = "Asia/Singapore") {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
export function monthRange(month: string) {
  const [year, m] = month.split("-").map(Number);
  return {
    from: month + "-01",
    to: month + "-" + new Date(Date.UTC(year, m, 0)).getUTCDate(),
  };
}
export const kindLabel: Record<string, string> = {
  expense: "消费",
  refund: "退款",
  cash_income: "收入",
  fee: "费用",
  repayment: "还款",
  transfer: "转账",
  income: "收入",
  opening_balance: "期初余额",
  investment_trade: "投资交易",
  unknown: "待确认",
};
export const accountLabel: Record<string, string> = {
  cash: "日常钱包",
  savings: "银行储蓄",
  credit: "信用卡",
  investment: "投资账户",
};
export const riskLabel: Record<string, string> = {
  very_low: "稳稳当当",
  low: "较稳健",
  medium: "适度波动",
  high: "较高波动",
  unclassified: "未分类",
};
export const currencies = ["CNY", "SGD", "USD", "EUR", "JPY"];
export function sumDecimal(values: string[]) {
  const parsed = values.map((v) => /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(v));
  if (parsed.some((v) => !v)) return null;
  const places = Math.max(2, ...parsed.map((v) => v![3]?.length || 0));
  const total = parsed.reduce(
    (s, v) =>
      s +
      BigInt(
        (v![1] === "-" ? "-" : "") + v![2] + (v![3] || "").padEnd(places, "0"),
      ),
    0n,
  );
  const digits = (total < 0n ? -total : total)
    .toString()
    .padStart(places + 1, "0");
  return (
    (total < 0n ? "-" : "") +
    digits.slice(0, -places) +
    "." +
    digits.slice(-places)
  );
}
