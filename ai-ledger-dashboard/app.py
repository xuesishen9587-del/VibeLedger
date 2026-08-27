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
        "⚖️ 账户校准与对账",
        "📥 待办工单中心",
        "📋 交易明细与纠错/作废",
        "⚙️ 账户与分类管理",
        "📜 审计日志"
    ]
)

# 3. 侧边栏快捷余额校准 (Item 1: Timezone-aware ISO as_of)
st.sidebar.divider()
st.sidebar.subheader("🎯 快速余额校准 (Snapshot)")
st.sidebar.caption("向后端提交账户实际余额，由后端对账引擎自动核对校准。")

active_accounts: List[Dict[str, Any]] = []
try:
    if client.auth_token:
        acc_resp = client.list_accounts(status="active")
        active_accounts = acc_resp.get("items", [])
except Exception:
    pass

if active_accounts:
    acc_map = {f"{a['name']} ({a['currency']})": a for a in active_accounts}
    with st.sidebar.form("quick_snapshot_form", clear_on_submit=True):
        sel_acc_label = st.selectbox("选择账户", options=list(acc_map.keys()))
        input_balance = st.number_input("当前权威真实余额", value=0.0, step=100.0, format="%.2f")
        submit_snap = st.form_submit_button("提交校准")

        if submit_snap:
            chosen_acc = acc_map[sel_acc_label]
            try:
                iso_as_of = format_iso_timestamp(get_dashboard_today())
                snap_res = client.create_account_snapshot(
                    account_id=chosen_acc["id"],
                    balance=Decimal(str(input_balance)),
                    as_of=iso_as_of,
                    currency=chosen_acc["currency"]
                )
                if snap_res.get("status") == "committed":
                    st.sidebar.success(f"🎉 校准成功！账户已确认为 {chosen_acc['currency']} {input_balance:,.2f}")
                elif snap_res.get("status") == "needs_review":
                    st.sidebar.warning("⚠️ 差额超过自动平账阈值，已创建对账工单等待人工复核。请前往「待办工单中心」查看。")
                else:
                    st.sidebar.info(f"对账结果: {snap_res.get('status')}")
                st.rerun()
            except Exception as ex:
                handle_api_error(ex, "余额校准失败")
else:
    st.sidebar.caption("💡 配置有效 Token 后将加载可用账户列表。")


