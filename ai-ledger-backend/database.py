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
    
    # 3. 动态升级表结构以支持请求幂等、跨币种转账以及信用卡账单日/还款日机制 (Issue 01, 02, 03, 05)
    cur.execute(f"""
        ALTER TABLE {TRANSACTIONS_TABLE} 
        ADD COLUMN IF NOT EXISTS idempotency_key TEXT UNIQUE,
        ADD COLUMN IF NOT EXISTS parent_idempotency_key TEXT,
        ADD COLUMN IF NOT EXISTS from_amount NUMERIC(12, 2),
        ADD COLUMN IF NOT EXISTS from_currency TEXT,
        ADD COLUMN IF NOT EXISTS to_amount NUMERIC(12, 2),
        ADD COLUMN IF NOT EXISTS to_currency TEXT,
        ADD COLUMN IF NOT EXISTS fx_rate NUMERIC(12, 6);
        
        ALTER TABLE {ACCOUNTS_TABLE}
        ADD COLUMN IF NOT EXISTS billing_day INTEGER,
        ADD COLUMN IF NOT EXISTS due_day INTEGER;
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"🚀 {ACCOUNTS_TABLE} 和 {TRANSACTIONS_TABLE} 表初始化/核对成功！")

def check_idempotency_key_exists_in_tx(cur, idempotency_key: str) -> bool:
    """内部辅助函数：检查给定的幂等键（主键或父键）是否已在数据库中存在"""
    if not idempotency_key:
        return False
    cur.execute(f"""
        SELECT id FROM {TRANSACTIONS_TABLE}
        WHERE idempotency_key = %s OR parent_idempotency_key = %s
        LIMIT 1;
    """, (idempotency_key, idempotency_key))
    return cur.fetchone() is not None

def check_idempotency_key_exists(idempotency_key: str) -> bool:
    """外部接口函数：检查给定的幂等键是否已存在"""
    if not idempotency_key:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        return check_idempotency_key_exists_in_tx(cur, idempotency_key)
    finally:
        cur.close()
        conn.close()

def clean_remarks(remarks: str) -> str:
    """去除备注中的分期前缀，便于比对去重"""
    if not remarks:
        return ""
    # 匹配类似 “【分期 1/12 期】” 或 “【分期 12/12】” 等格式
    return re.sub(r'^【分期\s*\d+/\d+\s*期?】\s*', '', remarks).strip()

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
        
    # 寻找最近的已出账单日 S
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

def update_account_balance_delta(cur, account_name, delta):
    """辅助函数：并发安全地增减指定账户当前余额（原子更新）"""
    cur.execute(
        f"UPDATE {ACCOUNTS_TABLE} SET current_balance = current_balance + %s, updated_at = CURRENT_TIMESTAMP WHERE account_name = %s;",
        (delta, account_name)
    )

def update_account_balance_absolute(cur, account_name, absolute_balance):
    """辅助函数：将账户余额设置为绝对水位（对账用）"""
    cur.execute(
        f"UPDATE {ACCOUNTS_TABLE} SET current_balance = %s, updated_at = CURRENT_TIMESTAMP WHERE account_name = %s;",
        (absolute_balance, account_name)
    )

def insert_single_transaction_in_tx(cur, date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment=False, idempotency_key=None, parent_idempotency_key=None, from_amount=None, from_currency=None, to_amount=None, to_currency=None, fx_rate=None):
    """
    在已有事务(cur)中执行单笔流水写入，并在底层驱动账户水位并发安全更新。
    支持跨币种转账的精确入库与校验（不降级 1:1，自动汇率计算，支持还清反推）。
    """
    # 状态机：原子更新账户余额水位
    if transaction_type == 'expense':
        if not from_account:
            raise ValueError("支出类型交易(expense)必须指定 from_account")
        update_account_balance_delta(cur, from_account, -float(amount))
        
    elif transaction_type == 'income':
        if not to_account:
            raise ValueError("收入类型交易(income)必须指定 to_account")
        update_account_balance_delta(cur, to_account, float(amount))
        
    elif transaction_type == 'transfer':
        if not from_account or not to_account:
            raise ValueError("转账类型交易(transfer)必须同时指定 from_account 和 to_account")
            
        # 获取两端账户信息
        cur.execute(f"SELECT account_type, currency FROM {ACCOUNTS_TABLE} WHERE account_name = %s;", (from_account,))
        from_acc_data = cur.fetchone()
        cur.execute(f"SELECT account_type, currency FROM {ACCOUNTS_TABLE} WHERE account_name = %s;", (to_account,))
        to_acc_data = cur.fetchone()
        
        if not from_acc_data or not to_acc_data:
            raise ValueError(f"账户不存在：{from_account} 或 {to_account}")
            
        curr_from = from_acc_data['currency']
        curr_to = to_acc_data['currency']
        
        # 1. 确定防死锁锁定顺序（按账户名字典排序）
        lock_order = sorted([from_account, to_account])
        for acc in lock_order:
            cur.execute(f"SELECT id FROM {ACCOUNTS_TABLE} WHERE account_name = %s FOR UPDATE;", (acc,))
            
        # 2. 判断是否跨币种
        if curr_from != curr_to:
            resolved_from_amount = from_amount
            resolved_to_amount = to_amount
            
            # 根据原始交易的币种，将 amount 映射到对应的转入或转出端
            if original_currency == curr_from and resolved_from_amount is None:
                resolved_from_amount = float(amount)
            elif original_currency == curr_to and resolved_to_amount is None:
                resolved_to_amount = float(amount)
            
            # 尝试从备注提取实际的人民币扣款金额
            if resolved_from_amount is None:
                match = re.search(r'(?:实际扣除|扣除|支付)\s*([\d\.]+)\s*(?:元|cny|rmb)', remarks, re.IGNORECASE) if remarks else None
                if match:
                    resolved_from_amount = float(match.group(1))
            
            # 智能推导：如果是全额还信用卡，自动根据信用卡欠款推导应还外币金额
            if resolved_to_amount is None:
                is_full_pay = any(k in remarks for k in ["还清", "全额", "还卡", "全额还款"]) if remarks else False
                if is_full_pay and to_acc_data['account_type'] == 'credit':
                    stmt_info = get_credit_card_statement_info(cur, to_account, date)
                    if stmt_info['remaining_due'] > 0:
                        resolved_to_amount = stmt_info['remaining_due']
            
            # 如果依然无法完全确定两个币种的具体金额，报错拒绝写入
            if resolved_from_amount is None or resolved_to_amount is None:
                raise ValueError("CROSS_CURRENCY_MISSING_INFO")
                
            resolved_fx_rate = round(resolved_from_amount / resolved_to_amount, 6)
            
            # 扣除/转入各自对应币种的金额
            update_account_balance_delta(cur, from_account, -resolved_from_amount)
            update_account_balance_delta(cur, to_account, resolved_to_amount)
            
            # 覆写最终存入流水的属性
            from_amount = resolved_from_amount
            from_currency = curr_from
            to_amount = resolved_to_amount
            to_currency = curr_to
            fx_rate = resolved_fx_rate
            
        else:
            # 同币种转账直接原子扣减与增加
            update_account_balance_delta(cur, from_account, -float(amount))
            update_account_balance_delta(cur, to_account, float(amount))
            
            from_amount = float(amount)
            from_currency = curr_from
            to_amount = float(amount)
            to_currency = curr_to
            fx_rate = 1.000000
            
    # 写入交易流水表
    sql_insert = f"""
        INSERT INTO {TRANSACTIONS_TABLE} (
            date, amount, original_currency, from_account, to_account, 
            transaction_type, category, remarks, is_installment, 
            idempotency_key, parent_idempotency_key,
            from_amount, from_currency, to_amount, to_currency, fx_rate
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    cur.execute(
        sql_insert, 
        (
            date, amount, original_currency, from_account, to_account, 
            transaction_type, category, remarks, is_installment, 
            idempotency_key, parent_idempotency_key,
            from_amount, from_currency, to_amount, to_currency, fx_rate
        )
    )

def insert_record(date, amount, original_currency, from_account, to_account, transaction_type, category, remarks, is_installment=False, installment_months=1, idempotency_key=None):
    """向云端数据库插入交易记录，同时处理信用卡分期逻辑（带双层防重校验）"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Tier 1: 幂等性主/父键物理去重
        if idempotency_key and check_idempotency_key_exists_in_tx(cur, idempotency_key):
            raise ValueError("DUPLICATE_TRANSACTION")
            
        # Tier 2: 业务内容比对去重（剥离分期前缀，防手动重复记账）
        cleaned_req_remarks = clean_remarks(remarks)
        
        # 针对分期付款，数据库中存储的单期金额是 amount / installment_months
        target_db_amount = round(float(amount) / installment_months, 2) if is_installment and installment_months > 1 else amount
        
        cur.execute(f"""
            SELECT remarks FROM {TRANSACTIONS_TABLE}
            WHERE date = %s AND amount = %s AND original_currency = %s
              AND (from_account = %s OR (from_account IS NULL AND %s IS NULL))
              AND (to_account = %s OR (to_account IS NULL AND %s IS NULL))
              AND transaction_type = %s AND category = %s;
        """, (date, target_db_amount, original_currency, from_account, from_account, to_account, to_account, transaction_type, category))
        
        existing_txs = cur.fetchall()
        for tx in existing_txs:
            if clean_remarks(tx['remarks']) == cleaned_req_remarks:
                raise ValueError("DUPLICATE_TRANSACTION")
                
        # 处理信用卡分期逻辑
        if is_installment and installment_months > 1:
            start_date = datetime.strptime(date, "%Y-%m-%d").date()
            monthly_amount = round(float(amount) / installment_months, 2)
            
            for i in range(installment_months):
                curr_date = start_date + relativedelta(months=i)
                curr_date_str = curr_date.strftime("%Y-%m-%d")
                curr_remarks = f"【分期 {i+1}/{installment_months} 期】{remarks}"
                
                # 第一期为正常录入，后续期为 True 标记
                curr_is_inst = True if i > 0 else False
                
                # 为每期生成唯一的子幂等键
                sub_key = f"{idempotency_key}_inst_{i}" if idempotency_key else None
                
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
                    is_installment=curr_is_inst,
                    idempotency_key=sub_key,
                    parent_idempotency_key=idempotency_key
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
                is_installment=is_installment,
                idempotency_key=idempotency_key
            )
            
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        # 捕获数据库唯一键冲突
        if e.pgcode == '23505':
            raise ValueError("DUPLICATE_TRANSACTION")
        raise e
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def apply_adjustment(account_name, target_balance, date, remarks, idempotency_key=None):
    """
    对账校准逻辑 (adjustment)
    计算真实绝对水位与数据库当前水位差值，并自动生成一笔类型为 adjustment 的流水。
    对于投资账户，其差值分类为对应的投资收益分类，但类型为 adjustment 从而与常规现金流解耦。
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Tier 1: 幂等键物理去重
        if idempotency_key and check_idempotency_key_exists_in_tx(cur, idempotency_key):
            raise ValueError("DUPLICATE_TRANSACTION")
            
        # 获取账户当前类型与币种
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
            
        # 判定是否为投资收益账务自动转为收入分类
        if acc_type == 'investment' and bal != 0.00:
            if account_name in ['Broker_Stocks', 'Alipay_Advanced_Investment']:
                category = 'Advanced_Investment_Income'
            else:
                category = 'Stable_Investment_Income'
            # 投资收益记为对账类型 adjustment，在表现层进行现金流合并与过滤
            tx_type = 'adjustment'
            from_acc = None
            to_acc = account_name
            amount = diff  # 保留正负号
        else:
            # 普通现金、储蓄卡、信用卡平账
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
        
        # 强制将账户余额设定为最新目标绝对水位
        update_account_balance_absolute(cur, account_name, target_balance)
        
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if e.pgcode == '23505':
            raise ValueError("DUPLICATE_TRANSACTION")
        raise e
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