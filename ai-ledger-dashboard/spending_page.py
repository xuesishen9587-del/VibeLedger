"""S2 Spending and saved-metadata Review; REST-only presentation."""
from datetime import date
from decimal import Decimal
import streamlit as st
from spending_controller import SpendingActions
from time_utils import get_dashboard_today


def render(client, review_only=False):
    st.title("支出复核" if review_only else "支出")
    actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)
    for slot in list(actions.state):
        st.warning("有一次保存结果尚未确定。重试会使用原请求，不会重复记账。")
        if st.button("重试上次保存", key="retry_" + slot):
            try:
                actions.retry(slot)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    def save(slot, method, path, body):
        try:
            result = actions.execute(slot, method, path, body)
            st.success("已保存")
            return result
        except Exception as exc:
            st.error(str(exc))
            return None

    # Catch-up is a separate Dashboard command. Its outcome stays visible on failure.
    try:
        result = actions.execute("catch_up", "POST", "/api/v1/spending-schedules/materialize", {})
        st.caption("月度计划已补记至 " + result["schedules_current_through"])
    except Exception as exc:
        st.warning("月度计划补记未完成，支出可能不完整：" + str(exc))
    try:
        accounts = client.request("GET", "/api/v1/accounts", params={"status": "active"})["items"]
        categories = client.request("GET", "/api/v1/categories", params={"status": "active"})["items"]
        expense_categories = [c for c in categories if c.get("category_type", c.get("type")) == "expense"]
        counts = client.request("GET", "/api/v1/review")["counts"]
    except Exception as exc:
        st.error(str(exc))
        return
    st.caption(f"待修正记录 {counts['transactions']} · 缺失账户 {counts['missing_account']} · 分类不确定 {counts['category_uncertain']} · 计划待决定 {counts['schedule_occurrences']}")
    account_names = {None: "账户未知", **{a["id"]: a["name"] for a in accounts}}
    category_names = {c["id"]: c["name"] for c in categories}
    if counts["drafts"] and review_only:
        from capture_review import render as render_capture_review
        render_capture_review(client, save, account_names, expense_categories)
    elif counts["drafts"]:
        st.info(f"另有 {counts['drafts']} 条截图草稿，请进入支出复核页面处理。")
    if counts["schedule_occurrences"]:
        with st.expander("计划待决定的期间", expanded=review_only):
            pending = client.request("GET", "/api/v1/review", params={"section": "schedule", "limit": 200})
            for occurrence in pending["items"]:
                st.write(occurrence["due_on"], occurrence["amount"], occurrence["currency"])
                matches = client.request("GET", "/api/v1/transactions", params={
                    "from": occurrence["due_on"], "to": occurrence["due_on"],
                    "transaction_type": "expense", "limit": 200})
                candidates = {r["id"]: r for r in matches["items"]
                              if not r.get("schedule_occurrence_id")
                              and r["original_currency"] == occurrence["currency"]
                              and Decimal(r["original_amount"]) == Decimal(occurrence["amount"])}
                with st.form("period_" + occurrence["id"]):
                    action = st.selectbox("处理方式", ["link_existing", "record_separately", "skip"],
                        format_func=lambda v: {"link_existing": "关联已记录支出", "record_separately": "确认是另一笔支出", "skip": "跳过本期"}[v])
                    target = st.selectbox("同日同金额的已记录支出", [None, *candidates],
                        format_func=lambda key: "请选择" if key is None else
                        f"{candidates[key].get('merchant') or '未填商户'} · {candidates[key]['original_amount']} {candidates[key]['original_currency']} · {key[:8]}")
                    if matches["next_cursor"]:
                        st.caption("当天记录超过 200 条，可在支出列表找到记录并填写下方 ID。")
                    manual_target = st.text_input("其他关联支出 ID（可留空）")
                    if st.form_submit_button("保存本期决定"):
                        body = {"expected_version": occurrence["row_version"], "action": action}
                        try:
                            if action == "link_existing":
                                linked = candidates.get(target)
                                if manual_target:
                                    linked = client.request("GET", "/api/v1/transactions/" + manual_target)
                                if not linked:
                                    st.error("请先选择已记录支出。")
                                    continue
                                body.update(transaction_id=linked["id"], expected_transaction_version=linked["row_version"])
                            if save("period_" + occurrence["id"], "POST", f"/api/v1/schedule-occurrences/{occurrence['id']}/resolve", body):
                                st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
            if pending["next_cursor"]:
                st.caption("还有待决定期间；处理当前记录后刷新以继续。")

    if not review_only:
        from spending_report_view import render as render_report
        render_report(client, actions, category_names)
        with st.expander("记录支出／退款／收入"):
            with st.form("spending_create"):
                kind = st.selectbox("类型", ["expense", "refund", "cash_income"])
                day = st.date_input("发生日期", value=get_dashboard_today())
                amount = st.text_input("金额", "0.00")
                currency = st.selectbox("币种", ["CNY", "SGD", "USD", "EUR", "JPY"])
                category = st.selectbox("分类", list(category_names), format_func=category_names.get)
                account = st.selectbox("账户", list(account_names), format_func=account_names.get)
                merchant = st.text_input("商户")
                remarks = st.text_input("备注（未关联退款必填）")
                refund_id = st.text_input("关联原支出 ID（仅退款，可留空）")
                if st.form_submit_button("保存记录"):
                    result = save("create_transaction", "POST", "/api/v1/transactions", {
                        "transaction_type": kind, "occurred_on": str(day), "original_amount": amount,
                        "original_currency": currency, "category_id": category, "account_id": account,
                        "merchant": merchant or None, "remarks": remarks or None,
                        "refund_of_transaction_id": refund_id or None})
                    if result:
                        st.rerun()
        _schedules(client, save, expense_categories, account_names)

    choice = st.selectbox("筛选", ["待修正", "账户缺失", "分类不确定", "全部"] if review_only else ["全部", "待修正", "账户缺失", "分类不确定"])
    filters = {"待修正": {"needs_metadata_review": True}, "账户缺失": {"missing_account": True},
               "分类不确定": {"category_uncertain": True}, "全部": {}}
    page_key = "review_cursor" if review_only else "spending_cursor"
    if st.session_state.get(page_key + "_filter") != choice:
        st.session_state[page_key] = None
        st.session_state[page_key + "_filter"] = choice
    response = client.request("GET", "/api/v1/transactions", params={**filters[choice], "limit": 50, "cursor": st.session_state.get(page_key)})
    for row in response["items"]:
        badges = " · ".join({"MISSING_ACCOUNT": "账户未知", "CATEGORY_UNCERTAIN": "分类不确定"}[r] for r in row["review_reasons"])
        with st.expander(f"{row['occurred_on']} · {row['original_amount']} {row['original_currency']} · {row.get('merchant') or '支出'} · {badges}"):
            st.caption("已保存并计入支出 · " + row["id"])
            if st.button("查看修改历史", key="history_" + row["id"]):
                history = client.request("GET", "/api/v1/history", params={"entity_type": "transaction", "entity_id": row["id"]})
                st.write(history["items"])
            account_options = dict(account_names)
            category_options = dict(category_names)
            if row.get("account_id") not in account_options:
                account_options[row["account_id"]] = "原账户（已停用）"
            category_options.setdefault(row["category_id"], "原分类（已停用）")
            with st.form("metadata_" + row["id"]):
                account = st.selectbox("付款账户", list(account_options), index=list(account_options).index(row["account_id"]), format_func=account_options.get)
                category = st.selectbox("分类", list(category_options), index=list(category_options).index(row["category_id"]), format_func=category_options.get)
                confirm_category = st.checkbox("确认当前分类（包括 Other）", value=False)
                acknowledge = st.checkbox("暂时保留账户未知", value=row["account_review_acknowledged"])
                if st.form_submit_button("保存修正"):
                    body = {"expected_version": row["row_version"]}
                    if account != row["account_id"]:
                        body["account_id"] = account
                    if category != row["category_id"] or confirm_category:
                        body["category_id"] = category
                    if acknowledge != row["account_review_acknowledged"]:
                        body["account_review_acknowledged"] = acknowledge
                    if len(body) > 1 and save("metadata_" + row["id"], "PATCH", "/api/v1/transactions/" + row["id"], body):
                        st.rerun()
            with st.form("void_" + row["id"]):
                reason = st.text_input("作废原因")
                if st.form_submit_button("作废此记录") and save("void_" + row["id"], "POST", "/api/v1/transactions/" + row["id"] + "/void", {"expected_version": row["row_version"], "delete_reason": reason}):
                    st.rerun()
            with st.form("amount_" + row["id"]):
                changed_amount = st.text_input("修正金额", row["original_amount"])
                changed_day = st.date_input("修正日期", date.fromisoformat(row["occurred_on"]))
                if st.form_submit_button("保存金额／日期"):
                    body = {"expected_version": row["row_version"], "original_amount": changed_amount, "occurred_on": str(changed_day)}
                    if save("amount_" + row["id"], "PATCH", "/api/v1/transactions/" + row["id"], body):
                        st.rerun()
    if response["next_cursor"] and st.button("下一页"):
        st.session_state[page_key] = response["next_cursor"]
        st.rerun()
    if st.session_state.get(page_key) and st.button("返回第一页"):
        st.session_state[page_key] = None
        st.rerun()


