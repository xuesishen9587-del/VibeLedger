import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import urllib.request
import json
from database import fetch_all_accounts, fetch_all_records, apply_adjustment

# --- 页面全局设置 ---
st.set_page_config(page_title="Vibe Finance Center 2.0", page_icon="🏦", layout="wide")

# --- 汇率 API (缓存 1 小时) ---
@st.cache_data(ttl=3600)
def get_usd_cny_rate():
    try:
        url = "https://api.frankfurter.app/latest?from=USD&to=CNY"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            return float(data['rates']['CNY'])
    except Exception as e:
        st.warning(f"⚠️ 无法获取实时汇率，将采用默认固定汇率 1 USD = 7.20 CNY (错误: {e})")
        return 7.20

USD_CNY_RATE = get_usd_cny_rate()

# --- 侧边栏导航与手动平账 ---
st.sidebar.title("🏦 Vibe Finance 2.0")
st.sidebar.markdown("智能多模态家庭资产负债中心")

# 多页面路由
menu = st.sidebar.radio("功能中心", ["💰 资产负债中心", "📊 收支统计中心"])

# 展示侧边栏平账表单
st.sidebar.divider()
st.sidebar.subheader("上帝调平 (手动平账)")
st.sidebar.caption("💡 提示：您可以使用本表单手动调平账户，也可以继续使用 iPhone 快捷指令轻敲背板上传账户余额截图进行自动识别调平，两者完全兼容！")

accounts_list = []
try:
    accounts_data = fetch_all_accounts()
    accounts_list = [acc['account_name'] for acc in accounts_data]
except Exception:
    pass

if accounts_list:
    with st.sidebar.form("adjustment_form", clear_on_submit=True):
        adj_account = st.selectbox("选择要调平的账户", options=accounts_list)
        adj_balance = st.number_input("该账户当下真实余额", min_value=-999999.0, max_value=999999.0, step=100.0)
        adj_remarks = st.text_input("备注", value="手动上帝调平")
        submit_btn = st.form_submit_button("一键平账")
        
        if submit_btn:
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                apply_adjustment(adj_account, adj_balance, today_str, adj_remarks)
                st.sidebar.success(f"🎉 调平成功！{adj_account} 余额已更新为 ￥{adj_balance:,.2f}")
                st.cache_data.clear()
                st.rerun()
            except Exception as ex:
                st.sidebar.error(f"❌ 调平失败: {ex}")
else:
    st.sidebar.info("💡 等待数据库连接配置完成后，将在此显示可用账户列表。")


# --- 数据载入与格式清洗 ---
def load_data():
    try:
        raw_accounts = fetch_all_accounts()
        raw_transactions = fetch_all_records()
        df_acc = pd.DataFrame(raw_accounts) if raw_accounts else pd.DataFrame()
        df_tx = pd.DataFrame(raw_transactions) if raw_transactions else pd.DataFrame()
        return df_acc, df_tx
    except Exception as e:
        st.error(f"🔌 数据库连接异常: {e}")
        return pd.DataFrame(), pd.DataFrame()

df_acc, df_tx = load_data()


