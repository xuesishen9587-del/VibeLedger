"""Review unsaved screenshot drafts separately from already-saved metadata."""
from datetime import date
import streamlit as st


def render(client, save, accounts, categories):
    st.subheader("待保存的截图")
    cursor = st.session_state.get("capture_review_cursor")
    page = client.request("GET", "/api/v1/ingestion-requests", params={"limit": 50, "cursor": cursor})
    drafts = [item for item in page["items"] if "intent" in item.get("draft", {})]
    if not drafts:
        st.info("当前页没有待保存的支出截图。")
    else:
        lookup = {item["request_id"]: item for item in drafts}
        selected = st.selectbox("选择截图草稿", list(lookup), format_func=lambda key:
            f"{lookup[key]['draft'].get('occurred_on') or '日期待定'} · {lookup[key]['draft'].get('merchant') or '商户待定'} · {key[:8]}")
        item = lookup[selected]
        draft = item["draft"]
        base = "/api/v1/ingestion-requests/" + selected
        st.caption("尚未计入支出")
        for warning in item.get("warnings", []):
            st.warning(warning["message"])
        with st.form("capture_fields_" + selected):
            amount = st.text_input("截图金额", draft.get("original_amount") or "")
            currencies = [None, "CNY", "SGD", "USD", "EUR", "JPY"]
            currency = st.selectbox("截图币种", currencies, index=currencies.index(draft.get("original_currency")) if draft.get("original_currency") in currencies else 0)
            occurred = date.fromisoformat(draft["occurred_on"]) if draft.get("occurred_on") else None
            occurred = st.date_input("截图发生日期", value=occurred)
            merchant = st.text_input("截图商户", draft.get("merchant") or "")
            intents = ["expense", "refund", "transfer", "repayment", "failed", "pending", "unknown"]
            intent = st.selectbox("款项性质", intents, index=intents.index(draft.get("intent") or "unknown"))
            modes = [None, "one_off", "installment"]
            mode = st.selectbox("消费方式", modes, index=modes.index(draft.get("payment_mode")) if draft.get("payment_mode") in modes else 0)
            account = (draft.get("from_account") or {}).get("id")
            account = st.selectbox("截图付款账户", list(accounts), index=list(accounts).index(account) if account in accounts else 0, format_func=accounts.get)
            category_names = {None: "分类待定", **{c["id"]: c["name"] for c in categories}}
            category = (draft.get("category") or {}).get("id")
            category = st.selectbox("截图分类", list(category_names), index=list(category_names).index(category) if category in category_names else 0, format_func=category_names.get)
            if st.form_submit_button("保存草稿修正"):
                result = save("draft_" + selected, "PATCH", base + "/draft", {
                    "expected_version": item["row_version"], "original_amount": amount or None,
                    "original_currency": currency, "occurred_on": str(occurred) if occurred else None,
                    "merchant": merchant or None, "intent": intent, "payment_mode": mode,
                    "from_account_id": account, "category_id": category})
                if result:
                    st.rerun()
        with st.form("capture_note_" + selected):
            note = st.text_input("用自然语言修正")
            if st.form_submit_button("修改草稿") and note:
                if save("revise_" + selected, "POST", base + "/revise", {"expected_version": item["row_version"], "correction_note": note}):
                    st.rerun()
        if draft.get("payment_mode") == "installment":
            st.info("分期截图须明确选择全额一次记录、关联已到期期间，或建立月度计划。")
            if st.button("选择按全额一次记录", key="full_" + selected):
                if save("full_" + selected, "PATCH", base + "/draft", {"expected_version": item["row_version"], "action": "record_full_purchase"}):
                    st.rerun()
            schedules = client.request("GET", "/api/v1/spending-schedules", params={"limit": 200})["items"]
            if schedules:
                choices = {s["id"]: s for s in schedules}
                with st.form("bind_" + selected):
                    schedule_id = st.selectbox("关联已有计划", list(choices), format_func=lambda key: choices[key]["name"])
                    period = st.number_input("关联第几期", min_value=1, step=1)
                    if st.form_submit_button("选择此期间"):
                        if save("bind_" + selected, "PATCH", base + "/draft", {"expected_version": item["row_version"],
                            "action": "use_schedule_period", "schedule_id": schedule_id, "period_no": period,
                            "expected_schedule_version": choices[schedule_id]["row_version"]}):
                            st.rerun()
            from spending_page import _schedules
            _schedules(client, save, categories, accounts, source_draft=item)
        if st.button("确认保存这笔支出", key="confirm_" + selected):
            result = save("confirm_" + selected, "POST", base + "/confirm", {"expected_version": item["row_version"]})
            if result:
                st.rerun()
        if st.button("放弃此截图", key="reject_" + selected):
            if save("reject_" + selected, "POST", base + "/reject", {"expected_version": item["row_version"]}):
                st.rerun()
    if page["next_cursor"] and st.button("下一页截图"):
        st.session_state["capture_review_cursor"] = page["next_cursor"]
        st.rerun()
    if cursor and st.button("返回截图第一页"):
        st.session_state["capture_review_cursor"] = None
        st.rerun()
