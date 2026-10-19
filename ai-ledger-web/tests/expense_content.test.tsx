import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { SpendingPage, TransactionEditor } from "../src/pages/Spending";
import { ExpenseLabel } from "../src/components/ExpenseLabel";
import type { Transaction } from "../src/types";

const row: Transaction = {
  id: "tx",
  occurred_on: "2026-10-19",
  original_amount: "34.60",
  original_currency: "CNY",
  transaction_type: "expense",
  category_id: "food",
  account_id: null,
  merchant: "美团",
  remarks: "周一午餐",
  expense_content: "西少爷（新辰里店）",
  expense_content_source: "screenshot",
  row_version: 3,
  category_uncertain: false,
  account_review_acknowledged: false,
  status: "committed",
};
function wrap(children: React.ReactNode) {
  const api = new Api("content-user", async () => "fixture-token");
  const request = vi
    .spyOn(api, "request")
    .mockImplementation(
      async (path) =>
        (path.startsWith("/transactions")
          ? { items: [row], next_cursor: null }
          : null) as never,
    );
  const command = vi.spyOn(api, "command").mockResolvedValue(row);
  const view = render(
    <AppContext.Provider
      value={{
        api,
        accounts: [],
        categories: [
          {
            id: "food",
            name: "餐饮",
            category_type: "expense",
            status: "active",
            row_version: 0,
            is_fallback: false,
          },
        ],
        timezone: "Asia/Shanghai",
        revision: 0,
        refresh: vi.fn(),
        notify: vi.fn(),
      }}
    >
      {children}
    </AppContext.Provider>,
  );
  return { ...view, request, command };
}

it("shows content as primary text and merchant as secondary, including legacy records", () => {
  const { container, rerender } = render(<ExpenseLabel row={row} />);
  expect(container.querySelector("strong")).toHaveTextContent(
    "西少爷（新辰里店）",
  );
  expect(container.querySelector("small")).toHaveTextContent("美团");
  rerender(<ExpenseLabel row={{ merchant: "京东平台商户" }} />);
  expect(container.querySelector("strong")).toHaveTextContent("用途待补充");
  expect(container.querySelector("small")).toHaveTextContent("京东平台商户");
});

it("searches using q and resets pagination for the optional missing-content filter", async () => {
  const { request } = wrap(<SpendingPage />);
  await screen.findByRole("table");
  fireEvent.change(screen.getByLabelText("搜索消费内容或商户"), {
    target: { value: "西少爷" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "用途待补充" }));
  await waitFor(() =>
    expect(
      request.mock.calls.some(([path]) => {
        if (!path.startsWith("/transactions")) return false;
        const params = new URL(path, "http://test").searchParams;
        return (
          params.get("q") === "西少爷" &&
          params.get("missing_content") === "true" &&
          !params.has("merchant") &&
          !params.has("cursor")
        );
      }),
    ).toBe(true),
  );
});

it("edits and clears content without changing merchant or remarks, preserving source when untouched", async () => {
  const { command } = wrap(
    <TransactionEditor record={row} onClose={vi.fn()} />,
  );
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("消费内容"), {
    target: { value: "" },
  });
  fireEvent.submit(dialog.querySelector("form")!);
  await waitFor(() => expect(command).toHaveBeenCalled());
  expect(command.mock.calls[0][2]).toMatchObject({
    expected_version: 3,
    expense_content: null,
    merchant: "美团",
    remarks: "周一午餐",
  });
  expect(command.mock.calls[0][2]).not.toHaveProperty("expense_content_source");
});

it("does not assert manual provenance when only another field changes", async () => {
  const { command } = wrap(
    <TransactionEditor record={row} onClose={vi.fn()} />,
  );
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/备注/), {
    target: { value: "加班午餐" },
  });
  fireEvent.submit(dialog.querySelector("form")!);
  await waitFor(() => expect(command).toHaveBeenCalled());
  expect(command.mock.calls[0][2]).not.toHaveProperty("expense_content");
});