# ==============================================================================
# 页面 1: 💰 资产负债中心
# ==============================================================================
if menu == "💰 资产负债中心":
    st.title("💰 家庭资产负债中心")
    st.markdown("全天候监控家庭资产、负债、净资产与信用卡账单状态 (数据源自权威后端 API)")

    try:
        overview = client.get_overview()
        accounts_data = client.list_accounts(status="active").get("items", [])
        freshness_data = client.get_account_freshness().get("items", [])
        installments_data = client.list_installment_plans().get("items", [])
    except Exception as e:
        handle_api_error(e, "加载资产负债数据失败")
        overview = {}
        accounts_data = []
        freshness_data = []
        installments_data = []

    if overview:
        rep_curr = overview.get("reporting_currency", "CNY")
        tot_assets = Decimal(str(overview.get("total_assets", "0.00")))
        tot_liab = Decimal(str(overview.get("total_liabilities", "0.00")))
        net_worth = Decimal(str(overview.get("net_worth", "0.00")))
        freshness = overview.get("data_freshness", {})
        ratio_30d = float(freshness.get("confirmed_within_30d_ratio", 1.0)) * 100

        # 1. KPI Cards
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric(label=f"家庭总资产 ({rep_curr})", value=f"￥{tot_assets:,.2f}" if rep_curr == "CNY" else f"{tot_assets:,.2f} {rep_curr}")
        with c2:
            st.metric(label=f"家庭总负债 ({rep_curr})", value=f"￥{tot_liab:,.2f}" if rep_curr == "CNY" else f"{tot_liab:,.2f} {rep_curr}", delta_color="inverse")
        with c3:
            st.metric(label=f"家庭净资产水位 ({rep_curr})", value=f"￥{net_worth:,.2f}" if rep_curr == "CNY" else f"{net_worth:,.2f} {rep_curr}")
        with c4:
            st.metric(label="数据新鲜度 (30日权威对账率)", value=f"{ratio_30d:.1f}%")

        st.divider()

        # 2. 账户分布矩阵 (四分类)
        st.subheader("🏦 账户资产负债分布明细")
        freshness_map = {item["account_id"]: item for item in freshness_data}

        col_left, col_right = st.columns(2)
        with col_left:
            with st.expander("👛 现金钱包 (活期借记卡/电子钱包)", expanded=True):
                cash_accs = [a for a in accounts_data if a["account_type"] == "cash"]
                for a in cash_accs:
                    bal = a.get("state", {}).get("ledger_balance", "0.00")
                    f_info = freshness_map.get(a["id"], {})
                    f_tag = f"🟢  свежий ({f_info.get('age_days', 0)}天)" if f_info.get("freshness") == "fresh" else "🟡 待核对"
                    st.write(f"- **{a['name']}**: `{a['currency']} {Decimal(str(bal)):,.2f}` · *{f_tag}*")

            with st.expander("🛡️ 储蓄存款 (银行定期/大额存单/国债)", expanded=True):
                save_accs = [a for a in accounts_data if a["account_type"] == "savings"]
                for a in save_accs:
                    bal = a.get("state", {}).get("ledger_balance", "0.00")
                    st.write(f"- **{a['name']}**: `{a['currency']} {Decimal(str(bal)):,.2f}`")

        with col_right:
            with st.expander("📈 投资资产 (理财/股票/基金)", expanded=True):
                inv_accs = [a for a in accounts_data if a["account_type"] == "investment"]
                for a in inv_accs:
                    bal = a.get("state", {}).get("ledger_balance", "0.00")
                    st.write(f"- **{a['name']}**: `{a['currency']} {Decimal(str(bal)):,.2f}`")

            with st.expander("💳 信用负债 (信用卡/信用账户)", expanded=True):
                credit_accs = [a for a in accounts_data if a["account_type"] == "credit"]
                for a in credit_accs:
                    bal = a.get("state", {}).get("ledger_balance", "0.00")
                    st.write(f"- **{a['name']}**: `{a['currency']} {Decimal(str(bal)):,.2f}`")

        st.divider()

        # 3. 信用卡还款透视 (后端权威状态)
        st.subheader("💳 信用卡还款透视")
        credit_cards = [a for a in accounts_data if a["account_type"] == "credit"]
        if credit_cards:
            cc_rows = []
            for card in credit_cards:
                try:
                    cc_state = client.get_credit_card_state(card["id"])
                    snap = cc_state.get("latest_snapshot") or {}
                    b_day = card.get("billing_day")
                    d_day = card.get("due_day")
                    b_str = f"每月 {int(b_day)} 号" if b_day is not None else "-"
                    d_str = f"每月 {int(d_day)} 号" if d_day is not None else "-"
                    stmt_bal = snap.get("statement_balance", "0.00")
                    rem_due = snap.get("remaining_statement_due", "0.00")
                    unbilled = snap.get("unbilled_balance", "0.00")
                    curr_out = snap.get("current_outstanding", "0.00")

                    cc_rows.append({
                        "卡片名称": card["name"],
                        "币种": card["currency"],
                        "账单日": b_str,
                        "还款日": d_str,
                        "已出账单金额": f"{card['currency']} {Decimal(str(stmt_bal)):,.2f}",
                        "本期剩余应还": f"{card['currency']} {Decimal(str(rem_due)):,.2f}",
                        "未出账单消费": f"{card['currency']} {Decimal(str(unbilled)):,.2f}",
                        "总欠款水位": f"{card['currency']} {Decimal(str(curr_out)):,.2f}"
                    })
                except Exception:
                    pass

            if cc_rows:
                st.dataframe(pd.DataFrame(cc_rows), use_container_width=True, hide_index=True)
            else:
                st.info("暂无可用的信用卡账单快照数据。")
        else:
            st.info("当前无信用卡账户记录。")

        # 4. 分期计划透视
        if installments_data:
            st.subheader("📋 进行中的分期付款计划")
            inst_rows = []
            for plan in installments_data:
                inst_rows.append({
                    "计划ID": plan["id"][:8],
                    "商户/商品": plan.get("merchant", "-"),
                    "总期数": plan.get("total_periods"),
                    "已入账期数": plan.get("recognized_periods", 0),
                    "总金额": f"{plan.get('currency', 'CNY')} {Decimal(str(plan.get('total_amount', '0.00'))):,.2f}",
                    "状态": plan.get("status")
                })
            st.dataframe(pd.DataFrame(inst_rows), use_container_width=True, hide_index=True)

        st.divider()

        # 5. 资产配置透视饼图 (Item 6: Consumes Backend Authoritative Reporting Currency asset_allocation)
        st.subheader("📊 家庭资产配置透视 (本位币核算)")
        asset_allocations = overview.get("asset_allocation", [])
        chart_rows = []
        for alloc in asset_allocations:
            amt_dec = Decimal(str(alloc.get("amount", "0.00")))
            if amt_dec > 0:
                chart_rows.append({
                    "资产类型": alloc.get("account_type_label", alloc.get("account_type")),
                    "金额": float(amt_dec)
                })

        if chart_rows:
            df_chart = pd.DataFrame(chart_rows)
            fig = px.pie(
                df_chart, values="金额", names="资产类型", hole=0.4,
                color_discrete_sequence=px.colors.qualitative.Pastel
            )
            fig.update_traces(textposition="outside", textinfo="percent+label")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("当前无正资产配置数据。")


