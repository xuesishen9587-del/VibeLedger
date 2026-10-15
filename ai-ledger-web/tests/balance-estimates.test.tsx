import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { BalanceEditor } from "../src/pages/Wealth";
import type { Account, BalanceEstimateReport } from "../src/types";

const account: Account = {
  id: "bank",
  household_id: "family",
  name: "日常账户",
  account_type: "savings",
  currency: "CNY",
  balance_scope: "全部余额",
  risk_level: "low",
  status: "active",
  row_version: 3,
  statement_import_enabled: false,
  latest_snapshot: null,
  opened_on: "2026-01-01",
  owner_user_id: null,
};
function report(amount = "85.00"): BalanceEstimateReport {
  return {
    as_of: new Date().toISOString(),
    items: [
      {
        account_id: account.id,
        currency: "CNY",
        status: "ready",
        snapshot_id: "source-snapshot",
        baseline_date: "2026-02-02",
        baseline_balance: "100.00",
        recorded_change: "-15.00",
        estimated_balance: amount,
        transaction_count: 2,
        same_day_count: 0,
        currency_mismatch_count: 0,
      },
    ],
  };
}
function setup(
  options: {
    account?: Account;
    load?: () => Promise<BalanceEstimateReport>;
  } = {},
) {
  const selected = options.account ?? account;
  const api = new Api("fixture-user", async () => "fixture-token");
  const request = vi
    .spyOn(api, "request")
    .mockImplementation(
      async () =>
        (await (options.load?.() ?? Promise.resolve(report()))) as never,
    );
  const command = vi.spyOn(api, "command").mockResolvedValue({});
  const onClose = vi.fn();
  const view = render(
    <AppContext.Provider
      value={{
        api,
        accounts: [selected],
        categories: [],
        timezone: "Asia/Singapore",
        revision: 0,
        refresh: vi.fn(),
        notify: vi.fn(),
      }}
    >
      <BalanceEditor account={selected} onClose={onClose} />
    </AppContext.Provider>,
  );
  return { user: userEvent.setup(), request, command, onClose, ...view };
}

it.each([
  ["savings", "CNY", "-15.00", "-15.00", false],
  ["cash", "CNY", "-15.00", "-15.00", false],
  ["credit", "CNY", "-105.00", "105.00", false],
  ["credit", "CNY", "15.00", "15.00", true],
  ["credit", "JPY", "0", "0", false],
] as const)(
  "preserves %s %s signed estimate %s on explicit use",
  async (kind, currency, signed, input, surplus) => {
    const { user, command, request } = setup({
      account: { ...account, account_type: kind, currency },
      load: async () => ({
        ...report(signed),
        items: report(signed).items.map((r) => ({ ...r, currency })),
      }),
    });
    const field = screen.getByRole("textbox", {
      name: kind === "credit" ? "当前总欠款（正数）" : "当前余额（可填零）",
    });
    await screen.findByRole("button", { name: "使用推荐值" });
    expect(field).toHaveValue("");
    expect(command).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "使用推荐值" }));
    expect(field).toHaveValue(input);
    if (kind === "credit")
      expect(
        screen.getByRole("checkbox", { name: "这是多存的溢缴款" }),
      ).toHaveProperty("checked", surplus);
    expect(request).toHaveBeenCalledTimes(1);
    expect(command).not.toHaveBeenCalled();
  },
);

it("saves only after manual submission, retaining the recommendation's baseline instead of cached account metadata", async () => {
  const { user, command, onClose } = setup();
  const field = screen.getByRole("textbox", { name: "当前余额（可填零）" });
  await user.click(await screen.findByRole("button", { name: "使用推荐值" }));
  await user.clear(field);
  await user.type(field, "84.50");
  expect(command).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "保存本次余额" }));
  await waitFor(() => expect(command).toHaveBeenCalledTimes(1));
  expect(command).toHaveBeenCalledWith(
    "/balance-updates",
    "POST",
    {
      observations: [
        expect.objectContaining({
          account_id: "bank",
          balance: "84.50",
          expected_account_version: 3,
          expected_latest_snapshot_id: "source-snapshot",
          time_basis: "explicit",
        }),
      ],
    },
    "更新账户余额",
  );
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("clears an applied current recommendation when changing the observation time", async () => {
  const { user, command } = setup();
  await user.click(await screen.findByRole("button", { name: "使用推荐值" }));
  const time = screen.getByLabelText("记录时间（设备当地时间）");
  fireEvent.change(time, { target: { value: "2026-01-01T12:00" } });
  expect(
    screen.getByRole("textbox", { name: "当前余额（可填零）" }),
  ).toHaveValue("");
  expect(
    screen.getByText("记录时间已修改，请按该时刻实际余额手工填写。"),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "使用推荐值" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "保存本次余额" }));
  expect(command).not.toHaveBeenCalled();
});