def _schedules(client, save, categories, accounts, source_draft=None):
    suffix = "_" + source_draft["request_id"] if source_draft else ""
    preview_key = "spending_schedule_preview" + suffix
    with st.expander("月度循环／分期计划"):
        if not categories:
            st.info("请先在设置中添加支出分类。")
            return
        names = {c["id"]: c["name"] for c in categories}
        with st.form("new_spending_schedule" + suffix):
            name = st.text_input("计划名称")
            kind = st.selectbox("计划类型", ["installment"] if source_draft else ["installment", "recurring"])
            amount = st.text_input("每期金额", "0.00")
            currency = st.selectbox("计划币种", ["CNY", "SGD", "USD", "EUR", "JPY"])
            count = st.number_input("期数（循环支出可用 0 表示持续）", min_value=0, max_value=1200, value=12, step=1)
            start = st.date_input("起始月份", value=get_dashboard_today().replace(day=1))
            day = st.number_input("每月几号", min_value=1, max_value=31, value=1, step=1)
            merchant = st.text_input("计划商户")
            category = st.selectbox("计划分类", list(names), format_func=names.get)
            account = st.selectbox("计划账户", list(accounts), format_func=accounts.get)
            if st.form_submit_button("预览日期和金额"):
                body = {"name": name, "kind": kind, "amount_per_period": amount, "currency": currency,
                        "period_count": count or None, "start_month": str(start.replace(day=1)),
                        "day_of_month": day, "merchant": merchant, "category_id": category, "account_id": account}
                try:
                    preview = client.request("POST", "/api/v1/spending-schedules/preview", json_data=body)
                    if source_draft:
                        body.update(source_draft_request_id=source_draft["request_id"], expected_draft_version=source_draft["row_version"])
                    st.session_state[preview_key] = (body, preview)
                except Exception as exc:
                    st.session_state.pop(preview_key, None)
                    st.error(str(exc))
        if preview_key in st.session_state:
            body, preview = st.session_state[preview_key]
            st.write(f"待保存预览：{body['name']} · 每期 {body['amount_per_period']} {body['currency']} · {body['period_count'] or '持续'} 期")
            st.write("已到期期数", len(preview["due_periods"]), "合计", preview["due_total"])
            st.dataframe(preview["due_periods"])
            st.write("后续日期", preview["upcoming_dates"])
            if st.button("保存以上预览计划", key="save_preview" + suffix):
                if save("create_schedule" + suffix, "POST", "/api/v1/spending-schedules", {**body, "acknowledged_due_through": preview["acknowledged_due_through"]}):
                    st.session_state.pop(preview_key, None)
                    st.rerun()
        if source_draft:
            return
        response = client.request("GET", "/api/v1/spending-schedules", params={"cursor": st.session_state.get("schedule_cursor")})
        schedules = response["items"]
        if response["next_cursor"] and st.button("下一页计划"):
            st.session_state["schedule_cursor"] = response["next_cursor"]
            st.rerun()
        if st.session_state.get("schedule_cursor") and st.button("返回计划第一页"):
            st.session_state["schedule_cursor"] = None
            st.rerun()
        for row in schedules:
            st.write(row["name"], row["amount_per_period"], row["currency"], row["status"])
            if row["status"] in ("active", "paused"):
                with st.form("terms_" + row["id"]):
                    amount = st.text_input("后续每期金额", row["amount_per_period"])
                    count = st.number_input("总期数（循环计划 0 为持续）", min_value=0, max_value=1200, value=row["period_count"] or 0, step=1)
                    if st.form_submit_button("保存后续计划变更"):
                        if save("terms_" + row["id"], "PATCH", f"/api/v1/spending-schedules/{row['id']}",
                            {"expected_version": row["row_version"], "amount_per_period": amount, "period_count": count or None}):
                            st.rerun()
                action = "pause" if row["status"] == "active" else "resume"
                if st.button("暂停" if action == "pause" else "恢复", key=action + row["id"]):
                    if save(action + row["id"], "POST", f"/api/v1/spending-schedules/{row['id']}/{action}", {"expected_version": row["row_version"]}):
                        st.rerun()
                if st.button("停止计划", key="cancel" + row["id"]):
                    if save("cancel" + row["id"], "POST", f"/api/v1/spending-schedules/{row['id']}/cancel", {"expected_version": row["row_version"]}):
                        st.rerun()
