import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from dateutil.relativedelta import relativedelta
import re
from dotenv import load_dotenv

# 加载 .env 配置文件
load_dotenv()

# 支持配置开发环境与生产环境的表后缀
TABLE_SUFFIX = os.environ.get("TABLE_SUFFIX", "_dev")
ACCOUNTS_TABLE = f"accounts{TABLE_SUFFIX}"
TRANSACTIONS_TABLE = f"transactions{TABLE_SUFFIX}"

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    """建立并返回 PostgreSQL 数据库连接"""
    if not DATABASE_URL:
        raise ValueError("错误：未找到 DATABASE_URL 环境变量！请检查 .env 配置。")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_account_balance_and_currency(cur, account_name):
    """辅助函数：获取指定账户的当前余额和币种"""
    cur.execute(
        f"SELECT current_balance, currency FROM {ACCOUNTS_TABLE} WHERE account_name = %s;",
        (account_name,)
    )
    res = cur.fetchone()
    if not res:
        raise ValueError(f"账户 '{account_name}' 不存在，请先在数据库中录入！")
    return float(res['current_balance']), res['currency']

def update_account_balance(cur, account_name, new_balance):
    """辅助函数：更新指定账户的当前余额"""
    cur.execute(
        f"UPDATE {ACCOUNTS_TABLE} SET current_balance = %s, updated_at = CURRENT_TIMESTAMP WHERE account_name = %s;",
        (new_balance, account_name)
    )

def safe_replace_day(date_obj, target_day: int):
    """安全地替换日期对象的 Day，如果目标天数超出了该月最大天数则自动限制为该月最后一天"""
    import calendar
    _, last_day = calendar.monthrange(date_obj.year, date_obj.month)
    return date_obj.replace(day=min(target_day, last_day))

def get_credit_card_statement_info(cur, account_name, today_date_str_or_obj):
    """
    计算指定信用卡的账单数据：
    - statement_balance: 已出账单金额
    - repaid_amount: 本期已还金额
    - remaining_due: 本期剩余应还
    - unbilled_balance: 未出账单消费
    """
    cur.execute(
        f"SELECT billing_day, due_day FROM {ACCOUNTS_TABLE} WHERE account_name = %s AND account_type = 'credit';",
        (account_name,)
    )
    acc = cur.fetchone()
    if not acc or acc['billing_day'] is None or acc['due_day'] is None:
        return {
            "statement_balance": 0.0,
            "repaid_amount": 0.0,
            "remaining_due": 0.0,
            "unbilled_balance": 0.0
        }
        
    b_day = int(acc['billing_day'])
    d_day = int(acc['due_day'])
    
    if isinstance(today_date_str_or_obj, str):
        today = datetime.strptime(today_date_str_or_obj, "%Y-%m-%d").date()
    else:
        today = today_date_str_or_obj
        
    # 寻找最近 of 已出账单日 S
    if today.day >= b_day:
        statement_date = safe_replace_day(today, b_day)
    else:
        statement_date = today - relativedelta(months=1)
        statement_date = safe_replace_day(statement_date, b_day)
        
    # 账单区间：[S - 1 month + 1 day, S]
    cycle_end = statement_date
    cycle_start = (cycle_end - relativedelta(months=1)) + relativedelta(days=1)
    
    # 还款日：根据 d_day 是否大于 b_day 确定是同月还是下月
    if d_day > b_day:
        due_date = safe_replace_day(statement_date, d_day)
    else:
        due_date = statement_date + relativedelta(months=1)
        due_date = safe_replace_day(due_date, d_day)
        
    repay_start = cycle_end + relativedelta(days=1)
    repay_end = due_date
    
    # 查询账单区间内该卡的消费总额 (expense)
    cur.execute(f"""
        SELECT SUM(amount) as total FROM {TRANSACTIONS_TABLE}
        WHERE from_account = %s AND transaction_type = 'expense'
          AND date >= %s AND date <= %s;
    """, (account_name, cycle_start, cycle_end))
    res_exp = cur.fetchone()
    statement_balance = float(res_exp['total']) if res_exp and res_exp['total'] is not None else 0.0
    
    # 查询还款窗口内已还金额 (transfer to card)
    cur.execute(f"""
        SELECT SUM(amount) as total FROM {TRANSACTIONS_TABLE}
        WHERE to_account = %s AND transaction_type = 'transfer'
          AND date >= %s AND date <= %s;
    """, (account_name, repay_start, repay_end))
    res_repay = cur.fetchone()
    repaid_amount = float(res_repay['total']) if res_repay and res_repay['total'] is not None else 0.0
    
    remaining_due = max(0.0, statement_balance - repaid_amount)
    
    # 查询未出账单消费
    unbilled_start = cycle_end + relativedelta(days=1)
    cur.execute(f"""
        SELECT SUM(amount) as total FROM {TRANSACTIONS_TABLE}
        WHERE from_account = %s AND transaction_type = 'expense'
          AND date >= %s AND date <= %s;
    """, (account_name, unbilled_start, today))
    res_unbilled = cur.fetchone()
    unbilled_balance = float(res_unbilled['total']) if res_unbilled and res_unbilled['total'] is not None else 0.0
    
    return {
        "statement_balance": statement_balance,
        "repaid_amount": repaid_amount,
        "remaining_due": remaining_due,
        "unbilled_balance": unbilled_balance
    }