# ==============================================================================
# 页面 2: 📊 收支统计中心
# ==============================================================================
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
elif menu == "⚖️ 账户校准与对账":
    st.title("⚖️ 账户校准与对账中心")
    st.markdown("支持银行对账单 PDF 解析核对及手动余额快照校准")

    tab_stmt, tab_manual = st.tabs(["📄 银行对账单 PDF 上传与复核", "🎯 手工余额校准 (Snapshot)"])

    with tab_stmt:
        st.subheader("1. 上传银行对账单 PDF")
        if active_accounts:
            acc_opts = {f"{a['name']} ({a['currency']})": a for a in active_accounts}
            selected_stmt_acc = st.selectbox("选择对账账户", options=list(acc_opts.keys()), key="stmt_upload_acc")
            pdf_file = st.file_uploader("选择银行账单 PDF 文件", type=["pdf"])
            pdf_password = st.text_input("PDF 密码 (如账单有加密密码)", type="password", help="仅用于当次解析，后端绝不持久化密码")

            if st.button("开始上传并解析对账单") and pdf_file:
                target_acc = acc_opts[selected_stmt_acc]
                with st.spinner("正在由后端解析对账单并执行智能匹配引擎..."):
                    try:
                        file_bytes = pdf_file.read()
                        upload_res = client.upload_statement(
                            account_id=target_acc["id"],
                            file_bytes=file_bytes,
                            filename=pdf_file.name,
                            password=pdf_password if pdf_password else None
                        )
                        st.session_state["active_batch_id"] = upload_res.get("batch_id")
                        st.success(f"🎉 账单解析完成！对账批次 ID: {upload_res.get('batch_id')}")
                        st.rerun()
                    except Exception as ex:
                        handle_api_error(ex, "账单上传解析失败")

        # Item 4 & Item 5: 批次复核与提交展示
        active_batch_id = st.session_state.get("active_batch_id")
        if active_batch_id:
            st.divider()
            st.subheader(f"2. 对账批次复核 (批次: {active_batch_id[:8]}...)")
            try:
                preview = client.get_reconciliation_preview(active_batch_id)
                summary = preview.get("summary", {})
                batch_info = preview.get("batch", {})
                candidates = preview.get("candidates", [])

                p1, p2, p3, p4 = st.columns(4)
                with p1:
                    st.metric("匹配交易数", summary.get("matched_count", 0))
                with p2:
                    st.metric("新建流水数", summary.get("created_count", 0))
                with p3:
                    st.metric("待复核候选", summary.get("pending_count", 0))
                with p4:
                    st.metric("自动平账差异", f"￥{preview.get('adjustment_amount') or '0.00'}")

                # Item 1, 2, 3: Actionable Review Controls from preview["candidates"]
                if candidates:
                    st.markdown("#### 🔍 候选流水对账复核 (Candidate Review)")
                    classified = classify_candidates(candidates)
                    actionable_cands = classified["actionable"]
                    resolved_cands = classified["resolved"]
                    rejected_cands = classified["rejected"]

                    if not actionable_cands:
                        st.info("✅ 所有候选已完成复核决策，可提交对账批次入账。")
                    else:
                        for cand in actionable_cands:
                            c_id = cand["id"]
                            sl = cand.get("statement_line") or {}
                            c_type = cand.get("candidate_type")
                            r_code = cand.get("reason_code")
                            options = format_candidate_options(cand)
                            is_ambiguous = is_ambiguous_match_candidate(cand)
                            is_type_ambig = is_type_ambiguous_candidate(cand)
                            is_credit_ambig = is_credit_ambiguous_candidate(cand)
                            is_cat_req = is_category_required_candidate(cand)

                            with st.container():
                                c_col1, c_col2 = st.columns([3, 2])
                                with c_col1:
                                    st.write(f"📅 **{sl.get('transaction_on', '-')}** · `{sl.get('currency', '')} {sl.get('amount', '')}` · {sl.get('description', '')}")
                                with c_col2:
                                    st.write(f"类型: `{c_type}` | 状态: `{cand.get('status')}` | 原因: `{r_code or 'normal'}`")

                                selected_tx_id = None
                                # Ambiguous match selector
                                if is_ambiguous and options:
                                    st.warning("⚠️ 存在多个疑似匹配交易，请选择具体匹配项：")
                                    opt_labels = []
                                    opt_map = {}
                                    for opt in options:
                                        lbl = f"{opt.get('occurred_on')} | {opt.get('merchant') or '-'} | {opt.get('currency')} {opt.get('amount')} (匹配分: {opt.get('match_score')})"
                                        opt_labels.append(lbl)
                                        opt_map[lbl] = opt.get("transaction_id")
                                    sel_lbl = st.selectbox("选择目标交易", options=opt_labels, key=f"sel_target_{c_id}")
                                    selected_tx_id = opt_map.get(sel_lbl)

                                # Type ambiguous resolution (Debit / General ambiguity)
                                if is_type_ambig:
                                    st.warning("⚠️ 交易类型不明确，必须人工明确业务语义及支出分类：")
                                    t_col1, t_col2, t_col3 = st.columns([2, 2, 1])
                                    with t_col1:
                                        sel_type = st.selectbox("选择交易性质", options=["expense", "fee"], format_func=lambda x: "支出 (Expense)" if x == "expense" else "手续费 (Fee)", key=f"sel_type_{c_id}")
                                    with t_col2:
                                        exp_cats = [c for c in active_categories if c.get("category_type") == "expense"] or active_categories
                                        chosen_cat = st.selectbox(
                                            "选择支出分类",
                                            options=exp_cats,
                                            format_func=lambda x: x["name"],
                                            key=f"type_ambig_cat_{c_id}"
                                        )
                                    with t_col3:
                                        if st.button("确认性质与分类", key=f"res_type_btn_{c_id}"):
                                            try:
                                                client.resolve_reconciliation_candidate(
                                                    candidate_id=c_id,
                                                    resolution_type=sel_type,
                                                    category_id=chosen_cat["id"]
                                                )
                                                st.success("已明确交易性质并更新分类")
                                                st.rerun()
                                            except Exception as ex:
                                                handle_api_error(ex, "类型确认失败")

                                # Credit ambiguous resolution (Income vs Refund vs Transfer vs Match)
                                if is_credit_ambig:
                                    st.warning("⚠️ 存入/贷记流水业务性质未定，请选择确定项：")
                                    res_kind = st.radio(
                                        "选择资金性质",
                                        options=["cash_income", "refund", "transfer", "match"],
                                        format_func=lambda x: {
                                            "cash_income": "收入 (Cash Income)",
                                            "refund": "退款 (Refund)",
                                            "transfer": "内部转账 (Internal Transfer)",
                                            "match": "关联已有交易 (Match Transaction)"
                                        }[x],
                                        key=f"res_kind_{c_id}",
                                        horizontal=True
                                    )

                                    if res_kind == "cash_income":
                                        inc_col1, inc_col2 = st.columns([3, 1])
                                        with inc_col1:
                                            inc_cats = [c for c in active_categories if c.get("category_type") == "income"] or active_categories
                                            chosen_inc_cat = st.selectbox(
                                                "选择收入分类",
                                                options=inc_cats,
                                                format_func=lambda x: x["name"],
                                                key=f"res_inc_cat_{c_id}"
                                            )
                                        with inc_col2:
                                            if st.button("确认为收入", key=f"btn_res_inc_{c_id}"):
                                                try:
                                                    client.resolve_reconciliation_candidate(
                                                        candidate_id=c_id,
                                                        resolution_type="cash_income",
                                                        category_id=chosen_inc_cat["id"]
                                                    )
                                                    st.success("已确认为收入并更新分类")
                                                    st.rerun()
                                                except Exception as ex:
                                                    handle_api_error(ex, "收入确认失败")

                                    elif res_kind == "refund":
                                        ref_col1, ref_col2 = st.columns([3, 1])
                                        with ref_col1:
                                            orig_exp_input = st.text_input("原支出交易 ID (Original Expense ID)", key=f"orig_exp_id_{c_id}")
                                        with ref_col2:
                                            if st.button("确认为退款", key=f"btn_res_ref_{c_id}"):
                                                try:
                                                    client.resolve_reconciliation_candidate(
                                                        candidate_id=c_id,
                                                        resolution_type="refund",
                                                        original_expense_id=orig_exp_input.strip()
                                                    )
                                                    st.success("已确认为退款关联")
                                                    st.rerun()
                                                except Exception as ex:
                                                    handle_api_error(ex, "退款关联失败")

                                    elif res_kind == "transfer":
                                        tf_col1, tf_col2, tf_col3 = st.columns([2, 2, 1])
                                        with tf_col1:
                                            other_accs = [a for a in active_accounts if a["id"] != batch_info.get("account_id")] or active_accounts
                                            sel_counter_acc = st.selectbox(
                                                "对方转出账户",
                                                options=other_accs,
                                                format_func=lambda x: f"{x['name']} ({x['currency']})",
                                                key=f"sel_counter_{c_id}"
                                            )
                                        with tf_col2:
                                            cross_curr = sel_counter_acc and sel_counter_acc["currency"] != sl.get("currency")
                                            tf_counter_amt = None
                                            if cross_curr:
                                                tf_counter_amt = st.text_input(f"对方账户扣款金额 ({sel_counter_acc['currency']})", key=f"tf_amt_{c_id}")
                                            else:
                                                st.caption(f"同币种划转：扣款金额自动等额 ({sl.get('currency')} {sl.get('amount')})")
                                        with tf_col3:
                                            if st.button("确认为转账", key=f"btn_res_tf_{c_id}"):
                                                try:
                                                    client.resolve_reconciliation_candidate(
                                                        candidate_id=c_id,
                                                        resolution_type="transfer",
                                                        counter_account_id=sel_counter_acc["id"],
                                                        counter_amount=tf_counter_amt
                                                    )
                                                    st.success("已确认为内部转账")
                                                    st.rerun()
                                                except Exception as ex:
                                                    handle_api_error(ex, "转账确认失败")

                                    elif res_kind == "match":
                                        m_col1, m_col2 = st.columns([3, 1])
                                        with m_col1:
                                            match_tx_input = st.text_input("匹配目标交易 ID (Target Transaction ID)", key=f"target_tx_id_{c_id}")
                                        with m_col2:
                                            if st.button("确认为匹配", key=f"btn_res_m_{c_id}"):
                                                try:
                                                    client.resolve_reconciliation_candidate(
                                                        candidate_id=c_id,
                                                        resolution_type="match",
                                                        target_transaction_id=match_tx_input.strip()
                                                    )
                                                    st.success("已确认为交易匹配")
                                                    st.rerun()
                                                except Exception as ex:
                                                    handle_api_error(ex, "匹配确认失败")

                                # Category patch expander/form
                                if is_cat_req and not is_type_ambig:
                                    st.info("💡 该交易需要补充支出分类：")
                                    cat_col1, cat_col2 = st.columns([3, 1])
                                    with cat_col1:
                                        exp_cats = [c for c in active_categories if c.get("category_type") == "expense"] or active_categories
                                        chosen_cat = st.selectbox(
                                            "选择分类",
                                            options=exp_cats,
                                            format_func=lambda x: x["name"],
                                            key=f"patch_cat_sel_{c_id}"
                                        )
                                    with cat_col2:
                                        if st.button("保存分类", key=f"patch_cat_btn_{c_id}"):
                                            try:
                                                patch_p = build_category_patch_payload(cand, chosen_cat["id"])
                                                client.patch_reconciliation_candidate(c_id, patch_p)
                                                st.success("分类已补充，预览已刷新")
                                                st.rerun()
                                            except Exception as ex:
                                                handle_api_error(ex, "分类修改失败")

                                # Action buttons: direct Accept disabled if candidate has unresolved semantic ambiguity
                                has_unresolved_semantics = bool(r_code in ("TYPE_AMBIGUOUS", "INCOME_TRANSFER_REFUND_AMBIGUOUS"))
                                btn_c1, btn_c2 = st.columns(2)
                                with btn_c1:
                                    if st.button("接受 (Accept)", key=f"acc_cand_{c_id}", type="secondary", disabled=has_unresolved_semantics):
                                        try:
                                            client.accept_reconciliation_candidate(c_id, target_transaction_id=selected_tx_id)
                                            st.success("已接受该候选")
                                            st.rerun()
                                        except Exception as ex:
                                            handle_api_error(ex, "接受失败")
                                with btn_c2:
                                    if st.button("忽略 (Reject)", key=f"rej_cand_{c_id}"):
                                        try:
                                            client.reject_reconciliation_candidate(c_id, reason="用户手动忽略")
                                            st.success("已忽略该候选")
                                            st.rerun()
                                        except Exception as ex:
                                            handle_api_error(ex, "忽略失败")
                                st.divider()

                    if resolved_cands:
                        with st.expander(f"✅ 已解决候选 (Resolved Candidates) ({len(resolved_cands)})", expanded=False):
                            for rc in resolved_cands:
                                r_sl = rc.get("statement_line") or {}
                                st.write(f"- `{rc.get('status')}` | {r_sl.get('transaction_on')} | {r_sl.get('description')} | {r_sl.get('currency')} {r_sl.get('amount')}")

                    if rejected_cands:
                        with st.expander(f"🚫 已忽略候选 (Rejected Candidates) ({len(rejected_cands)})", expanded=False):
                            for rjc in rejected_cands:
                                rj_sl = rjc.get("statement_line") or {}
                                st.write(f"- `{rjc.get('status')}` | {rj_sl.get('transaction_on')} | {rj_sl.get('description')} | {rj_sl.get('currency')} {rj_sl.get('amount')}")

                # Item 5: Concurrency Safe Batch Commit using preview["batch"]["row_version"]
                batch_row_ver = batch_info.get("row_version", 0)
                ready_to_commit = is_batch_ready_to_commit(preview)
                if ready_to_commit:
                    if st.button("🚀 原子提交该对账批次 (Commit Ledger)", type="primary"):
                        try:
                            commit_res = client.commit_reconciliation_batch(active_batch_id, row_version=batch_row_ver)
                            if commit_res.get("status") == "committed":
                                st.success("🎉 对账批次已原子提交入账！")
                                st.session_state.pop("active_batch_id", None)
                                st.rerun()
                            elif commit_res.get("status") == "needs_review":
                                st.warning("⚠️ 尚有未解决的待复核项，无法直接提交入账。")
                        except Exception as ex:
                            handle_api_error(ex, "对账批次提交失败")
                else:
                    st.warning(f"⚠️ 尚有待复核候选未完成决策，请先完成决策后再提交入账。")
                    st.button("🚀 原子提交该对账批次 (Commit Ledger)", type="primary", disabled=True)

            except Exception as e:
                handle_api_error(e, "加载对账批次失败")

    with tab_manual:
        st.subheader("🎯 提交账户当前余额快照")
        st.caption("适合日常定期手动核对借记卡、钱包或储蓄账户余额。")
        if active_accounts:
            m_acc_opts = {f"{a['name']} ({a['currency']})": a for a in active_accounts}
            with st.form("manual_snap_page_form", clear_on_submit=True):
                s_acc_label = st.selectbox("选择账户", options=list(m_acc_opts.keys()), key="man_snap_acc")
                s_balance = st.number_input("账户当前真实余额", value=0.0, step=100.0, format="%.2f", key="man_snap_bal")
                s_as_of = st.date_input("基准日期", value=get_dashboard_today(), key="man_snap_date")
                s_submit = st.form_submit_button("提交余额并校准")

                if s_submit:
                    target_a = m_acc_opts[s_acc_label]
                    try:
                        iso_as_of = format_iso_timestamp(s_as_of)
                        res = client.create_account_snapshot(
                            account_id=target_a["id"],
                            balance=Decimal(str(s_balance)),
                            as_of=iso_as_of,
                            currency=target_a["currency"]
                        )
                        if res.get("status") == "committed":
                            st.success(f"🎉 校准成功！{target_a['name']} 当前余额已确认为 {target_a['currency']} {s_balance:,.2f}")
                        elif res.get("status") == "needs_review":
                            st.warning("⚠️ 余额差异过大（超过200元），已创建待复核工单。请前往「待办工单中心」进行确认。")
                    except Exception as ex:
                        handle_api_error(ex, "提交校准失败")


