"""Show complete versus known spending without treating missing FX as zero."""
import streamlit as st
from time_utils import get_dashboard_today


def render(client, actions, categories=None):
    today = get_dashboard_today()
    with st.expander("支出合计", expanded=True):
        start = st.date_input("合计起始日期", today.replace(day=1))
        end = st.date_input("合计截止日期", today)
        if start > end:
            st.warning("起始日期不能晚于截止日期。")
            return
        params = {"from": str(start), "to": str(end)}
        try:
            report = client.request("GET", "/api/v1/reports/spending", params=params)
            if report["missing_conversion_count"]:
                scope = str(today)
                automatic = st.session_state.get("_spending_fx_attempt_day") != scope
                manual = st.button("补齐缺失汇率")
                if automatic or manual:
                    # One automatic attempt per session/day; explicit retry retains the command.
                    st.session_state["_spending_fx_attempt_day"] = scope
                    try:
                        if "refresh_fx" in actions.state:
                            actions.retry("refresh_fx")
                        else:
                            actions.execute("refresh_fx", "POST", "/api/v1/reports/refresh-fx", {})
                        report = client.request("GET", "/api/v1/reports/spending", params=params)
                    except Exception as exc:
                        st.warning("汇率补齐未完成：" + str(exc))
            currency = report["reporting_currency"]
            missing = report["missing_conversion_count"]
            if missing:
                st.warning(f"有 {missing} 笔记录缺少汇率。以下仅显示已折算部分，合计尚不完整。")
            prefix = "known_" if missing else ""
            for field, label in (("gross_expenses", "支出"), ("refunds", "退款"),
                                 ("net_spending", "净支出"), ("recorded_income", "已记录收入")):
                st.metric(("已折算" if missing else "") + label, f"{report[prefix + field]} {currency}")
            st.caption("月度计划已处理至 " + report["schedules_current_through"])
            def table(rows, names=None):
                return [{"分组": (names or {}).get(row["key"], row["key"] or "未填写"),
                         "支出": row["gross_expenses"], "退款": row["refunds"],
                         "净支出": row["net_spending"], "已记录收入": row["recorded_income"],
                         "已折算净支出": row["known_net_spending"],
                         "缺少汇率笔数": row["missing_conversion_count"]} for row in rows]
            st.write("原币金额")
            st.dataframe(table(report["native_currency_totals"]))
            for key, title in (("category", "按分类"), ("merchant", "按商户"), ("month", "按月份")):
                with st.expander(title):
                    st.dataframe(table(report[key], categories if key == "category" else None))
        except Exception as exc:
            st.error("支出合计暂时不可用：" + str(exc))