# ==============================================================================
# 页面 1: 💰 资产负债中心
# ==============================================================================
if menu == "💰 资产负债中心":
    # 优化点 2：将标题中的“负债”二字去掉
    st.title("💰 家庭财富与资产控制中心")
    st.markdown("监控家庭各大类别账户净资产及信用卡分期应还情况")
    
    if df_acc.empty:
        st.info("💡 数据库连接中，请在本地 `.env` 配置正确的 `DATABASE_URL` 以渲染真实数据。")
    else:
        # 数据转换 (折算外币)
        df_acc['balance_cny'] = df_acc.apply(
            lambda row: float(row['current_balance']) * USD_CNY_RATE if row['currency'] == 'USD' else float(row['current_balance']),
            axis=1
        )
        
        # 计算三大指标
        assets_df = df_acc[df_acc['account_type'].isin(['cash', 'savings', 'investment'])]
        total_assets = assets_df['balance_cny'].sum()
        
        credit_df = df_acc[df_acc['account_type'] == 'credit']
        # 负债总额为所有负数余额的绝对值之和
        total_liabilities = abs(credit_df[credit_df['balance_cny'] < 0]['balance_cny'].sum())
        # 信用卡多还产生的溢缴款（正数）作为资产累加
        credit_overpayment = credit_df[credit_df['balance_cny'] > 0]['balance_cny'].sum()
        
        net_assets = total_assets + credit_overpayment - total_liabilities
        
        # 1. 顶层 KPI
        col_kpi1, col_kpi2, col_kpi3 = st.columns(3)
        with col_kpi1:
            st.metric(label="总资产 (CNY折算)", value=f"￥{total_assets:,.2f}")
        with col_kpi2:
            st.metric(label="总负债 (已用额度绝对值)", value=f"￥{total_liabilities:,.2f}", delta_color="inverse")
        with col_kpi3:
            st.metric(label="净资产 (家庭财富水位)", value=f"￥{net_assets:,.2f}")
            
        st.divider()
        
        # 2. 中层四宫格矩阵
        st.subheader("🏦 账户资产负债分布明细")
        
        col_a, col_b = st.columns(2)
        with col_a:
            with st.expander("👛 现金钱包 (微信/支付宝/借记卡等活期)", expanded=True):
                cash_sub = df_acc[df_acc['account_type'] == 'cash']
                st.markdown(f"**分类汇总: ￥{cash_sub['balance_cny'].sum():,.2f}**")
                for _, r in cash_sub.iterrows():
                    curr_sym = "$" if r['currency'] == 'USD' else "￥"
                    st.write(f"- {r['account_name']}: `{curr_sym}{float(r['current_balance']):,.2f}`")
            
            with st.expander("🛡️ 储蓄存款 (银行定期/大额存单)", expanded=True):
                save_sub = df_acc[df_acc['account_type'] == 'savings']
                st.markdown(f"**分类汇总: ￥{save_sub['balance_cny'].sum():,.2f}**")
                for _, r in save_sub.iterrows():
                    st.write(f"- {r['account_name']}: `￥{float(r['current_balance']):,.2f}`")
                    
        with col_b:
            with st.expander("📈 投资资产 (股票/基金/定期理财)", expanded=True):
                inv_sub = df_acc[df_acc['account_type'] == 'investment']
                st.markdown(f"**分类汇总: ￥{inv_sub['balance_cny'].sum():,.2f}**")
                for _, r in inv_sub.iterrows():
                    st.write(f"- {r['account_name']}: `￥{float(r['current_balance']):,.2f}`")
            
            with st.expander("💳 信用负债 (信用卡/花呗额度占用)", expanded=True):
                credit_sub = df_acc[df_acc['account_type'] == 'credit']
                st.markdown(f"**分类汇总: ￥{credit_sub['balance_cny'].sum():,.2f}**")
                for _, r in credit_sub.iterrows():
                    curr_sym = "$" if r['currency'] == 'USD' else "￥"
                    st.write(f"- {r['account_name']}: `{curr_sym}{float(r['current_balance']):,.2f}`")

        st.divider()
        
        # 3. 信用卡分期与应还分析
        st.subheader("💳 信用卡与分期透视")
        if df_tx.empty:
            st.caption("暂无交易流水分析分期账单")
        else:
            df_tx_clean = df_tx.copy()
            df_tx_clean['date'] = pd.to_datetime(df_tx_clean['date'])
            df_tx_clean['amount'] = df_tx_clean['amount'].astype(float)
            
            credit_card_names = credit_df['account_name'].tolist()
            cc_txs = df_tx_clean[df_tx_clean['from_account'].isin(credit_card_names)]
            
            if cc_txs.empty:
                st.info("当前无信用卡消费流水记录。")
            else:
                today = pd.Timestamp(datetime.now().date())
                payable_txs = cc_txs[cc_txs['date'] <= today]
                total_payable = payable_txs['amount'].sum()
                
                future_txs = cc_txs[cc_txs['date'] > today]
                total_future = future_txs['amount'].sum()
                
                col_c1, col_c2 = st.columns(2)
                with col_c1:
                    st.info(f"**本期应还款总额 (已入账): ￥{total_payable:,.2f}**")
                    st.dataframe(
                        payable_txs[['date', 'from_account', 'amount', 'remarks']].style.format({'amount': '￥{:.2f}'}),
                        use_container_width=True, hide_index=True
                    )
                with col_c2:
                    st.warning(f"**未来待出账单 (未来分期未入账): ￥{total_future:,.2f}**")
                    st.dataframe(
                        future_txs[['date', 'from_account', 'amount', 'remarks']].style.format({'amount': '￥{:.2f}'}),
                        use_container_width=True, hide_index=True
                    )

        st.divider()
        
        # 4. 资产占比饼图
        st.subheader("📊 家庭资产配置透视")
        
        # 重新定义资产风险四级梯度分类
        def categorize_asset_risk(row):
            acc_name = row['account_name']
            acc_type = row['account_type']
            if acc_type == 'cash':
                return '👛 活期资产 (活期钱包/借记卡)'
            elif acc_type == 'savings':
                return '🛡️ 低风险资产 (定期存款/国债/柜台债)'
            elif acc_type == 'investment':
                # 中高风险/权益类投资资产
                if acc_name in ['Broker_Stocks', 'Alipay_Advanced_Investment']:
                    return '🚀 中高风险资产 (股票/进阶投资)'
                else:
                    # 中低风险资产（银行理财、支付宝稳健理财等）
                    return '⚖️ 中低风险资产 (银行理财/稳健理财)'
            return '其他'
        
        pie_df = assets_df.copy()
        pie_df['label'] = pie_df.apply(categorize_asset_risk, axis=1)
        pie_data = pie_df.groupby('label')['balance_cny'].sum().reset_index()
        
        if pie_data['balance_cny'].sum() == 0:
            st.info("资产余额全为 0，暂不显示配置饼图。")
        else:
            fig_pie = px.pie(
                pie_data, values='balance_cny', names='label',
                hole=0.4, color_discrete_sequence=px.colors.qualitative.Set2
            )
            fig_pie.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig_pie, use_container_width=True)


