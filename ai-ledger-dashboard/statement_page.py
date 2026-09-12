"""Selected-account statement import and one editable preview."""
import hashlib
from datetime import date
from uuid import uuid4
import streamlit as st
from api_client import ApiError
from spending_controller import SpendingActions
from statement_targets import TargetBrowser


def target_choices(client, identity, visible):
    state=st.session_state.setdefault("statement_targets_"+identity,{})
    transactions=TargetBrowser(state.setdefault("transactions",{}),client,"/api/v1/transactions")
    plans=TargetBrowser(state.setdefault("plans",{}),client,"/api/v1/spending-schedules")
    with st.expander("查找要关联的支出和月度计划"):
        st.caption("按实际交易日期和完整商户名称筛选；加载更多可查看后续记录。翻页或搜索前请先保存下方预览修正。")
        with st.form("stmt_target_search_"+identity):
            start=st.date_input("查找起始日期",value=None,key="stmt_find_start_"+identity)
            end=st.date_input("查找截止日期",value=None,key="stmt_find_end_"+identity)
            merchant=st.text_input("完整商户名称（可留空）",key="stmt_find_merchant_"+identity)
            if st.form_submit_button("查找支出或退款"):
                if start and end and start>end:
                    st.error("起始日期不能晚于截止日期。")
                else:
                    transactions.load({"from":str(start) if start else None,"to":str(end) if end else None,"merchant":merchant})
        if "items" not in transactions.state:
            transactions.load()
        if "items" not in plans.state:
            plans.load()
        st.caption(f"已加载 {len(transactions.state['items'])} 条交易、{len(plans.state['items'])} 个月度计划。")
        if transactions.state.get("next_cursor") and st.button("加载更多交易",key="stmt_more_transactions_"+identity):
            transactions.load(transactions.state.get("filters"),more=True)
        if plans.state.get("next_cursor") and st.button("加载更多月度计划",key="stmt_more_plans_"+identity):
            plans.load(more=True)
        if st.button("刷新关联目标",key="stmt_refresh_targets_"+identity):
            transactions.load(transactions.state.get("filters"))
            plans.load()
    transaction_ids=[]
    plan_ids=[]
    for row in visible:
        prefix=identity+row["row_id"]
        transaction_ids.extend([row.get("transaction_id"),st.session_state.get(prefix+"target")])
        plan_ids.extend([row.get("schedule_id"),st.session_state.get(prefix+"schedule")])
    targets,missing_transactions=transactions.choices(transaction_ids)
    plan_map,missing_plans=plans.choices(plan_ids)
    if missing_transactions or missing_plans:
        st.warning("部分原关联记录已不可用，请重新选择关联目标或跳过该行。")
    # Keep a visible tombstone for a saved selection; never silently clear its ID.
    for key in missing_transactions:
        targets[key]={"id":key,"occurred_on":"记录不可用","merchant":key,"original_amount":"","original_currency":"","row_version":None}
    for key in missing_plans:
        plan_map[key]={"id":key,"name":"记录不可用 · "+key,"row_version":None}
    return targets,plan_map


