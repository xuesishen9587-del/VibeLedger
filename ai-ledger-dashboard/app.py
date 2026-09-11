import os
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Dict, Any, List

from api_client import (
    ApiClient,
    ApiError,
    AuthError,
    ForbiddenError,
    NotFoundError,
    ConflictError,
    ValidationError,
    ServiceUnavailableError,
    BackendUnavailableError,
    TimeoutError
)
from time_utils import format_iso_timestamp, get_dashboard_today, get_dashboard_now, get_dashboard_timezone
from dashboard_controller import (
    classify_candidates,
    format_candidate_options,
    is_ambiguous_match_candidate,
    is_type_ambiguous_candidate,
    is_credit_ambiguous_candidate,
    is_category_required_candidate,
    build_category_patch_payload,
    is_batch_ready_to_commit
)
from settings_controller import (
    SettingsActionController,
    MutationModifiedPendingError,
    SLOT_CREATE_ACCOUNT,
    SLOT_CREATE_ALIAS,
    SLOT_CREATE_CATEGORY,
)

# --- 页面全局设置 ---
st.set_page_config(page_title="Vibe Finance Center", page_icon="🏦", layout="wide")

# --- 注入紧凑美观的 CSS 样式 ---
st.markdown("""
    <style>
    .block-container {
        padding-top: 1rem !important;
        padding-bottom: 1.5rem !important;
        padding-left: 1.2rem !important;
        padding-right: 1.2rem !important;
    }
    div[data-testid="stMetric"] {
        padding: 0.5rem 0.8rem !important;
        border-radius: 8px;
        background-color: rgba(240, 242, 246, 0.2);
        border: 1px solid rgba(0,0,0,0.08);
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.45rem !important;
    }
    div[data-testid="stMetricLabel"] {
        font-size: 0.85rem !important;
    }
    </style>
""", unsafe_allow_html=True)


# --- 初始化 Session State & API Client ---

def get_api_client() -> ApiClient:
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    auth_token = st.session_state.get("auth_token", os.environ.get("AUTH_TOKEN"))
    return ApiClient(base_url=backend_url, auth_token=auth_token)

client = get_api_client()


# --- 辅助函数：安全错误展示 ---

def handle_api_error(e: Exception, context_msg: str = "请求异常"):
    if isinstance(e, AuthError):
        st.error(f"🔒 认证失败或会话已过期: {e.message}。请在左侧侧边栏配置有效的 Browser Token。")
    elif isinstance(e, ForbiddenError):
        st.error(f"🚫 权限不足: {e.message}")
    elif isinstance(e, ConflictError):
        st.error(f"⚠️ 版本或并发冲突: {e.message} (错误码: {e.code})。请刷新数据后重试。")
    elif isinstance(e, ValidationError):
        st.error(f"❌ 参数校验失败: {e.message}")
    elif isinstance(e, (BackendUnavailableError, TimeoutError, ServiceUnavailableError)):
        st.error(f"🔌 后端服务暂不可用: {e.message}")
    elif isinstance(e, ApiError):
        st.error(f"❌ {context_msg}: [{e.code}] {e.message}")
    else:
        st.error(f"❌ {context_msg}: {e}")


# --- 侧边栏：认证管理、导航与快捷余额校准 ---

st.sidebar.title("🏦 Vibe Ledger")
st.sidebar.caption("智能多模态家庭账本 · Product v1")

# 1. 认证管理与会话状态
with st.sidebar.expander("🔑 会话认证配置", expanded=not bool(client.auth_token)):
    input_token = st.text_input(
        "Browser JWT Token",
        value=st.session_state.get("auth_token", os.environ.get("AUTH_TOKEN", "")),
        type="password",
        help="输入后端 Phase 10 Browser JWT Token 以调用 REST API"
    )
    if st.button("更新会话 Token"):
        st.session_state["auth_token"] = input_token.strip()
        client.set_auth_token(input_token.strip())
        st.success("Token 已更新！")
        st.rerun()

    if client.auth_token:
        st.caption("✅ 认证 Token 已挂载")
    else:
        st.warning("⚠️ 未配置 Token，后端接口需要 Browser Auth")

