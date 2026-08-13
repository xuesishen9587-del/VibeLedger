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

def init_db():
    """在云端初始化创建资产负债中心 2.0 数据表"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. 创建 accounts 表
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {ACCOUNTS_TABLE} (
            id SERIAL PRIMARY KEY,
            account_name TEXT UNIQUE NOT NULL,
            account_type TEXT NOT NULL CHECK (account_type IN ('cash', 'credit', 'savings', 'investment')),
            current_balance NUMERIC(12, 2) DEFAULT 0.00,
            currency TEXT DEFAULT 'CNY',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # 2. 创建 transactions 表
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRANSACTIONS_TABLE} (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            amount NUMERIC(12, 2) NOT NULL,
            original_currency TEXT DEFAULT 'CNY',
            from_account TEXT REFERENCES {ACCOUNTS_TABLE}(account_name),
            to_account TEXT REFERENCES {ACCOUNTS_TABLE}(account_name),
            transaction_type TEXT NOT NULL CHECK (transaction_type IN ('expense', 'income', 'transfer', 'adjustment')),
            category TEXT NOT NULL,
            remarks TEXT,
            is_installment BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"🚀 {ACCOUNTS_TABLE} 和 {TRANSACTIONS_TABLE} 表初始化/核对成功！")

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

def insert_single_transaction_in_tx(cur, date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment=False):
    """
    在已有事务(cur)中执行单笔流水写入，并驱动账户余额水位更新
    """
    # 1. 写入交易流水表
    sql_insert = f"""
        INSERT INTO {TRANSACTIONS_TABLE} (date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    cur.execute(sql_insert, (date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment))
    
    # 2. 状态机：更新账户余额水位
    if transaction_type == 'expense':
        if not from_account:
            raise ValueError("支出类型交易(expense)必须指定 from_account")
        bal, curr = get_account_balance_and_currency(cur, from_account)
        # 注意：此处金额都是正数，支出需减去
        update_account_balance(cur, from_account, bal - float(amount))
        
    elif transaction_type == 'income':
        if not to_account:
            raise ValueError("收入类型交易(income)必须指定 to_account")
        bal, curr = get_account_balance_and_currency(cur, to_account)
        update_account_balance(cur, to_account, bal + float(amount))
        
    elif transaction_type == 'transfer':
        if not from_account or not to_account:
            raise ValueError("转账类型交易(transfer)必须同时指定 from_account 和 to_account")
            
        bal_from, curr_from = get_account_balance_and_currency(cur, from_account)
        bal_to, curr_to = get_account_balance_and_currency(cur, to_account)
        
        # 处理可能的多币种转账换汇（如 CNY 转账还款至 USD 信用卡）
        # 默认情况下，如果两个账户币种相同，直接 1:1 转账
        # 如果不同，且没有备注指定损益，按转入/转出卡原始值操作
        # 这里默认以 amount 代表 to_account (转入账户) 的增加金额
        # 转出账户金额需要进行币种扣减。如果有汇兑差额，可以从 remarks 匹配实际扣减的 CNY。
        cny_spent = None
        if curr_from == "CNY" and curr_to == "USD":
            # 尝试从 remarks 匹配类似于 "实际扣除 725元" 或 "扣除725.5CNY" 等字段
            match = re.search(r'(?:实际扣除|扣除|支付)\s*([\d\.]+)\s*(?:元|cny|rmb)', remarks, re.IGNORECASE) if remarks else None
            if match:
                cny_spent = float(match.group(1))
                
        # 执行余额变动
        if cny_spent is not None:
            # 扣减转出账户实际的人民币金额
            update_account_balance(cur, from_account, bal_from - cny_spent)
            # 增加转入账户的外币金额
            update_account_balance(cur, to_account, bal_to + float(amount))
            
            # 自动计算汇率损益 (FX_Loss) 并记录
            # 使用近似汇率 (例如假设基准汇率 7.2) 算多付的差额
            base_rate = 7.2
            cny_expected = float(amount) * base_rate
            loss = cny_spent - cny_expected
            if loss > 0:
                # 插入一笔外汇损失流水，计在 from_account 上
                sql_loss = f"""
                    INSERT INTO {TRANSACTIONS_TABLE} (date, amount, original_currency, from_account, transaction_type, category, remarks)
                    VALUES (%s, %s, 'CNY', %s, 'expense', 'FX_Loss', %s);
                """
                cur.execute(sql_loss, (date, loss, from_account, f"换汇还款差额损益，对应主转账金额: {amount} {curr_to}"))
        else:
            # 同币种转账，或未检测到差额标注的跨币种转账
            update_account_balance(cur, from_account, bal_from - float(amount))
            update_account_balance(cur, to_account, bal_to + float(amount))

import re

def insert_record(date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment=False, installment_months=1):
    """向云端数据库插入交易记录，同时处理信用卡分期逻辑"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # 处理信用卡分期逻辑
        if is_installment and installment_months > 1:
            # 方案一：录入时，把 N 期总额在各期以 amount/N 插入数据库（每次插入均会驱动余额扣减，最终合力扣减总额）
            # 流水生成：从当前月份开始，每期日期递增一个月
            start_date = datetime.strptime(date, "%Y-%m-%d").date()
            monthly_amount = round(float(amount) / installment_months, 2)
            
            for i in range(installment_months):
                curr_date = start_date + relativedelta(months=i)
                curr_date_str = curr_date.strftime("%Y-%m-%d")
                curr_remarks = f"【分期 {i+1}/{installment_months} 期】{remarks}"
                
                # 第 1 期为正常录入，后续期为 True 标记
                curr_is_inst = True if i > 0 else False
                
                insert_single_transaction_in_tx(
                    cur, 
                    date=curr_date_str, 
                    amount=monthly_amount, 
                    original_currency=original_currency,
                    from_account=from_account, 
                    to_account=to_account, 
                    transaction_type=transaction_type, 
                    category=category, 
                    remarks=curr_remarks,
                    is_installment=curr_is_inst
                )
        else:
            # 普通非分期单笔记录
            insert_single_transaction_in_tx(
                cur, 
                date=date, 
                amount=amount, 
                original_currency=original_currency,
                from_account=from_account, 
                to_account=to_account, 
                transaction_type=transaction_type, 
                category=category, 
                remarks=remarks,
                is_installment=is_installment
            )
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def apply_adjustment(account_name, target_balance, date, remarks):
    """
    对账校准逻辑 (adjustment)
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