# ==============================================================================
# 页面 2: 📊 收支统计中心
# ==============================================================================
elif menu == "📊 收支统计中心":
    st.title("📊 家庭收支统计与看板")
    
    if df_tx.empty:
        st.info("💡 目前数据库中还没有账单记录，赶快用手机双击背板记一笔吧！")
    else:
        df = df_tx.copy()
        df['amount'] = df['amount'].astype(float)
        df['date'] = pd.to_datetime(df['date'])
        df['year'] = df['date'].dt.year
        df['month'] = df['date'].dt.month
        df['year_month'] = df['date'].dt.strftime("%Y-%m")
        
        # 优化点 3：将单列表下拉框月份筛选，改造为“年份 + 月份”双级联动级联选择器
        st.subheader("🔍 月度数据检索")
        available_years = sorted(df['year'].unique(), reverse=True)
        
        col_sel1, col_sel2 = st.columns(2)
        with col_sel1:
            selected_year = st.selectbox("选择年份", options=available_years)
            
        # 根据所选年份筛选出该年内有记录的月份
        months_in_year = sorted(df[df['year'] == selected_year]['month'].unique(), reverse=True)
        # 格式化成带中文的“X月”增强易读性
        month_options = {m: f"{m}月" for m in months_in_year}
        
        with col_sel2:
            selected_month_num = st.selectbox(
                "选择月份", 
                options=list(month_options.keys()), 
                format_func=lambda x: month_options[x]
            )
            
        # 组装出年月字符串（格式：YYYY-MM）
        selected_month = f"{selected_year}-{selected_month_num:02d}"
        
        # 过滤所选月份的数据
        month_df = df[df['year_month'] == selected_month]
        
        # 2. 月度收支大盘 KPI
        month_income = month_df[month_df['transaction_type'] == 'income']['amount'].sum()
        month_expense = month_df[month_df['transaction_type'] == 'expense']['amount'].sum()
        month_net = month_income - month_expense
        
        st.markdown(f"### 📅 {selected_year}年{selected_month_num}月 财务汇总")
        col_m1, col_m2, col_m3 = st.columns(3)
        with col_m1:
            st.metric(label="本月总收入", value=f"￥{month_income:,.2f}")
        with col_m2:
            st.metric(label="本月总支出", value=f"￥{month_expense:,.2f}", delta_color="inverse")
        with col_m3:
            st.metric(label="本月收支净结余", value=f"￥{month_net:,.2f}", delta=f"{'盈余' if month_net>=0 else '赤字'}")
            
        st.divider()
        
        # 3. 收支对比趋势与分类饼图
        col_chart1, col_chart2 = st.columns(2)
        
        with col_chart1:
            st.write("🍕 本月支出构成 (分类)")
            month_exp_group = month_df[month_df['transaction_type'] == 'expense'].groupby('category')['amount'].sum().reset_index()
            if month_exp_group.empty:
                st.caption("该月无支出数据")
            else:
                fig_exp_pie = px.pie(
                    month_exp_group, values='amount', names='category',
                    hole=0.3, color_discrete_sequence=px.colors.qualitative.Pastel
                )
                st.plotly_chart(fig_exp_pie, use_container_width=True)
                
        with col_chart2:
            st.write("🍕 本月收入构成 (分类)")
            month_inc_group = month_df[month_df['transaction_type'] == 'income'].groupby('category')['amount'].sum().reset_index()
            if month_inc_group.empty:
                st.caption("该月无收入数据")
            else:
                fig_inc_pie = px.pie(
                    month_inc_group, values='amount', names='category',
                    hole=0.3, color_discrete_sequence=px.colors.qualitative.Safe
                )
                st.plotly_chart(fig_inc_pie, use_container_width=True)
                
        st.divider()
        
        # 优化点 1：历史趋势变动图支持分类筛选
        st.subheader("📈 历史收支与消费分类趋势变动图")
        
        # 趋势展示维度选择
        trend_dimension = st.selectbox(
            "趋势展示维度", 
            options=["📊 总收入 vs 总支出趋势", "🎨 指定消费分类趋势"]
        )
        
        if trend_dimension == "📊 总收入 vs 总支出趋势":
            # 计算总收支趋势
            trend_df = df.groupby(['year_month', 'transaction_type'])['amount'].sum().unstack(fill_value=0.0).reset_index()
            if 'income' not in trend_df.columns:
                trend_df['income'] = 0.0
            if 'expense' not in trend_df.columns:
                trend_df['expense'] = 0.0
                
            fig_trend = go.Figure()
            fig_trend.add_trace(go.Bar(
                x=trend_df['year_month'], y=trend_df['income'],
                name='月度收入', marker_color='#2ecc71'
            ))
            fig_trend.add_trace(go.Bar(
                x=trend_df['year_month'], y=trend_df['expense'],
                name='月度支出', marker_color='#e74c3c'
            ))
            fig_trend.update_layout(
                barmode='group',
                xaxis_title="月份",
                yaxis_title="金额 (元)",
                legend_title="收支类型",
                hovermode="x unified"
            )
            st.plotly_chart(fig_trend, use_container_width=True)
            
        else:
            # 筛选出属于支出的分类
            all_expense_records = df[df['transaction_type'] == 'expense']
            available_categories = sorted(all_expense_records['category'].unique())
            
            if not available_categories:
                st.info("暂无历史消费支出数据，无法生成分类趋势图。")
            else:
                # 允许多选或者单选需要对比的支出类型
                selected_trend_cats = st.multiselect(
                    "选择需要展示趋势的消费分类", 
                    options=available_categories, 
                    default=available_categories[:3] if len(available_categories) >= 3 else available_categories
                )
                
                if not selected_trend_cats:
                    st.warning("请选择至少一个分类以渲染趋势图。")
                else:
                    # 按月份和选定分类进行聚合
                    cat_trend_df = all_expense_records[all_expense_records['category'].isin(selected_trend_cats)]
                    cat_trend_grouped = cat_trend_df.groupby(['year_month', 'category'])['amount'].sum().reset_index()
                    # 排序确保折线正常
                    cat_trend_grouped = cat_trend_grouped.sort_values('year_month')
                    
                    fig_cat_trend = px.line(
                        cat_trend_grouped, 
                        x='year_month', 
                        y='amount', 
                        color='category',
                        markers=True,
                        labels={'year_month': '月份', 'amount': '月度消费额 (元)', 'category': '消费分类'}
                    )
                    fig_cat_trend.update_layout(hovermode="x unified")
                    st.plotly_chart(fig_cat_trend, use_container_width=True)
        
        st.divider()
        
        # 5. 年度总收支账单
        current_year = datetime.now().year
        st.subheader(f"📅 {current_year} 年度收支总览 (大账本)")
        
        year_df = df[df['year'] == current_year]
        year_income = year_df[year_df['transaction_type'] == 'income']['amount'].sum()
        year_expense = year_df[year_df['transaction_type'] == 'expense']['amount'].sum()
        
        col_y1, col_y2 = st.columns(2)
        with col_y1:
            st.success(f"**年度累计总收入: ￥{year_income:,.2f}**")
        with col_y2:
            st.error(f"**年度累计总支出: ￥{year_expense:,.2f}**")
            
        st.write("📋 月度流水明细清单:")
        display_month_df = month_df[['date', 'amount', 'transaction_type', 'category', 'from_account', 'to_account', 'remarks']].copy()
        display_month_df['date'] = display_month_df['date'].dt.strftime('%Y-%m-%d')
        st.dataframe(
            display_month_df.style.format({'amount': '￥{:.2f}'}),
            use_container_width=True, hide_index=True
        )