# ==============================================================================
# 页面 5: 📥 待办工单中心
# ==============================================================================
elif menu == "📥 待办工单中心":
    st.title("📥 待办工单中心 (Work Queue)")
    st.markdown("集中处理需要人工确认的记账草稿 (Shortcut) 及需要人工复核的对账批次")

    try:
        wq = client.get_work_queue()
        items = wq.get("items", [])
    except Exception as e:
        handle_api_error(e, "获取待办工单失败")
        items = []

    if not items:
        st.info("🎉 当前家庭没有待处理的待办工单！工作队列已清空。")
    else:
        st.markdown(f"**待处理工单数量: {len(items)}**")
        for item in items:
            w_type = item.get("work_type")
            w_id = item.get("id")
            w_status = item.get("status")
            w_summary = item.get("summary")

            with st.expander(f"📌 [{w_type.upper()}] {w_summary}", expanded=True):
                st.write(f"**工单 ID**: `{w_id}` | **状态**: `{w_status}`")

                if w_type == "ingestion":
                    st.caption("该笔快捷指令截图识别置信度不足或存在模糊项，请复核后确认:")
                    col_btn1, col_btn2 = st.columns(2)
                    with col_btn1:
                        if st.button("✅ 确认入账 (Confirm)", key=f"conf_{w_id}"):
                            try:
                                client.confirm_ingestion_request(w_id)
                                st.success("草稿已成功确认入账！")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "确认失败")
                    with col_btn2:
                        if st.button("❌ 废弃草稿 (Reject)", key=f"rej_ing_{w_id}"):
                            try:
                                client.reject_ingestion_request(w_id, reason="用户手动废弃")
                                st.success("草稿已废弃")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "废弃失败")

                elif w_type == "reconciliation":
                    st.caption("该对账批次存在待复核的模糊明细或较大差额:")
                    if st.button("🔍 打开对账复核详情", key=f"open_batch_{w_id}"):
                        st.session_state["active_batch_id"] = w_id
                        st.info(f"已选定批次 {w_id}，请切换至「⚖️ 账户校准与对账」页面进行详细复核。")


