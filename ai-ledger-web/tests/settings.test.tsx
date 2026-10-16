import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { SettingsPage } from "../src/pages/Settings";
import type { Account, HouseholdMember } from "../src/types";

const members: HouseholdMember[] = [
  { user_id: "member-one", display_name: "小林", status: "active" },
  { user_id: "member-two", display_name: "小夏", status: "active" },
];
const account: Account = {
  id: "account-one",
  household_id: "family",
  name: "日常钱包",
  account_type: "cash",
  currency: "CNY",
  balance_scope: "仅钱包现金",
  risk_level: null,
  status: "active",
  row_version: 4,
  statement_import_enabled: false,
  latest_snapshot: null,
  opened_on: "2026-01-01",
  owner_user_id: null,
};

function setup({
  owner = null as string | null,
  family = members,
  failed = false,
} = {}) {
  const api = new Api("fixture-user", async () => "fixture-token");
  const request = vi.spyOn(api, "request").mockImplementation(async (path) => {
    if (path === "/household-members") {
      if (failed) throw new Error("成员暂时无法读取");
      return { items: family, next_cursor: null } as never;
    }
    return { items: [], next_cursor: null } as never;
  });
  const command = vi.spyOn(api, "command").mockResolvedValue({});
  render(
    <AppContext.Provider
      value={{
        api,
        accounts: [{ ...account, owner_user_id: owner }],
        categories: [],
        timezone: "Asia/Singapore",
        revision: 0,
        refresh: vi.fn(),
        notify: vi.fn(),
      }}
    >
      <SettingsPage onLogout={vi.fn()} />
    </AppContext.Provider>,
  );
  return { user: userEvent.setup(), command, request };
}

it("reads real member names and leaves an unassigned account unassigned when editing", async () => {
  const { user, command, request } = setup();
  await user.click(screen.getByRole("button", { name: "设置" }));
  const dialog = within(screen.getByRole("dialog"));
  const owner = await dialog.findByRole("combobox", { name: /^账户归属/ });
  await waitFor(() => expect(owner).toBeEnabled());
  expect(request).toHaveBeenCalledWith("/household-members");
  expect(owner).toHaveValue("");
  expect(dialog.getByRole("option", { name: "小林" })).toBeVisible();
  expect(dialog.getByRole("option", { name: "小夏" })).toBeVisible();
  await user.click(dialog.getByRole("button", { name: "保存" }));
  expect(command).toHaveBeenCalledWith(
    "/accounts/account-one",
    "PATCH",
    expect.objectContaining({ owner_user_id: null, expected_version: 4 }),
    "修改账户",
  );
});

it("adds an account with an explicitly selected member, without changing financial facts", async () => {
  const { user, command } = setup();
  await user.click(screen.getByRole("button", { name: "添加账户" }));
  const dialog = within(screen.getByRole("dialog"));
  await waitFor(() =>
    expect(dialog.getByRole("combobox", { name: /^账户归属/ })).toBeEnabled(),
  );
  expect(dialog.getByRole("combobox", { name: /^账户归属/ })).toHaveValue("");
  await user.type(dialog.getByLabelText("账户名称"), "家庭储蓄");
  await user.type(dialog.getByLabelText(/这个余额包含什么/), "仅活期存款");
  await user.selectOptions(
    dialog.getByRole("combobox", { name: /^账户归属/ }),
    "member-two",
  );
  await user.click(dialog.getByRole("button", { name: "保存" }));
  expect(command).toHaveBeenCalledOnce();
  expect(command).toHaveBeenCalledWith(
    "/accounts",
    "POST",
    expect.objectContaining({ name: "家庭储蓄", owner_user_id: "member-two" }),
    "添加账户",
  );
});

it("shows the owner name and allows an explicit change and clear", async () => {
  const { user, command } = setup({ owner: "member-one" });
  await screen.findByText("归属：小林");
  await user.click(screen.getByRole("button", { name: "设置" }));
  const dialog = within(screen.getByRole("dialog"));
  const owner = dialog.getByRole("combobox", { name: /^账户归属/ });
  expect(owner).toHaveValue("member-one");
  await user.selectOptions(owner, "member-two");
  await user.selectOptions(owner, "");
  await user.click(dialog.getByRole("button", { name: "保存" }));
  expect(command).toHaveBeenCalledWith(
    "/accounts/account-one",
    "PATCH",
    expect.objectContaining({ owner_user_id: null, expected_version: 4 }),
    "修改账户",
  );
});

it("preserves an existing owner when member loading fails", async () => {
  const { user, command } = setup({ owner: "member-two", failed: true });
  await screen.findByText("成员暂时无法读取");
  await user.click(screen.getByRole("button", { name: "设置" }));
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByRole("combobox", { name: /^账户归属/ })).toBeDisabled();
  await user.click(dialog.getByRole("button", { name: "保存" }));
  const body = command.mock.calls[0][2] as Record<string, unknown>;
  expect(body).not.toHaveProperty("owner_user_id");
});

it("preserves a disabled owner and does not offer disabled members for new assignment", async () => {
  const { user, command } = setup({
    owner: "member-two",
    family: [members[0], { ...members[1], status: "disabled" }],
  });
  await screen.findByText(/当前可选家庭成员不足两人/);
  await user.click(screen.getByRole("button", { name: "设置" }));
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByRole("option", { name: "小夏（已停用）" })).toBeDisabled();
  expect(dialog.getByRole("combobox", { name: /^账户归属/ })).toHaveValue(
    "member-two",
  );
  await user.click(dialog.getByRole("button", { name: "保存" }));
  expect(command).toHaveBeenCalledWith(
    "/accounts/account-one",
    "PATCH",
    expect.objectContaining({ owner_user_id: "member-two" }),
    "修改账户",
  );
});

it.each([
  ["ios_shortcuts", false],
  ["android_app", true],
])(
  "provisions the selected device platform %s and keeps iPhone as default",
  async (platform, change) => {
    const { user, request } = setup();
    await user.click(screen.getByRole("button", { name: "记账设备" }));
    await user.click(screen.getByRole("button", { name: "添加设备" }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByRole("combobox", { name: "设备平台" })).toHaveValue(
      "ios_shortcuts",
    );
    if (change)
      await user.selectOptions(
        dialog.getByRole("combobox", { name: "设备平台" }),
        "android_app",
      );
    await user.type(dialog.getByLabelText("设备名称"), "合成测试手机");
    request.mockResolvedValueOnce({
      token: "synthetic-once-only-token",
    } as never);
    await user.click(dialog.getByRole("button", { name: "创建设备" }));
    expect(request).toHaveBeenCalledWith("/devices", {
      method: "POST",
      body: JSON.stringify({ device_name: "合成测试手机", platform }),
    });
    expect(await dialog.findByLabelText("设备访问凭据")).toHaveValue(
      "synthetic-once-only-token",
    );
  },
);
