"""Dated step history with explicit gaps and separate known partial amounts."""
from datetime import timedelta
import plotly.graph_objects as go
import streamlit as st
from time_utils import get_dashboard_today


def history_figure(points, currency):
    figure=go.Figure()
    labels=(("total_assets","known_assets","资产","#2563eb"),
            ("total_liabilities","known_liabilities","负债","#d97706"),
            ("net_worth","known_net_worth","净资产","#059669"))
    times=[p["as_of"] for p in points]
    for total,known,label,color in labels:
        for partial in (False,True):
            values=[]
            for point in points:
                usable=not point.get("setup_required",False)
                complete=point["coverage"]["complete"]
                value=point.get(known if partial else total)
                values.append(float(value) if usable and complete!=partial and value is not None else None)
            figure.add_trace(go.Scatter(x=times,y=values,name=("已知部分 · " if partial else "完整 · ")+label,
                mode="lines+markers",connectgaps=False,line={"shape":"hv","color":color,"dash":"dot" if partial else "solid"},
                marker={"size":4},hovertemplate="%{x}<br>%{y:,.2f} "+currency+"<extra>%{fullData.name}</extra>"))
    figure.update_layout(xaxis_title="观察时间",yaxis_title=currency,hovermode="x unified",
        legend={"orientation":"h"},margin={"l":16,"r":16,"t":16,"b":16})
    return figure


def history_rows(points):
    return [{"时间":p["as_of"],"覆盖情况":"无已配置账户" if p.get("setup_required") else "完整" if p["coverage"]["complete"] else "仅已知部分",
        "资产":p["total_assets"],"负债":p["total_liabilities"],"净资产":p["net_worth"],
        "已知资产":p["known_assets"],"已知负债":p["known_liabilities"],"已知净资产":p["known_net_worth"],
        "缺余额账户数":len(p["coverage"]["missing_account_ids"]),"缺汇率币种":"、".join(p["coverage"]["missing_fx_currencies"]),
        "旧余额账户数":len(p["coverage"]["stale_account_ids"]),
        "最早余额日期":p["coverage"]["oldest_observation_at"],"最新余额日期":p["coverage"]["newest_observation_at"]} for p in points]


def render(client):
    with st.expander("家庭财富历史"):
        st.caption("按最近一次余额观察阶梯展示，不插值估算每日余额。实线为完整金额，虚线仅为已知部分；空缺不代表零。历史不推算风险分布。")
        today=get_dashboard_today()
        with st.form("wealth_history_range"):
            start=st.date_input("财富历史起始日期",value=today-timedelta(days=365),max_value=today)
            end=st.date_input("财富历史截止日期",value=today,max_value=today)
            submitted=st.form_submit_button("查看／刷新财富历史")
        if submitted:
            if start>end or (end-start).days>3660:
                st.error("请选择先后有序、最长十年的日期范围。")
                return
            st.session_state["_wealth_history_query"]={"from":str(start),"to":str(end)}
        params=st.session_state.get("_wealth_history_query")
        if not params:
            return
        try:
            report=client.request("GET","/api/v1/reports/wealth-history",params=params)
        except Exception as exc:
            st.error(str(exc))
            return
        points=report["points"]
        st.caption(f"显示范围：{params['from']} 至 {params['to']}。历史外币按各时点可用的参考汇率折算。")
        if not points or all(p.get("setup_required",False) for p in points):
            st.info("此范围内没有已配置账户的财富记录。")
            return
        if any(not p["coverage"]["complete"] for p in points):
            st.warning("部分历史时点缺少余额或汇率，请结合已知部分和覆盖明细阅读。")
        st.plotly_chart(history_figure(points,points[0]["reporting_currency"]),use_container_width=True)
        st.dataframe(history_rows(points),use_container_width=True)