it("ignores an older read after closing and reopening the balance editor", async () => {
  let finishOldRead!: (data: BalanceEstimateReport) => void;
  const first = setup({
    load: () =>
      new Promise((done) => {
        finishOldRead = done;
      }),
  });
  first.unmount();
  const second = setup({ load: async () => report("72.00") });
  await screen.findByRole("button", { name: "使用推荐值" });
  await act(async () => finishOldRead(report("999.00")));
  await second.user.click(screen.getByRole("button", { name: "使用推荐值" }));
  expect(
    screen.getByRole("textbox", { name: "当前余额（可填零）" }),
  ).toHaveValue("72.00");
  expect(first.request).toHaveBeenCalledTimes(1);
  expect(second.request).toHaveBeenCalledTimes(1);
  expect(first.command).not.toHaveBeenCalled();
  expect(second.command).not.toHaveBeenCalled();
});

it("keeps the editor open and does not retry saving when its source balance changed", async () => {
  const { user, command, onClose } = setup();
  command.mockRejectedValue(new Error("余额已变化，请重新读取"));
  await user.click(await screen.findByRole("button", { name: "使用推荐值" }));
  await user.click(screen.getByRole("button", { name: "保存本次余额" }));
  await screen.findByText("余额已变化，请重新读取");
  expect(command).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
  expect(
    screen.getByRole("textbox", { name: "当前余额（可填零）" }),
  ).toHaveValue("85.00");
});

it("does not overwrite manual input when a delayed read completes or the user changes the date", async () => {
  let resolve!: (data: BalanceEstimateReport) => void;
  const { user, command } = setup({
    load: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  const field = screen.getByRole("textbox", { name: "当前余额（可填零）" });
  await user.type(field, "72.00");
  await act(async () => resolve(report()));
  expect(field).toHaveValue("72.00");
  fireEvent.change(screen.getByLabelText("记录时间（设备当地时间）"), {
    target: { value: "2026-01-01T12:00" },
  });
  expect(field).toHaveValue("72.00");
  expect(command).not.toHaveBeenCalled();
});

it("allows manual balance entry if the read-only suggestion API fails", async () => {
  const { user, command } = setup({
    load: async () => {
      throw new Error("推荐暂不可用");
    },
  });
  await screen.findByText("推荐暂不可用");
  await user.type(
    screen.getByRole("textbox", { name: "当前余额（可填零）" }),
    "70.00",
  );
  await user.click(screen.getByRole("button", { name: "保存本次余额" }));
  await waitFor(() => expect(command).toHaveBeenCalledTimes(1));
  expect(command.mock.calls[0][2]).toEqual({
    observations: [
      expect.objectContaining({
        balance: "70.00",
        expected_latest_snapshot_id: null,
      }),
    ],
  });
});

it.each([
  "no_snapshot",
  "unsupported_account",
  "ambiguous_same_day",
  "currency_mismatch",
] as const)(
  "never fills or saves an unavailable %s estimate",
  async (status) => {
    const { command } = setup({
      load: async () => ({
        ...report(),
        items: [
          {
            ...report().items[0],
            status,
            estimated_balance: null,
            recorded_change: null,
          },
        ],
      }),
    });
    await waitFor(() =>
      expect(
        screen.queryByText("正在计算可参考的余额…"),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "使用推荐值" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "当前余额（可填零）" }),
    ).toHaveValue("");
    expect(command).not.toHaveBeenCalled();
  },
);