# 2. 页面导航
menu = st.sidebar.radio(
    "功能中心",
    [
        "💰 资产负债中心",
        "📊 收支统计中心",
        "📈 投资管理中心",
        "📄 账单导入",
        "🔎 支出复核",
        "💸 支出",
        "⚙️ 账户与分类管理",
        "📜 审计日志"
    ]
)

# ==============================================================================
# 页面 1: 💰 资产负债中心
# ==============================================================================
if menu == "💰 资产负债中心":
    from wealth_page import render
    try:
        render(client)
    except Exception as exc:
        handle_api_error(exc, "加载财富失败")

elif menu == "📊 收支统计中心":
    st.title("📊 家庭收支统计看板")
    st.markdown("权威统计家庭日常现金收支、费用与退款（投资盈亏与内部转账严格分离）")

    col_f1, col_f2 = st.columns(2)
    now_d = get_dashboard_today()
    current_year = now_d.year
    current_month = now_d.month

    with col_f1:
        sel_year = st.selectbox("年份", options=list(range(current_year, current_year - 5, -1)), index=0)
    with col_f2:
        sel_month = st.selectbox("月份", options=list(range(1, 13)), index=current_month - 1)

    import calendar
    _, last_day = calendar.monthrange(sel_year, sel_month)
    from_str = f"{sel_year:04d}-{sel_month:02d}-01"
    to_str = f"{sel_year:04d}-{sel_month:02d}-{last_day:02d}"

    try:
        cf = client.get_cash_flow(from_date=from_str, to_date=to_str)
        tx_resp = client.list_transactions(from_date=from_str, to_date=to_str, limit=100)
        transactions = tx_resp.get("items", [])
    except Exception as e:
        handle_api_error(e, "获取收支统计失败")
        cf = {}
        transactions = []

    if cf:
        rep_curr = cf.get("reporting_currency", "CNY")
        income = Decimal(str(cf.get("cash_income", "0.00")))
        expense = Decimal(str(cf.get("expense", "0.00")))
        refund = Decimal(str(cf.get("refund", "0.00")))
        net_cf = Decimal(str(cf.get("net_cash_flow", "0.00")))

        st.markdown(f"### 📅 {sel_year}年{sel_month}月 财务收支汇总")
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric(label="本月现金总收入", value=f"￥{income:,.2f}" if rep_curr == "CNY" else f"{income:,.2f} {rep_curr}")
        with m2:
            st.metric(label="本月总支出 (含手续费)", value=f"￥{expense:,.2f}" if rep_curr == "CNY" else f"{expense:,.2f} {rep_curr}", delta_color="inverse")
        with m3:
            st.metric(label="本月退款冲减", value=f"￥{refund:,.2f}" if rep_curr == "CNY" else f"{refund:,.2f} {rep_curr}")
        with m4:
            st.metric(label="本月净收支结余", value=f"￥{net_cf:,.2f}" if rep_curr == "CNY" else f"{net_cf:,.2f} {rep_curr}", delta="结余盈余" if net_cf >= 0 else "赤字")

        st.divider()

        # Item 7: 支出分类构成图 (Consumes Backend Authoritative expense_by_category)
        expense_by_category = cf.get("expense_by_category", [])
        if expense_by_category:
            cat_rows = [
                {"分类": item["category_name"], "金额": float(Decimal(str(item["amount"])))}
                for item in expense_by_category
                if Decimal(str(item["amount"])) > 0
            ]
            if cat_rows:
                df_exp = pd.DataFrame(cat_rows)
                c_pie1, c_pie2 = st.columns([1, 1])
                with c_pie1:
                    st.subheader("🍕 本月支出构成 (按分类)")
                    fig = px.pie(df_exp, values="金额", names="分类", hole=0.3, color_discrete_sequence=px.colors.qualitative.Safe)
                    fig.update_traces(textposition="outside", textinfo="percent+label")
                    st.plotly_chart(fig, use_container_width=True)

                with c_pie2:
                    st.subheader("📋 支出分类明细排行")
                    st.dataframe(df_exp.sort_values(by="金额", ascending=False), use_container_width=True, hide_index=True)

        st.divider()

        # 流水清单
        st.subheader("📋 本月交易流水明细")
        if transactions:
            tx_table = []
            for t in transactions:
                tx_table.append({
                    "日期": t.get("occurred_on"),
                    "类型": t.get("transaction_type"),
                    "商户/交易方": t.get("merchant", "-"),
                    "原始金额": f"{t.get('original_currency')} {t.get('original_amount')}",
                    "扣款金额": f"{t.get('from_currency', '')} {t.get('from_amount', '')}" if t.get("from_amount") else "-",
                    "分类": t.get("category", {}).get("name") if t.get("category") else "-",
                    "状态": t.get("verification_status")
                })
            st.dataframe(pd.DataFrame(tx_table), use_container_width=True, hide_index=True)
        else:
            st.info("该月暂无交易记录。")