def render(client,review_only=False):
    if not review_only:
        st.title("账单导入")
        st.caption("批量记录支出，并可保存账单日期的余额。转账和还款不会记为支出。")
    actions=SpendingActions(st.session_state.setdefault("_statement_actions",{}),client)
    for slot in list(actions.state):
        st.warning("上次保存结果尚未确定。")
        if st.button("重试账单保存",key="stmt_retry_"+slot):
            try:
                actions.retry(slot)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    accounts=client.request("GET","/api/v1/accounts",params={"status":"active"})["items"]
    account_map={a["id"]:a for a in accounts}
    enabled={a["id"]:a for a in accounts if a["statement_import_enabled"]}
    if not review_only:
        if not enabled:
            st.info("请先在账户设置中启用账单导入。")
        else:
            account_id=st.selectbox("账单所属账户",list(enabled),format_func=lambda k:enabled[k]["name"])
            file=st.file_uploader("上传 PDF（最多 20 MiB、50 页）",type=["pdf"])
            password=st.text_input("PDF 密码（如有）",type="password",key="statement_password")
            pending=st.session_state.get("statement_upload_pending")
            if pending:
                st.warning("有一次上传尚待确认结果，请保留原文件。")
                if st.button("查询上传结果"):
                    try:
                        result=client.request("GET","/api/v1/ingestion-requests/by-key/"+pending["key"])
                        st.info(result["display_summary"])
                        if result["status"]!="processing":
                            st.session_state.pop("statement_upload_pending",None)
                    except Exception as exc:
                        st.error(str(exc))
                if st.button("取消尚未确定的上传"):
                    try:
                        result=client.request("POST","/api/v1/ingestion-requests/by-key/"+pending["key"]+"/cancel")
                        st.info(result["display_summary"])
                        st.session_state.pop("statement_upload_pending",None)
                    except Exception as exc:
                        st.error(str(exc))
            if st.button("生成账单预览") and file:
                content=file.getvalue()
                digest=hashlib.sha256(content).hexdigest()
                if pending and (pending["digest"]!=digest or pending["account_id"]!=account_id):
                    st.error("先查询或取消上次上传，再上传另一份账单。")
                else:
                    command=pending or {"key":str(uuid4()),"digest":digest,"account_id":account_id}
                    st.session_state["statement_upload_pending"]=command
                    try:
                        result=client.request("POST",f"/api/v1/accounts/{account_id}/statement-imports",
                            files={"file":("statement.pdf",content,"application/pdf")},data={"password":password} if password else {},
                            headers={"Idempotency-Key":command["key"]},timeout=125)
                        if result["status"]!="processing":
                            st.session_state.pop("statement_upload_pending",None)
                        st.info(result["display_summary"])
                    except ApiError as exc:
                        if exc.code=="STATEMENT_ALREADY_UPLOADED":
                            st.info("此账单已有导入记录："+exc.details.get("request_id",""))
                            st.session_state.pop("statement_upload_pending",None)
                        elif 400<=exc.status_code<500:
                            st.session_state.pop("statement_upload_pending",None)
                        st.error(str(exc))
    page=client.request("GET","/api/v1/ingestion-requests",params={"limit":50,"request_kind":"statement","cursor":st.session_state.get("statement_cursor")})
    categories=client.request("GET","/api/v1/categories",params={"status":"active"})["items"]
    category_names={None:"请选择分类",**{c["id"]:c["name"] for c in categories if c["category_type"]=="expense"}}
    for item in page["items"]:
        draft=item["draft"]
        if "lines" not in draft:
            continue
        identity=item["request_id"]
        with st.expander("待保存账单 · "+identity[:8],expanded=True):
            for warning in item["warnings"]:
                st.warning(warning["message"])
            base="/api/v1/ingestion-requests/"+identity
            line_page=st.number_input("账单明细页",min_value=1,max_value=max(1,(len(draft["lines"])+24)//25),value=1,key="stmt_line_page_"+identity)
            visible=draft["lines"][(line_page-1)*25:line_page*25]
            shown={r["row_id"] for r in visible}
            targets,plan_map=target_choices(client,identity,visible)
            with st.form("statement_"+identity):
                start=st.date_input("账单起始日期",date.fromisoformat(draft["period_start"]) if draft["period_start"] else None,key="stmt_start_"+identity)
                end=st.date_input("账单截止日期",date.fromisoformat(draft["period_end"]) if draft["period_end"] else None,key="stmt_end_"+identity)
                partial=st.checkbox("明确接受本次仅覆盖部分账单",value=draft["acknowledge_partial"],key="stmt_partial_"+identity)
                owner=st.checkbox("已核实是所选账户的账单",value=draft["confirm_account_identity"],key="stmt_owner_"+identity)
                lines=[{"row_id":r["row_id"],"action":r["action"]} for r in draft["lines"] if r["row_id"] not in shown]
                for row in visible:
                    prefix=identity+row["row_id"]
                    st.write(f"第 {row['row_no']} 行 · {row.get('merchant') or ''}")
                    if row.get("duplicate_ids"):
                        st.caption(f"发现 {len(row['duplicate_ids'])} 条可能重复的记录，请核对后选择。")
                    action=st.selectbox("处理方式",["create","link_existing","skip","use_schedule_period"],
                        index=["create","link_existing","skip","use_schedule_period"].index(row["action"]),
                        format_func=lambda a:{"create":"创建独立支出／退款","link_existing":"关联已有记录","skip":"跳过","use_schedule_period":"关联分期期间"}[a],key=prefix+"action")
                    amount=st.text_input("原币金额",row.get("original_amount") or "",key=prefix+"amount")
                    currency=st.text_input("币种",row.get("original_currency") or "",key=prefix+"currency")
                    day=st.date_input("实际交易日期",date.fromisoformat(row["occurred_on"]) if row.get("occurred_on") else None,key=prefix+"day")
                    kind=st.selectbox("性质",[None,"expense","refund"],index=[None,"expense","refund"].index(row.get("transaction_type")),key=prefix+"kind")
                    category=st.selectbox("支出分类",list(category_names),index=list(category_names).index(row["category_id"]) if row.get("category_id") in category_names else 0,
                        format_func=category_names.get,key=prefix+"category")
                    note=st.text_input("备注／未关联退款说明",row.get("remarks") or "",key=prefix+"note")
                    reason=st.text_input("跳过原因",row.get("reason") or "",key=prefix+"reason")
                    target=st.selectbox("关联已记录支出或退款",[None,*targets],index=[None,*targets].index(row.get("transaction_id")) if row.get("transaction_id") in targets else 0,
                        format_func=lambda k:"不关联" if k is None else f"{targets[k]['occurred_on']} · {targets[k].get('merchant') or ''} · {targets[k]['original_amount']} {targets[k]['original_currency']}",key=prefix+"target")
                    version=targets[target]["row_version"] if target else None
                    schedule=st.selectbox("关联月度计划",[None,*plan_map],index=[None,*plan_map].index(row.get("schedule_id")) if row.get("schedule_id") in plan_map else 0,
                        format_func=lambda k:"不关联" if k is None else plan_map[k]["name"],key=prefix+"schedule")
                    period=st.number_input("第几期",min_value=1,value=row.get("period_no") or 1,key=prefix+"period")
                    schedule_version=plan_map[schedule]["row_version"] if schedule else None
                    edited={"row_id":row["row_id"],"action":action,"confirm_facts":True,"original_amount":amount or None,"original_currency":currency or None,
                        "occurred_on":str(day) if day else None,"transaction_type":kind,"remarks":note or None,"reason":reason or None,
                        "transaction_id":target or None,"expected_transaction_version":version if target else None,
                        "schedule_id":schedule or None,"period_no":period if schedule else None,"expected_schedule_version":schedule_version if schedule else None}
                    # Unchanged AI fallback does not silently become a user-confirmed category.
                    confirmed_category=st.checkbox("确认此分类",key=prefix+"confirm_category")
                    if category!=row.get("category_id") or confirmed_category:
                        edited["category_id"]=category
                    lines.append(edited)
                balance=draft.get("balance")
                selected_balance=None
                if balance:
                    st.write("账单期末余额（独立于支出合计）")
                    selected=st.checkbox("同时保存此余额",balance["selected"],key="stmt_balance_selected_"+identity)
                    value=st.text_input("期末带符号余额（欠款为负）",balance.get("balance") or "",key="stmt_balance_value_"+identity)
                    stamp=st.text_input("余额实际观察时间",balance.get("as_of") or "",key="stmt_balance_time_"+identity)
                    observations=client.request("GET",f"/api/v1/accounts/{draft['account_id']}/snapshots",params={"limit":200})["items"]
                    choices={r["id"]:r for r in observations}
                    reuse=st.selectbox("复用已记录余额（须日期金额相同）",[None,*choices],
                        format_func=lambda k:"新增观察" if k is None else f"{choices[k]['as_of']} · {choices[k]['balance']} {choices[k]['currency']}",key="stmt_balance_reuse_"+identity)
                    account=account_map.get(draft["account_id"],{})
                    selected_balance={"row_id":balance["row_id"],"selected":selected,"account_id":draft["account_id"],
                        "balance":value or None,"currency":account.get("currency"),"as_of":stamp or None,"time_basis":balance["time_basis"],
                        "expected_account_version":account.get("row_version"),"expected_latest_snapshot_id":(account.get("latest_snapshot") or {}).get("id"),
                        "reuse_snapshot_id":reuse or None,"exclusion_reason":None if selected else "User excluded balance"}
                if st.form_submit_button("保存账单预览修正"):
                    try:
                        body={"expected_version":item["row_version"],"lines":lines,"balance":selected_balance,
                            "period_start":str(start) if start else None,"period_end":str(end) if end else None,
                            "acknowledge_partial":partial,"confirm_account_identity":owner}
                        actions.execute("stmt_edit_"+identity,"PATCH",base+"/draft",body)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            for action,label in (("confirm","保存选中支出与余额"),("reject","放弃此账单")):
                if st.button(label,key="stmt_"+action+identity):
                    try:
                        result=actions.execute("stmt_"+action+identity,"POST",base+"/"+action,{"expected_version":item["row_version"]})
                        if result["status"]=="committed":
                            st.session_state["statement_last_result"]=result
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
    if st.session_state.get("statement_last_result"):
        result=st.session_state["statement_last_result"]
        st.success(f"上次保存：新建 {result['counts']['create']}、关联 {result['counts']['link']}、跳过 {result['counts']['skip']}；余额 {len(result['snapshots'])} 条。")
    if page["next_cursor"] and st.button("下一页账单"):
        st.session_state["statement_cursor"]=page["next_cursor"]
        st.rerun()
    if st.session_state.get("statement_cursor") and st.button("账单第一页"):
        st.session_state["statement_cursor"]=None
        st.rerun()
