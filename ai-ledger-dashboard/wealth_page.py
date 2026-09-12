"""S3 wealth and independent balance entry through REST."""
import base64
from datetime import date
from decimal import Decimal
from uuid import uuid4
import streamlit as st
from spending_controller import SpendingActions
from time_utils import format_iso_timestamp, get_dashboard_today
from wealth_history_view import render as render_wealth_history


def render(client):
    st.title("最近报告的家庭财富")
    st.caption("余额来自各账户最近一次观察；支出不会自动改变余额。")
    actions=SpendingActions(st.session_state.setdefault("_wealth_actions",{}),client)
    def save(slot,path,body):
        try:
            return actions.execute(slot,"POST",path,body)
        except Exception as exc:
            st.error(str(exc))
            return None
    for slot in list(actions.state):
        st.warning("上次保存结果尚未确定。")
        if st.button("重试原请求",key="wealth_retry_"+slot):
            try:
                actions.retry(slot)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    report=client.request("GET","/api/v1/reports/wealth")
    if report["setup_required"]:
        st.info("请先在设置中添加覆盖范围互不重叠的账户。")
    else:
        complete=report["coverage"]["complete"]
        if not complete:
            st.warning("缺少余额或汇率，以下为已知部分，不是完整家庭财富。")
        for name,label in (("assets","资产"),("liabilities","负债"),("net_worth","净资产")):
            key=(name if name=="net_worth" else "total_"+name) if complete else "known_"+name
            st.metric(("已知" if not complete else "")+label,f"{report[key]} {report['reporting_currency']}")
        st.write("账户观察日期",report["coverage"]["oldest_observation_at"],"至",report["coverage"]["newest_observation_at"])
        if report["coverage"]["stale_account_ids"]:
            st.warning("部分账户超过 30 天未更新；这些旧余额仍包含在已知金额中。")
        if report["coverage"]["stale_fx_currencies"]:
            st.warning("部分折算使用超过 7 天的旧汇率。")
        st.dataframe([{"账户":r["name"],"原币余额":r["balance"],"币种":r["currency"],"观察时间":r["as_of"],
            "折算金额":r["converted_amount"],"汇率日期":r["fx_as_of"],"距今天数":r["age_days"]} for r in report["accounts"]])
        st.write("正资产风险分布（负债不进入分母）")
        st.dataframe([{"风险":r["risk_level"],"金额":r["amount"],"占已知正资产比例 %":r["percentage"]} for r in report["risk_buckets"]])
    missing=report["coverage"]["missing_fx_currencies"] or report["coverage"]["stale_fx_currencies"]
    auto=missing and st.session_state.get("_wealth_fx_day")!=str(get_dashboard_today())
    if st.button("更新参考汇率") or auto:
        st.session_state["_wealth_fx_day"]=str(get_dashboard_today())
        if save("wealth_fx","/api/v1/reports/refresh-fx",{}):
            st.rerun()
    render_wealth_history(client)
    accounts=client.request("GET","/api/v1/accounts",params={"status":"active"})["items"]
    if accounts:
        names={a["id"]:a for a in accounts}
        with st.expander("手动更新余额"):
            selected=st.multiselect("本次更新哪些账户",list(names),format_func=lambda key:names[key]["name"])
            with st.form("balance_update"):
                rows=[]
                for key in selected:
                    a=names[key]
                    label="欠款金额（正数）" if a["account_type"]=="credit" else "余额（可为零）"
                    value=st.text_input(a["name"]+" · "+label,key="balance_"+key)
                    surplus=st.checkbox("这是信用卡溢缴款",key="surplus_"+key) if a["account_type"]=="credit" else False
                    timestamp=st.text_input(a["name"]+" · 观察时间（含时区）",format_iso_timestamp(),key="balance_time_"+key)
                    rows.append((a,value,surplus,timestamp))
                if st.form_submit_button("保存本次全部余额"):
                    try:
                        observations=[]
                        for a,value,surplus,timestamp in rows:
                            amount=Decimal(value)
                            if a["account_type"]=="credit":
                                if not amount.is_finite() or amount<0:
                                    raise ValueError("请输入正数欠款或溢缴款。")
                                amount=amount if surplus else -amount
                            observations.append({"account_id":a["id"],"balance":str(amount),"currency":a["currency"],"as_of":timestamp,
                                "time_basis":"explicit","expected_account_version":a["row_version"],
                                "expected_latest_snapshot_id":(a.get("latest_snapshot") or {}).get("id")})
                        if save("balances","/api/v1/balance-updates",{"observations":observations}):
                            st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        with st.expander("截图更新余额"):
            upload=st.file_uploader("账户概览截图",type=["png","jpg","jpeg"],key="balance_image")
            if st.button("识别余额截图") and upload:
                content=upload.getvalue()
                result=save("balance_capture","/api/v1/balance-captures",{"idempotency_key":str(uuid4()),"captured_at":format_iso_timestamp(),
                    "image":{"mime_type":upload.type,"base64":base64.b64encode(content).decode()}})
                if result:
                    st.info(result["display_summary"])
                    if result["status"]=="needs_confirmation":
                        st.session_state["balance_draft_id"]=result["request_id"]
    render_balance_drafts(client,actions)
    with st.expander("余额历史与纠错"):
        all_accounts=client.request("GET","/api/v1/accounts")["items"]
        if all_accounts:
            mapping={a["id"]:a for a in all_accounts}
            account_id=st.selectbox("查看账户历史",list(mapping),format_func=lambda key:mapping[key]["name"])
            history=client.request("GET",f"/api/v1/accounts/{account_id}/snapshots",params={"include_voided":True,"cursor":st.session_state.get("balance_history_cursor_"+account_id)})
            for row in sorted(history["items"],key=lambda r:r["as_of"],reverse=True):
                with st.expander(f"{row['as_of']} · {row['balance']} {row['currency']} · {row['status']}"):
                    if st.button("查看修改记录",key="balance_audit_"+row["id"]):
                        st.write(client.request("GET","/api/v1/history",params={"entity_type":"account_snapshot","entity_id":row["id"]})["items"])
                    if row["status"]=="active":
                        with st.form("snapshot_"+row["id"]):
                            action=st.selectbox("操作",["correct","void"],format_func=lambda a:"纠正余额" if a=="correct" else "作废观察")
                            value=st.text_input("修正后的带符号余额（欠款为负）",row["balance"])
                            stamp=st.text_input("修正后的时间",row["as_of"])
                            reason=st.text_input("原因")
                            reopen=st.checkbox("同时重新开启已关闭账户")
                            if st.form_submit_button("保存纠错"):
                                body={"expected_version":row["row_version"],"expected_latest_snapshot_id":history["expected_latest_snapshot_id"],
                                    "expected_account_version":history["expected_account_version"],"reason":reason,"reopen_account":reopen}
                                if action=="correct":
                                    body.update(balance=value,currency=row["currency"],as_of=stamp,time_basis=row["time_basis"])
                                if save("snapshot_"+row["id"],f"/api/v1/snapshots/{row['id']}/{action}",body):
                                    st.rerun()
            if history["next_cursor"] and st.button("下一页余额历史"):
                st.session_state["balance_history_cursor_"+account_id]=history["next_cursor"]
                st.rerun()
            if st.session_state.get("balance_history_cursor_"+account_id) and st.button("余额历史第一页"):
                st.session_state["balance_history_cursor_"+account_id]=None
                st.rerun()