# ==============================================================================
# 页面 6: 📋 交易明细与纠错/作废
# ==============================================================================
elif menu == "📋 交易明细与纠错/作废":
    st.title("📋 交易明细与历史变更中心")
    st.markdown("查询历史交易记录，支持通过后端权威事务进行交易纠错 (Correction Preview & Commit) 及交易作废 (Void)")

    # 筛选栏
    c_f1, c_f2, c_f3 = st.columns(3)
    with c_f1:
        tx_type_filter = st.selectbox("交易类型", ["全部", "expense", "cash_income", "refund", "transfer", "fee", "reconciliation_adjustment", "opening_balance"])
    with c_f2:
        verif_filter = st.selectbox("核对状态", ["全部", "unverified", "user_confirmed", "statement_confirmed"])
    with c_f3:
        query_limit = st.slider("每页数量", min_value=10, max_value=100, value=50)

    try:
        tx_res = client.list_transactions(
            transaction_type=None if tx_type_filter == "全部" else tx_type_filter,
            verification_status=None if verif_filter == "全部" else verif_filter,
            limit=query_limit
        )
        tx_list = tx_res.get("items", [])
    except Exception as e:
        handle_api_error(e, "加载交易记录失败")
        tx_list = []

    if tx_list:
        df_display = []
        for t in tx_list:
            df_display.append({
                "交易ID": t["id"],
                "日期": t.get("occurred_on"),
                "类型": t.get("transaction_type"),
                "商户": t.get("merchant", "-"),
                "原始金额": f"{t.get('original_currency')} {t.get('original_amount')}",
                "扣款金额": f"{t.get('from_currency', '')} {t.get('from_amount', '')}" if t.get("from_amount") else "-",
                "分类": t.get("category", {}).get("name") if t.get("category") else "-",
                "版本": t.get("row_version", 0),
                "状态": t.get("status"),
                "核对": t.get("verification_status")
            })
        st.dataframe(pd.DataFrame(df_display), use_container_width=True, hide_index=True)

        st.divider()

        # 交易操作与纠错工作流
        st.subheader("🛠️ 交易纠错与作废操作")
        selected_tx_id = st.selectbox("选择要操作的交易 ID", options=[t["id"] for t in tx_list])
        target_tx = next((t for t in tx_list if t["id"] == selected_tx_id), None)

        if target_tx:
            st.write(f"当前选中: **{target_tx.get('merchant', '-')}** · `{target_tx.get('original_currency')} {target_tx.get('original_amount')}` · 状态: `{target_tx.get('status')}` · 版本: `{target_tx.get('row_version', 0)}`")

            tab_corr, tab_void, tab_ref = st.tabs(["✏️ 交易变更与纠错 (Correction)", "🗑️ 交易作废 (Void)", "🔄 录入退款 (Refund)"])

            with tab_corr:
                st.caption("对于已入账或已对账的历史交易，通过变更预览与原子提交更新商户、金额或分类:")
                with st.form("corr_preview_form"):
                    new_merchant = st.text_input("修改商户名称", value=target_tx.get("merchant") or "")
                    new_amt = st.text_input("修改扣款金额 (from_amount)", value=target_tx.get("from_amount") or target_tx.get("original_amount") or "")
                    new_remarks = st.text_input("修改备注", value=target_tx.get("remarks") or "")
                    btn_prev = st.form_submit_button("1. 预览变更影响 (Preview)")

                    if btn_prev:
                        try:
                            preview_payload = {}
                            if new_merchant.strip():
                                preview_payload["merchant"] = new_merchant.strip()
                            if new_remarks.strip():
                                preview_payload["remarks"] = new_remarks.strip()
                            if new_amt.strip():
                                preview_payload["from_amount"] = Decimal(new_amt.strip())

                            prev_res = client.preview_transaction_correction(target_tx["id"], preview_payload)
                            st.session_state["active_corr_preview"] = prev_res
                            st.session_state["active_corr_changes"] = {
                                k: str(v) if isinstance(v, Decimal) else v
                                for k, v in preview_payload.items()
                            }
                            st.success("✅ 变更预览成功，请在下方确认账户余额投影影响！")
                        except Exception as ex:
                            handle_api_error(ex, "预览变更失败")

                # 显示预览结果与确认提交
                active_preview = st.session_state.get("active_corr_preview")
                if active_preview and active_preview.get("transaction_id") == target_tx["id"]:
                    st.info(f"预期版本号: `{active_preview.get('expected_version')}` | 是否为对账确认交易: `{active_preview.get('is_statement_confirmed')}`")
                    deltas = active_preview.get("account_state_deltas", [])
                    if deltas:
                        st.markdown("**账户余额变动影响 (Account State Deltas):**")
                        for d in deltas:
                            st.write(f"- 账户 **{d['account_name']}**: 当前 `{d['current_balance']}` ➔ 调整 `{d['delta']}` ➔ 变更后预计 `{d['projected_balance']}`")

                    corr_reason = st.text_input("变更原因 (必填)", value="修正录入差错", key="corr_reason_input")
                    if st.button("2. 确认提交变更 (Commit Correction)", type="primary"):
                        try:
                            changes = st.session_state.get("active_corr_changes", {})
                            client.commit_transaction_correction(
                                transaction_id=target_tx["id"],
                                expected_version=active_preview.get("expected_version"),
                                changes=changes,
                                reason=corr_reason
                            )
                            st.success("🎉 交易变更已原子提交入账！")
                            st.session_state.pop("active_corr_preview", None)
                            st.session_state.pop("active_corr_changes", None)
                            st.rerun()
                        except Exception as ex:
                            handle_api_error(ex, "提交变更失败")

            with tab_void:
                st.caption("作废交易将原子反向冲销账户余额投影，记录不可变审计日志，并软删除流水 (需要乐观锁版本号):")
                with st.form("void_form"):
                    void_reason = st.text_input("作废原因 (必填)", value="重复录入 / 错误流水")
                    btn_void = st.form_submit_button("⚠️ 确认作废此交易", type="secondary")

                    if btn_void:
                        if not void_reason.strip():
                            st.error("必须填写作废原因！")
                        else:
                            try:
                                v_res = client.void_transaction(
                                    transaction_id=target_tx["id"],
                                    delete_reason=void_reason.strip(),
                                    expected_version=target_tx.get("row_version", 0)
                                )
                                st.success(f"🎉 交易已作废！账户余额已恢复: {v_res.get('account_balance_restored')}")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "交易作废失败")

            with tab_ref:
                st.caption("退款作为独立流水关联至原消费，不删除原流水:")
                with st.form("refund_form"):
                    ref_amt = st.number_input("退款金额", value=float(Decimal(str(target_tx.get("original_amount") or "0.00"))), min_value=0.01, step=10.0, format="%.2f")
                    ref_acc_id = st.selectbox(
                        "接收退款的账户",
                        options=[a["id"] for a in active_accounts],
                        format_func=lambda x: next((a["name"] for a in active_accounts if a["id"] == x), x)
                    )
                    ref_date = st.date_input("退款入账日期", value=get_dashboard_today())
                    ref_remarks = st.text_input("退款备注", value="商品售后退款")
                    btn_ref = st.form_submit_button("提交退款")

                    if btn_ref:
                        try:
                            client.refund_transaction(
                                transaction_id=target_tx["id"],
                                amount=Decimal(str(ref_amt)),
                                currency=target_tx.get("original_currency", "CNY"),
                                to_account_id=ref_acc_id,
                                occurred_on=ref_date.isoformat(),
                                remarks=ref_remarks
                            )
                            st.success("🎉 退款流水已生成并入账！")
                            st.rerun()
                        except Exception as ex:
                            handle_api_error(ex, "录入退款失败")
    else:
        st.info("暂无交易记录。")