# ==============================================================================
# 页面 3: 📈 投资管理中心
# ==============================================================================
elif menu == "📈 投资管理中心":
    st.title("📈 投资管理中心")
    st.markdown("监控投资账户估值、已确认投资盈亏与资金进出（投资收益独立核算）")

    try:
        inv_summary = client.get_investments()
        accounts_data = client.list_accounts(account_type="investment", status="active").get("items", [])
    except Exception as e:
        handle_api_error(e, "加载投资数据失败")
        inv_summary = {}
        accounts_data = []

    # Item 3: KPI Metrics using total_valuation from backend
    tot_val = Decimal(str(inv_summary.get("total_valuation", "0.00")))
    pnl = Decimal(str(inv_summary.get("total_pnl", "0.00")))
    rep_curr = inv_summary.get("reporting_currency", "CNY")

    i1, i2 = st.columns(2)
    with i1:
        st.metric(label=f"投资总估值 ({rep_curr})", value=f"￥{tot_val:,.2f}" if rep_curr == "CNY" else f"{tot_val:,.2f} {rep_curr}")
    with i2:
        st.metric(label=f"累计确认投资盈亏 ({rep_curr})", value=f"￥{pnl:,.2f}" if rep_curr == "CNY" else f"{pnl:,.2f} {rep_curr}", delta=f"{'盈利' if pnl >= 0 else '亏损'}")

    st.divider()

    # Item 2: 投资估值录入与校准 (Aligned with Backend contract: total_asset_value, currency, as_of)
    st.subheader("📝 投资账户估值快照录入 (Investment Valuation Snapshot)")
    if accounts_data:
        acc_dict = {a["name"]: a for a in accounts_data}
        with st.form("investment_snapshot_form", clear_on_submit=True):
            sel_inv_name = st.selectbox("选择投资账户", options=list(acc_dict.keys()))
            inv_total_val = st.number_input("期末权威总资产估值", value=0.0, step=1000.0, format="%.2f")
            inv_as_of = st.date_input("估值基准日期", value=get_dashboard_today())
            submit_inv = st.form_submit_button("提交投资估值")

            if submit_inv:
                inv_acc = acc_dict[sel_inv_name]
                try:
                    iso_as_of = format_iso_timestamp(inv_as_of)
                    res = client.create_investment_snapshot(
                        account_id=inv_acc["id"],
                        total_asset_value=Decimal(str(inv_total_val)),
                        currency=inv_acc["currency"],
                        as_of=iso_as_of
                    )
                    pnl_obj = res.get("investment_pnl")
                    if pnl_obj is None:
                        st.success(f"🎉 投资初始基准已成功建立！快照 ID: {res.get('snapshot_id')}")
                    else:
                        st.success(f"🎉 投资估值已提交！计算确认投资盈亏: {pnl_obj.get('currency', 'CNY')} {pnl_obj.get('pnl_amount', '0.00')}")
                    st.rerun()
                except Exception as ex:
                    handle_api_error(ex, "投资估值提交失败")
    else:
        st.info("当前暂无投资类型账户。可在「账户与分类管理」中创建投资账户。")


