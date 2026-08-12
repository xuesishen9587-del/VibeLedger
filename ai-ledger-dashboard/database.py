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

def apply_adjustment(account_name, target_balance, date, remarks):
    """
    上帝平账逻辑 (adjustment)
    计算真实绝对水位与数据库当前水位差值，差值自动作差补齐流水。
    对于 investment 类型的账户且非初始设定(当前余额不为 0)，差值直接记为对应投资收益类型的 income，金额可正可负。
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # 获取账户当前类型
        cur.execute(f"SELECT account_type, current_balance, currency FROM {ACCOUNTS_TABLE} WHERE account_name = %s;", (account_name,))
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
            
            tx_type = 'income'
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
        
        sql_insert = f"""
            INSERT INTO {TRANSACTIONS_TABLE} (date, amount, original_currency, from_account, to_account, transaction_type, category, remarks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        cur.execute(sql_insert, (date, amount, curr, from_acc, to_acc, tx_type, category, remarks))
        
        # 强制将账户余额设定为最新目标余额
        update_account_balance(cur, account_name, target_balance)
        
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