def apply_adjustment(account_name, target_balance, date, remarks, idempotency_key=None):
    """
    对账校准逻辑 (adjustment)
    对于 investment 类型的账户且非初始设定(当前余额不为 0)，差值直接记为对应投资收益类型的 adjustment。
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # 获取账户当前类型
        cur.execute(f"SELECT account_type, current_balance, currency FROM {ACCOUNTS_TABLE} WHERE account_name = %s FOR UPDATE;", (account_name,))
        res = cur.fetchone()
        if not res:
            raise ValueError(f"账户 '{account_name}' 不存在")
        
        acc_type = res['account_type']
        bal = float(res['current_balance'])
        curr = res['currency']
        
        diff = float(target_balance) - bal
        
        if diff == 0:
            print(f"账户 {account_name} 余额一致，无需调平")
            return
            
        # 判定是否为投资收益账务自动转为收入
        if acc_type == 'investment' and bal != 0.00:
            # 区分稳健理财与进阶投资收益
            if account_name in ['Broker_Stocks', 'Alipay_Advanced_Investment']:
                category = 'Advanced_Investment_Income'
            else:
                category = 'Stable_Investment_Income'
            
            tx_type = 'adjustment'
            from_acc = None
            to_acc = account_name
            amount = diff  # 保留正负号
        else:
            # 初始化或普通现金、储蓄卡、信用卡平账
            from_acc = account_name if diff < 0 else None
            to_acc = account_name if diff > 0 else None
            tx_type = 'adjustment'
            category = 'Balance_Correction'
            amount = abs(diff)
            
        sub_key = f"{idempotency_key}_adj_{account_name}" if idempotency_key else None
        
        sql_insert = f"""
            INSERT INTO {TRANSACTIONS_TABLE} (
                date, amount, original_currency, from_account, to_account, 
                transaction_type, category, remarks, idempotency_key, parent_idempotency_key,
                from_amount, from_currency, to_amount, to_currency, fx_rate
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        cur.execute(
            sql_insert, 
            (
                date, amount, curr, from_acc, to_acc, tx_type, category, remarks, 
                sub_key, idempotency_key, amount, curr, amount, curr, 1.000000
            )
        )
        
        # 强制将账户余额设定为最新目标余额
        cur.execute(
            f"UPDATE {ACCOUNTS_TABLE} SET current_balance = %s, updated_at = CURRENT_TIMESTAMP WHERE account_name = %s;",
            (target_balance, account_name)
        )
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def fetch_all_records():
    """获取所有账单流水明细"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {TRANSACTIONS_TABLE} ORDER BY date DESC, created_at DESC;")
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records

def fetch_all_accounts():
    """获取所有账户信息余额"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {ACCOUNTS_TABLE} ORDER BY account_type ASC, account_name ASC;")
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records