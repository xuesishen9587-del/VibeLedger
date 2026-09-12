"""Whole investment intervals, explicit complete flows and recoverable edits."""
from decimal import Decimal
import streamlit as st
from spending_controller import SpendingActions


def render_interval(client,actions,item):
    identity=item["id"]
    with st.expander(item["account_name"]+" · "+item["period_start"]+" 至 "+item["period_end"]):
        estimated=item["gain_status"]=="estimated"
        st.write(("估算收益：" if estimated else "已确认收益：")+item["gain"]+" "+item["currency"])
        st.caption("期初 "+item["opening_value"]+" → 期末 "+item["closing_value"]+"；资金区间不含期初时刻、包含期末时刻。")
        precision={"explicit":"明确时刻","capture":"截图时间","date_only":"仅日期，按当天结束时刻记录"}
        st.caption("观察时间精度："+precision[item["opening_time_basis"]]+" → "+precision[item["closing_time_basis"]])
        if estimated:
            st.info("当前假设没有投入或取出资金；这不是已确认的资金流水。")
        if item["needs_review"]:
            st.warning("余额变动达到复核阈值，请确认期间是否有资金进出。保留估算不会阻止余额保存。")
        if item.get("reason")=="PAIR_CHANGED":
            st.warning("余额区间已改变，原资金确认未沿用，请按新起止时间核对。")
        # Version in widget keys prevents displaying stale fields after a confirmed edit.
        form_key="investment_"+identity+"_"+str(item["input_version"])
        with st.form(form_key):
            additions=st.text_input("期间投入总额",item["effective_contributions"],key=form_key+"add")
            withdrawals=st.text_input("期间取出总额（含转到账外的分红）",item["effective_withdrawals"],key=form_key+"take")
            notes=st.text_input("备注",item.get("notes") or "",key=form_key+"notes")
            complete=st.checkbox("确认以上金额完整覆盖所示区间；均为零表示确认无资金进出",key=form_key+"complete")
            if st.form_submit_button("保存完整资金确认"):
                if not complete:
                    st.error("请先确认投入和取出总额覆盖整个区间。")
                else:
                    try:
                        actions.execute(identity,"PUT","/api/v1/investment-period-inputs",{
                            "opening_snapshot_id":item["opening_snapshot_id"],"closing_snapshot_id":item["closing_snapshot_id"],
                            "contributions_amount":additions,"withdrawals_amount":withdrawals,
                            "expected_version":item["input_version"],"notes":notes or None})
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        if item.get("input_id"):
            if st.button("查看资金确认修改记录",key="investment_history_"+identity):
                st.write(client.request("GET","/api/v1/history",params={"entity_type":"investment_period_input","entity_id":item["input_id"]})["items"])
            if item["input_status"]=="active":
                with st.form("investment_void_"+identity):
                    reason=st.text_input("撤回确认原因")
                    if st.form_submit_button("撤回资金确认，恢复估算"):
                        try:
                            actions.execute(identity,"POST",f"/api/v1/investment-period-inputs/{item['input_id']}/void",
                                {"expected_version":item["input_version"],"reason":reason})
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))