# ==============================================================================
# 页面 4: ⚖️ 账户校准与对账
# ==============================================================================
elif menu == "📄 账单导入":
    from statement_page import render
    try:
        render(client)
    except Exception as exc:
        handle_api_error(exc, "加载账单失败")

elif menu in ("💸 支出", "🔎 支出复核"):
    from spending_page import render
    try:
        render(client, review_only=menu == "🔎 支出复核")
        if menu == "🔎 支出复核":
            from spending_controller import SpendingActions
            from wealth_page import render_balance_drafts
            from statement_page import render as render_statements
            render_balance_drafts(client, SpendingActions(st.session_state.setdefault("_wealth_actions", {}), client))
            render_statements(client, review_only=True)
    except Exception as exc:
        handle_api_error(exc, "加载支出失败")

elif menu == "⚙️ 账户与分类管理":
    settings_ctrl = SettingsActionController()
    col_hdr1, col_hdr2 = st.columns([5, 1])
    with col_hdr1:
        st.title("⚙️ 账户与分类管理")
        st.markdown("通过后端 REST API 维护家庭金融账户、账户别名及收支分类")
    with col_hdr2:
        if st.button("🔄 刷新状态", key="btn_settings_refresh", help="清除未决操作状态并重新加载最新数据"):
            settings_ctrl.clear_all_actions()
            st.rerun()

    tab_acc, tab_cat = st.tabs(["🏦 账户管理与别名", "🏷️ 收支分类管理"])

    with tab_acc:
        st.subheader("1. 现有账户列表")
        try:
            acc_list = client.list_accounts().get("items", [])
        except Exception as e:
            handle_api_error(e, "加载账户失败")
            acc_list = []

        if acc_list:
            acc_df = []
            for a in acc_list:
                acc_df.append({
                    "账户ID": a["id"],
                    "账户名称": a["name"],
                    "类型": a["account_type"],
                    "币种": a["currency"],
                    "平衡范围": a.get("balance_scope", "-"),
                    "风险级别": a.get("risk_level") or "-",
                    "开启日期": a.get("opened_on") or "-",
                    "版本": a.get("row_version", 0),
                    "状态": a.get("status", "active")
                })
            st.dataframe(pd.DataFrame(acc_df), use_container_width=True, hide_index=True)

        st.divider()

        # 2. 编辑已有账户
        if acc_list:
            st.subheader("2. 编辑已有账户")
            sel_edit_acc_id = st.selectbox(
                "选择要修改的账户",
                options=[a["id"] for a in acc_list],
                format_func=lambda x: next((f"{a['name']} ({a['status']})" for a in acc_list if a["id"] == x), x)
            )
            target_edit_acc = next((a for a in acc_list if a["id"] == sel_edit_acc_id), None)

            if target_edit_acc:
                acc_action_key = f"edit_acc_{target_edit_acc['id']}"
                if settings_ctrl.is_conflict_reload_required(acc_action_key):
                    st.warning("⚠️ 该账户已被并发操作修改 (ROW_VERSION_CONFLICT)。已重新加载当前版本，请重新确认修改并保存。")

                with st.form("edit_account_form"):
                    e_name = st.text_input("修改账户名称", value=target_edit_acc["name"])
                    e_scope = st.text_input("修改平衡范围 (balance_scope)", value=target_edit_acc.get("balance_scope", ""))
                    e_opened = st.text_input("开户日期 (YYYY-MM-DD, 选填)", value=str(target_edit_acc.get("opened_on") or ""))
                    if target_edit_acc["account_type"] == "credit":
                        e_risk = None
                        st.caption("信用卡账户不允许设置风险级别 (risk_level=None)")
                    else:
                        risk_choices = ["(保持原样)", "(清除设置)", "very_low", "low", "medium", "high"]
                        e_risk = st.selectbox("修改风险级别", risk_choices)
                    e_stmt = st.checkbox("启用账单导入", value=bool(target_edit_acc.get("statement_import_enabled", False)))
                    btn_update_acc = st.form_submit_button("保存账户修改")

                    if btn_update_acc:
                        payload: Dict[str, Any] = {
                            "expected_version": target_edit_acc.get("row_version", 0)
                        }
                        if e_name.strip() and e_name.strip() != target_edit_acc["name"]:
                            payload["name"] = e_name.strip()
                        if e_scope.strip() and e_scope.strip() != target_edit_acc.get("balance_scope", ""):
                            payload["balance_scope"] = e_scope.strip()
                        if e_opened.strip() and e_opened.strip() != str(target_edit_acc.get("opened_on") or ""):
                            payload["opened_on"] = e_opened.strip()
                        if target_edit_acc["account_type"] != "credit":
                            if e_risk == "(清除设置)":
                                payload["risk_level"] = None
                            elif e_risk != "(保持原样)":
                                payload["risk_level"] = e_risk
                        if e_stmt != bool(target_edit_acc.get("statement_import_enabled", False)):
                            payload["statement_import_enabled"] = e_stmt

                        if len(payload) > 1:
                            try:
                                settings_ctrl.execute_mutation(
                                    action_key=acc_action_key,
                                    operation="patch_account",
                                    payload=payload,
                                    resource_id=str(target_edit_acc["id"]),
                                    mutation_fn=lambda k: client.update_account(target_edit_acc["id"], payload, idempotency_key=k)
                                )
                                st.success("🎉 账户信息已更新！")
                                st.rerun()
                            except ConflictError as ex:
                                if ex.code == "ROW_VERSION_CONFLICT":
                                    st.error("⚠️ 保存冲突 (ROW_VERSION_CONFLICT)：该账户已被其他操作更新。请刷新数据后重新修改。")
                                    st.rerun()
                                else:
                                    handle_api_error(ex, "更新账户失败")
                            except MutationModifiedPendingError as ex:
                                st.error(f"⚠️ 操作异常: {ex}")
                            except Exception as ex:
                                handle_api_error(ex, "更新账户失败")

        st.divider()

        # 3. 创建新账户
        st.subheader("3. 创建新账户")
        with st.form("create_account_form", clear_on_submit=True):
            n_name = st.text_input("账户名称 (如: 工行日常卡)")
            n_type = st.selectbox("账户类型", ["cash", "savings", "credit", "investment"])
            n_scope = st.text_input("平衡范围 (balance_scope)", value="liability" if n_type == "credit" else "asset")
            n_curr = st.selectbox("币种", ["CNY", "USD", "EUR", "JPY", "SGD", "HKD", "GBP"])
            n_opened = st.text_input("开户日期 (YYYY-MM-DD, 选填)", value="")
            if n_type == "credit":
                n_risk = None
                st.caption("信用卡账户不允许设置风险级别 (risk_level=None)")
            else:
                n_risk_sel = st.selectbox("风险级别 (选填)", ["(无)", "very_low", "low", "medium", "high"])
                n_risk = None if n_risk_sel == "(无)" else n_risk_sel
            n_stmt = st.checkbox("启用账单导入", value=False)
            btn_create_acc = st.form_submit_button("创建账户")

            if btn_create_acc:
                if not n_name.strip() or not n_scope.strip():
                    st.error("账户名称和平衡范围为必填项！")
                else:
                    create_payload: Dict[str, Any] = {
                        "name": n_name.strip(),
                        "balance_scope": n_scope.strip(),
                        "account_type": n_type,
                        "currency": n_curr,
                        "statement_import_enabled": n_stmt,
                    }
                    if n_risk:
                        create_payload["risk_level"] = n_risk
                    if n_opened.strip():
                        create_payload["opened_on"] = n_opened.strip()

                    create_acc_action_key = SLOT_CREATE_ACCOUNT
                    try:
                        settings_ctrl.execute_mutation(
                            action_key=create_acc_action_key,
                            operation="create_account",
                            payload=create_payload,
                            mutation_fn=lambda k: client.create_account(
                                name=create_payload["name"],
                                balance_scope=create_payload["balance_scope"],
                                account_type=create_payload["account_type"],
                                currency=create_payload["currency"],
                                idempotency_key=k,
                                risk_level=create_payload.get("risk_level"),
                                opened_on=create_payload.get("opened_on"),
                                statement_import_enabled=create_payload["statement_import_enabled"],
                            )
                        )
                        st.success(f"🎉 账户 {n_name} 创建成功！")
                        st.rerun()
                    except MutationModifiedPendingError as ex:
                        st.error(f"⚠️ 操作异常: {ex}")
                    except Exception as ex:
                        handle_api_error(ex, "创建账户失败")

        # 4. 账户别名管理
        if acc_list:
            st.divider()
            st.subheader("4. 账户别名管理 (快捷指令截图多模态匹配)")
            sel_alias_acc = st.selectbox("选择要管理别名的账户", options=[a["id"] for a in acc_list], format_func=lambda x: next((a["name"] for a in acc_list if a["id"] == x), x))
            try:
                aliases = client.list_account_aliases(sel_alias_acc).get("items", [])
                if aliases:
                    st.write("已配置别名:")
                    for al in aliases:
                        al_col1, al_col2 = st.columns([3, 1])
                        with al_col1:
                            st.write(f"- `{al['alias']}` ({al.get('status', 'active')}, v{al.get('row_version', 0)})")
                        with al_col2:
                            if al.get("status") == "active":
                                if st.button("⛔ 停用别名", key=f"archive_al_{al['id']}"):
                                    alias_action_key = f"archive_alias_{al['id']}"
                                    archive_payload = {
                                        "status": "inactive",
                                        "expected_version": al.get("row_version", 0)
                                    }
                                    try:
                                        settings_ctrl.execute_mutation(
                                            action_key=alias_action_key,
                                            operation="patch_alias_archive",
                                            payload=archive_payload,
                                            resource_id=f"{sel_alias_acc}:{al['id']}",
                                            mutation_fn=lambda k: client.update_account_alias(
                                                account_id=sel_alias_acc,
                                                alias_id=al["id"],
                                                payload=archive_payload,
                                                idempotency_key=k,
                                            )
                                        )
                                        st.success("别名已停用")
                                        st.rerun()
                                    except ConflictError as ex:
                                        if ex.code == "ROW_VERSION_CONFLICT":
                                            st.error("⚠️ 停用冲突 (ROW_VERSION_CONFLICT)：该别名已被更新。请刷新数据后重试。")
                                            st.rerun()
                                        else:
                                            handle_api_error(ex, "停用别名失败")
                                    except MutationModifiedPendingError as ex:
                                        st.error(f"⚠️ 操作异常: {ex}")
                                    except Exception as ex:
                                        handle_api_error(ex, "停用别名失败")
                            else:
                                st.info("已停用")
                else:
                    st.caption("该账户暂无别名。")
            except Exception as e:
                handle_api_error(e, "加载别名失败")

            with st.form("add_alias_form", clear_on_submit=True):
                new_alias_str = st.text_input("新增别名 (如: 工行Visa卡)")
                if st.form_submit_button("添加别名") and new_alias_str.strip():
                    add_alias_key = SLOT_CREATE_ALIAS
                    alias_payload = {"alias": new_alias_str.strip()}
                    try:
                        settings_ctrl.execute_mutation(
                            action_key=add_alias_key,
                            operation="create_alias",
                            payload=alias_payload,
                            resource_id=str(sel_alias_acc),
                            mutation_fn=lambda k: client.create_account_alias(sel_alias_acc, new_alias_str.strip(), idempotency_key=k)
                        )
                        st.success("别名添加成功！")
                        st.rerun()
                    except MutationModifiedPendingError as ex:
                        st.error(f"⚠️ 操作异常: {ex}")
                    except Exception as ex:
                        handle_api_error(ex, "添加别名失败")

    with tab_cat:
        st.subheader("🏷️ 收支分类列表")
        try:
            cats = client.list_categories(status=None).get("items", [])
        except Exception as e:
            handle_api_error(e, "加载分类失败")
            cats = []

        if cats:
            cat_df = pd.DataFrame(cats)
            st.dataframe(cat_df, use_container_width=True, hide_index=True)

        st.divider()

        # 编辑或停用已有分类
        if cats:
            st.subheader("编辑或停用分类")
            sel_edit_cat_id = st.selectbox(
                "选择分类",
                options=[c["id"] for c in cats],
                format_func=lambda x: next((f"{c['name']} ({c['category_type']}, {c['status']})" for c in cats if c["id"] == x), x)
            )
            target_edit_cat = next((c for c in cats if c["id"] == sel_edit_cat_id), None)
            if target_edit_cat:
                cat_action_key = f"edit_cat_{target_edit_cat['id']}"
                if settings_ctrl.is_conflict_reload_required(cat_action_key):
                    st.warning("⚠️ 该分类已被并发操作修改 (ROW_VERSION_CONFLICT)。已重新加载当前版本，请核对并重新保存。")

                c_edit1, c_edit2 = st.columns([3, 1])
                with c_edit1:
                    with st.form("edit_cat_form"):
                        new_cat_name = st.text_input("分类名称", value=target_edit_cat["name"])
                        new_cat_desc = st.text_input("分类描述 (选填)", value=target_edit_cat.get("description") or "")
                        btn_update_cat = st.form_submit_button("保存分类修改")
                        if btn_update_cat:
                            cat_payload: Dict[str, Any] = {
                                "expected_version": target_edit_cat.get("row_version", 0)
                            }
                            if new_cat_name.strip() and new_cat_name.strip() != target_edit_cat["name"]:
                                cat_payload["name"] = new_cat_name.strip()
                            if (new_cat_desc.strip() or None) != target_edit_cat.get("description"):
                                cat_payload["description"] = new_cat_desc.strip() or None

                            if len(cat_payload) > 1:
                                try:
                                    settings_ctrl.execute_mutation(
                                        action_key=cat_action_key,
                                        operation="patch_category",
                                        payload=cat_payload,
                                        resource_id=str(target_edit_cat["id"]),
                                        mutation_fn=lambda k: client.update_category(target_edit_cat["id"], cat_payload, idempotency_key=k)
                                    )
                                    st.success("分类名称已更新！")
                                    st.rerun()
                                except ConflictError as ex:
                                    if ex.code == "ROW_VERSION_CONFLICT":
                                        st.error("⚠️ 保存冲突 (ROW_VERSION_CONFLICT)：该分类已被其他操作更新。请刷新数据后重试。")
                                        st.rerun()
                                    else:
                                        handle_api_error(ex, "更新分类失败")
                                except MutationModifiedPendingError as ex:
                                    st.error(f"⚠️ 操作异常: {ex}")
                                except Exception as ex:
                                    handle_api_error(ex, "更新分类失败")
                with c_edit2:
                    if target_edit_cat.get("status") == "active":
                        if st.button("⛔ 停用分类", key=f"archive_cat_{target_edit_cat['id']}"):
                            archive_cat_key = f"archive_cat_{target_edit_cat['id']}"
                            archive_payload = {
                                "status": "inactive",
                                "expected_version": target_edit_cat.get("row_version", 0)
                            }
                            try:
                                settings_ctrl.execute_mutation(
                                    action_key=archive_cat_key,
                                    operation="patch_category_archive",
                                    payload=archive_payload,
                                    resource_id=str(target_edit_cat["id"]),
                                    mutation_fn=lambda k: client.update_category(target_edit_cat["id"], archive_payload, idempotency_key=k)
                                )
                                st.success("分类已停用")
                                st.rerun()
                            except ConflictError as ex:
                                if ex.code == "ROW_VERSION_CONFLICT":
                                    st.error("⚠️ 停用冲突 (ROW_VERSION_CONFLICT)：该分类已被其他操作更新。请刷新数据后重试。")
                                    st.rerun()
                                else:
                                    handle_api_error(ex, "停用分类失败")
                            except MutationModifiedPendingError as ex:
                                st.error(f"⚠️ 操作异常: {ex}")
                            except Exception as ex:
                                handle_api_error(ex, "停用分类失败")
                    else:
                        st.info("已停用")

        st.divider()
        st.subheader("创建新分类")
        with st.form("create_cat_form", clear_on_submit=True):
            cat_name = st.text_input("分类名称 (如: 餐饮美食, 数码科技)")
            cat_type = st.selectbox("分类性质", ["expense", "income"])
            cat_desc = st.text_input("分类描述 (选填)", value="")
            if st.form_submit_button("创建分类") and cat_name.strip():
                create_cat_key = SLOT_CREATE_CATEGORY
                create_cat_payload = {
                    "name": cat_name.strip(),
                    "category_type": cat_type,
                    "description": cat_desc.strip() if cat_desc.strip() else None
                }
                try:
                    settings_ctrl.execute_mutation(
                        action_key=create_cat_key,
                        operation="create_category",
                        payload=create_cat_payload,
                        mutation_fn=lambda k: client.create_category(
                            name=cat_name.strip(),
                            category_type=cat_type,
                            description=create_cat_payload["description"],
                            idempotency_key=k,
                        )
                    )
                    st.success(f"分类 {cat_name} 创建成功！")
                    st.rerun()
                except MutationModifiedPendingError as ex:
                    st.error(f"⚠️ 操作异常: {ex}")
                except Exception as ex:
                    handle_api_error(ex, "创建分类失败")