# ==============================================================================
# 页面 7: ⚙️ 账户与分类管理 (Item 12: Full parity for edit / deactivate)
# ==============================================================================
elif menu == "⚙️ 账户与分类管理":
    st.title("⚙️ 账户与分类管理")
    st.markdown("通过后端 REST API 维护家庭金融账户、账户别名及收支分类")

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
                bal = a.get("state", {}).get("ledger_balance", "0.00")
                acc_df.append({
                    "账户ID": a["id"],
                    "账户名称": a["name"],
                    "机构": a.get("institution", "-"),
                    "类型": a["account_type"],
                    "币种": a["currency"],
                    "账面余额": f"{a['currency']} {Decimal(str(bal)):,.2f}",
                    "状态": a.get("status", "active")
                })
            st.dataframe(pd.DataFrame(acc_df), use_container_width=True, hide_index=True)

        st.divider()

        # 2. 编辑或停用已有账户 (Item 12)
        if acc_list:
            st.subheader("2. 编辑或停用已有账户")
            sel_edit_acc_id = st.selectbox(
                "选择要修改的账户",
                options=[a["id"] for a in acc_list],
                format_func=lambda x: next((f"{a['name']} ({a['status']})" for a in acc_list if a["id"] == x), x)
            )
            target_edit_acc = next((a for a in acc_list if a["id"] == sel_edit_acc_id), None)

            if target_edit_acc:
                col_e1, col_e2 = st.columns([3, 1])
                with col_e1:
                    with st.form("edit_account_form"):
                        e_name = st.text_input("修改账户名称", value=target_edit_acc["name"])
                        e_billing = st.number_input("修改账单日", min_value=1, max_value=31, value=int(target_edit_acc.get("billing_day") or 1)) if target_edit_acc["account_type"] == "credit" else None
                        e_due = st.number_input("修改还款日", min_value=1, max_value=31, value=int(target_edit_acc.get("due_day") or 25)) if target_edit_acc["account_type"] == "credit" else None
                        btn_update_acc = st.form_submit_button("保存账户修改")

                        if btn_update_acc:
                            payload: Dict[str, Any] = {}
                            if e_name.strip() and e_name.strip() != target_edit_acc["name"]:
                                payload["name"] = e_name.strip()
                            if e_billing is not None:
                                payload["billing_day"] = int(e_billing)
                            if e_due is not None:
                                payload["due_day"] = int(e_due)

                            if payload:
                                try:
                                    client.update_account(target_edit_acc["id"], payload)
                                    st.success("🎉 账户信息已更新！")
                                    st.rerun()
                                except Exception as ex:
                                    handle_api_error(ex, "更新账户失败")
                with col_e2:
                    st.write("**账户状态操作**")
                    if target_edit_acc.get("status") == "active":
                        if st.button("⛔ 停用该账户", key=f"deact_acc_{target_edit_acc['id']}"):
                            try:
                                client.deactivate_account(target_edit_acc["id"])
                                st.success("账户已成功停用")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "停用失败")
                    else:
                        st.info("已处于停用状态")

        st.divider()

        # 3. 创建新账户
        st.subheader("3. 创建新账户")
        with st.form("create_account_form", clear_on_submit=True):
            n_name = st.text_input("账户名称 (如: 工行信用卡)")
            n_inst = st.text_input("金融机构 (如: ICBC)")
            n_type = st.selectbox("账户类型", ["cash", "savings", "credit", "investment"])
            n_curr = st.selectbox("币种", ["CNY", "USD", "EUR", "JPY", "SGD", "HKD", "GBP"])
            n_billing = st.number_input("账单日 (信用卡选填)", min_value=1, max_value=31, value=1) if n_type == "credit" else None
            n_due = st.number_input("还款日 (信用卡选填)", min_value=1, max_value=31, value=25) if n_type == "credit" else None
            btn_create_acc = st.form_submit_button("创建账户")

            if btn_create_acc:
                if not n_name.strip() or not n_inst.strip():
                    st.error("账户名称和金融机构为必填项！")
                else:
                    try:
                        client.create_account(
                            name=n_name.strip(),
                            institution=n_inst.strip(),
                            account_type=n_type,
                            currency=n_curr,
                            billing_day=int(n_billing) if n_billing else None,
                            due_day=int(n_due) if n_due else None
                        )
                        st.success(f"🎉 账户 {n_name} 创建成功！")
                        st.rerun()
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
                            st.write(f"- `{al['alias']}`")
                        with al_col2:
                            if st.button("删除", key=f"del_al_{al['id']}"):
                                client.delete_account_alias(sel_alias_acc, al["id"])
                                st.success("别名已删除")
                                st.rerun()
                else:
                    st.caption("该账户暂无别名。")
            except Exception as e:
                handle_api_error(e, "加载别名失败")

            with st.form("add_alias_form", clear_on_submit=True):
                new_alias_str = st.text_input("新增别名 (如: 工行Visa卡)")
                if st.form_submit_button("添加别名") and new_alias_str.strip():
                    try:
                        client.create_account_alias(sel_alias_acc, new_alias_str.strip())
                        st.success("别名添加成功！")
                        st.rerun()
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

        # 编辑或停用已有分类 (Item 12)
        if cats:
            st.subheader("编辑或停用分类")
            sel_edit_cat_id = st.selectbox(
                "选择分类",
                options=[c["id"] for c in cats],
                format_func=lambda x: next((f"{c['name']} ({c['category_type']}, {c['status']})" for c in cats if c["id"] == x), x)
            )
            target_edit_cat = next((c for c in cats if c["id"] == sel_edit_cat_id), None)
            if target_edit_cat:
                c_edit1, c_edit2 = st.columns([3, 1])
                with c_edit1:
                    with st.form("edit_cat_form"):
                        new_cat_name = st.text_input("分类名称", value=target_edit_cat["name"])
                        btn_update_cat = st.form_submit_button("重命名分类")
                        if btn_update_cat and new_cat_name.strip() and new_cat_name.strip() != target_edit_cat["name"]:
                            try:
                                client.update_category(target_edit_cat["id"], new_cat_name.strip())
                                st.success("分类名称已更新！")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "重命名分类失败")
                with c_edit2:
                    if target_edit_cat.get("status") == "active":
                        if st.button("⛔ 停用分类", key=f"deact_cat_{target_edit_cat['id']}"):
                            try:
                                client.deactivate_category(target_edit_cat["id"])
                                st.success("分类已停用")
                                st.rerun()
                            except Exception as ex:
                                handle_api_error(ex, "停用分类失败")
                    else:
                        st.info("已停用")

        st.divider()
        st.subheader("创建新分类")
        with st.form("create_cat_form", clear_on_submit=True):
            cat_name = st.text_input("分类名称 (如: 餐饮美食, 数码科技)")
            cat_type = st.selectbox("分类性质", ["expense", "income"])
            if st.form_submit_button("创建分类") and cat_name.strip():
                try:
                    client.create_category(name=cat_name.strip(), category_type=cat_type)
                    st.success(f"分类 {cat_name} 创建成功！")
                    st.rerun()
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