def render(client,review_only=False):
    st.subheader("已保存余额的投资变动复核" if review_only else "投资区间收益")
    st.caption("收益 = 期末余额 − 期初余额 − 期间投入 + 期间取出。仅合计完整观察区间，不按月份摊分，也不计入日常收入。")
    actions=SpendingActions(st.session_state.setdefault("_investment_actions",{}),client)
    for slot in list(actions.state):
        st.warning("上次资金确认结果尚未确定，请先重试原请求。")
        if st.button("重试资金确认",key="investment_retry_"+slot):
            try:
                actions.retry(slot)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    if review_only:
        report=client.request("GET","/api/v1/review",params={"section":"investment","limit":50,"cursor":st.session_state.get("investment_cursor")})
        st.caption(f"待复核区间：{report['counts']['unusual_investment_estimates']}")
        if not report["items"]:
            st.info("本页没有待复核投资变动。")
    else:
        with st.form("investment_range"):
            start=st.date_input("投资区间起始日期",value=None)
            end=st.date_input("投资区间截止日期",value=None)
            if st.form_submit_button("查看投资区间"):
                if start and end and start>end:
                    st.error("起始日期不能晚于截止日期。")
                else:
                    st.session_state["_investment_range_query"]={k:str(v) for k,v in (("from",start),("to",end)) if v}
        report=client.request("GET","/api/v1/reports/investments",params=st.session_state.get("_investment_range_query",{}))
        if not report["native_currency_totals"]:
            st.info("请先在设置中添加投资账户，并在财富页记录余额。")
            return
        if not report["coverage"]["complete"]:
            st.warning("投资收益覆盖不完整；以下只合计可用的完整观察区间。")
        for total in report["native_currency_totals"]:
            status={"estimated":"含估算","user_confirmed":"已确认","unavailable":"不可用"}[total["gain_status"]]
            st.write(total["currency"]+" · "+status+"合计："+(total["combined_gain"] if total["combined_gain"] is not None else "暂无"))
            st.caption("已确认部分 "+total["confirmed_gain_subtotal"]+"；估算部分 "+total["estimated_gain_subtotal"])
        for row in report["unavailable"]:
            st.info(row["account_name"]+"：不足两次余额观察，收益暂不可用。")
        if report["first_observations"]:
            st.caption("各账户首次观察仅建立起点，不产生收益。")
        if report["coverage"]["gaps"] or report["excluded_boundary_intervals"]:
            with st.expander("未覆盖范围及跨边界区间"):
                labels={"MISSING_OBSERVATIONS":"尚无余额观察","BEFORE_FIRST_OBSERVATION":"首次观察之前","AFTER_LAST_OBSERVATION":"最近观察之后"}
                names={r["account_id"]:r["account_name"] for r in [*report["items"],*report["unavailable"],*report["excluded_boundary_intervals"],*report["first_observations"]]}
                st.dataframe([{"账户":names.get(r["account_id"],"投资账户"),"未覆盖原因":labels[r["reason"]],
                    "起始":r.get("from"),"截止":r.get("to")} for r in report["coverage"]["gaps"]])
                st.dataframe([{"账户":r["account_name"],"区间起始":r["period_start"],"区间截止":r["period_end"],
                    "整个区间收益（未计入本次合计）":r["gain"],"币种":r["currency"],
                    "收益状态":"含估算" if r["gain_status"]=="estimated" else "已确认"} for r in report["excluded_boundary_intervals"]])
        if report["historical_inputs"]:
            with st.expander("已失效区间的资金确认历史"):
                st.caption("以下确认不再对应当前相邻余额，未计入收益。")
                for row in report["historical_inputs"]:
                    st.write(row["opening_as_of"],"至",row["closing_as_of"],"投入",row["contributions_amount"],"取出",row["withdrawals_amount"])
                    if st.button("查看旧确认历史",key="old_investment_"+row["id"]):
                        st.write(client.request("GET","/api/v1/history",params={"entity_type":"investment_period_input","entity_id":row["id"]})["items"])
    for item in report["items"]:
        render_interval(client,actions,item)
    if review_only:
        if report["next_cursor"] and st.button("下一页投资复核"):
            st.session_state["investment_cursor"]=report["next_cursor"]
            st.rerun()
        if st.session_state.get("investment_cursor") and st.button("投资复核第一页"):
            st.session_state["investment_cursor"]=None
            st.rerun()


def render_settings(client):
    with st.expander("投资变动复核设置"):
        report=client.request("GET","/api/v1/review",params={"section":"investment","limit":1})
        actions=SpendingActions(st.session_state.setdefault("_investment_settings_actions",{}),client)
        for slot in list(actions.state):
            if st.button("重试复核阈值保存"):
                try:
                    actions.retry(slot)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        with st.form("investment_settings_"+str(report["settings_row_version"])):
            percent=st.text_input("估算余额变动达到多少百分比时提示复核",str(Decimal(report["investment_review_change_ratio"])*100))
            st.caption("例如 20 表示变动达到期初余额的 20%；这不是收益率。期初为零而期末非零时始终提示。")
            if st.form_submit_button("保存投资复核阈值"):
                try:
                    actions.execute("ratio","PATCH","/api/v1/household-settings",{"expected_version":report["settings_row_version"],
                        "investment_review_change_ratio":str(Decimal(percent)/100)})
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