# ==============================================================================
# 页面 8: 📜 审计日志
# ==============================================================================
elif menu == "📜 审计日志":
    st.title("📜 不可变审计日志中心 (Audit Events)")
    st.markdown("查看家庭所有关键金融操作（流水录入、对账提交、交易纠错、作废）的不可变审计记录 (只读)")

    col_a1, col_a2 = st.columns(2)
    with col_a1:
        aud_entity_type = st.selectbox("过滤实体类型", ["全部", "transaction", "account", "category", "device", "snapshot", "reconciliation_batch"])
    with col_a2:
        aud_limit = st.slider("查询数量", min_value=10, max_value=100, value=50, key="aud_limit_slider")

    try:
        aud_resp = client.list_audit_events(
            entity_type=None if aud_entity_type == "全部" else aud_entity_type,
            limit=aud_limit
        )
        events = aud_resp.get("items", [])
    except Exception as e:
        handle_api_error(e, "加载审计日志失败")
        events = []

    if events:
        for ev in events:
            with st.expander(f"🕒 {ev.get('created_at')} · [{ev.get('action').upper()}] {ev.get('entity_type')} ({ev.get('entity_id')[:8]}...)", expanded=False):
                st.write(f"**操作者类型**: `{ev.get('actor_type')}` | **用户 ID**: `{ev.get('actor_user_id') or '-'}` | **设备 ID**: `{ev.get('actor_device_id') or '-'}`")
                st.write(f"**Action**: `{ev.get('action')}` | **实体 ID**: `{ev.get('entity_id')}`")

                c_before, c_after = st.columns(2)
                with c_before:
                    st.markdown("**变更前数据 (before_data):**")
                    st.json(ev.get("before_data") or {})
                with c_after:
                    st.markdown("**变更后数据 (after_data):**")
                    st.json(ev.get("after_data") or {})
    else:
        st.info("暂无符合条件的审计日志记录。")