def render_balance_drafts(client,actions):
    page=client.request("GET","/api/v1/ingestion-requests",params={"limit":50,"request_kind":"balance_capture","cursor":st.session_state.get("balance_draft_cursor")})
    drafts=[r for r in page["items"] if "rows" in r.get("draft",{})]
    for item in drafts:
        with st.expander("待保存余额截图 · "+item["request_id"][:8],expanded=True):
            for warning in item["warnings"]:
                st.warning(warning["message"])
            accounts=client.request("GET","/api/v1/accounts",params={"status":"active"})["items"]
            mapping={None:{"name":"请选择账户"},**{a["id"]:a for a in accounts}}
            base="/api/v1/ingestion-requests/"+item["request_id"]
            with st.form("balance_draft_"+item["request_id"]):
                rows=[]
                for row in item["draft"]["rows"]:
                    st.write(row.get("label") or row["row_id"])
                    selected=st.checkbox("保留此余额",row["selected"],key=item["request_id"]+row["row_id"]+"selected")
                    account_id=st.selectbox("账户",list(mapping),index=list(mapping).index(row["account_id"]) if row["account_id"] in mapping else 0,
                        format_func=lambda k:mapping[k]["name"],key=item["request_id"]+row["row_id"]+"account")
                    value=st.text_input("带符号余额（欠款为负）",row["balance"] or "",key=item["request_id"]+row["row_id"]+"value")
                    timestamp=st.text_input("观察时间",row["as_of"] or "",key=item["request_id"]+row["row_id"]+"time")
                    account=mapping[account_id]
                    rows.append({"row_id":row["row_id"],"selected":selected,"account_id":account_id,"balance":value or None,
                        "currency":account.get("currency"),"as_of":timestamp or None,"time_basis":row["time_basis"],
                        "expected_account_version":account.get("row_version"),"expected_latest_snapshot_id":(account.get("latest_snapshot") or {}).get("id"),
                        "exclusion_reason":None if selected else "User excluded this row"})
                acknowledge=st.checkbox("已核对范围和总额，确认以上选择")
                if st.form_submit_button("保存余额草稿修正"):
                    try:
                        actions.execute("balance_edit_"+item["request_id"],"PATCH",base+"/draft",{"expected_version":item["row_version"],"rows":rows,"acknowledge_evidence":acknowledge})
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            for action,label in (("confirm","确认全部所选余额"),("reject","放弃余额截图")):
                if st.button(label,key=action+item["request_id"]):
                    try:
                        actions.execute(action+item["request_id"],"POST",base+"/"+action,{"expected_version":item["row_version"]})
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
    if page["next_cursor"] and st.button("下一页余额草稿"):
        st.session_state["balance_draft_cursor"]=page["next_cursor"]
        st.rerun()
    if st.session_state.get("balance_draft_cursor") and st.button("余额草稿第一页"):
        st.session_state["balance_draft_cursor"]=None
        st.rerun()
