import os
import psycopg2
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ 错误: 未能在环境变量或 .env 文件中找到 DATABASE_URL！")
    exit(1)

# 定义预设账户及类型（完成 Alipay_Investment 的拆分）
PRESET_ACCOUNTS = {
    # 现金钱包类 (cash)
    "WeChat_Pay": "cash",
    "Alipay_Cash": "cash",
    "ICBC_Debit": "cash",
    "BOC_Debit": "cash",
    "BOB_Debit": "cash",
    "ABC_Debit": "cash",
    
    # 信用负债类 (credit)
    "Huabei": "credit",
    "ABC_CUP_Credit": "credit",
    "ICBC_CUP_Credit": "credit",
    "BOC_CUP_Credit": "credit",
    "ICBC_Visa_Credit": "credit",
    "CCB_Visa_Credit": "credit",
    
    # 储蓄存款类 (savings)
    "ICBC_Savings": "savings",
    "BOC_Savings": "savings",
    "BOB_Savings": "savings",
    "Kunlun_Savings": "savings",
    
    # 理财投资类 (investment) (新增拆分后的账户，移除旧的 Alipay_Investment)
    "Alipay_Stable_Wealth": "investment",
    "Alipay_Advanced_Investment": "investment",
    "Broker_Stocks": "investment",
    "ICBC_Wealth": "investment",
    "BOC_Wealth": "investment",
    "BOB_Wealth": "investment"
}

def migrate():
    print("⏳ 正在建立 Supabase 数据库连接...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        print("✅ 数据库连接成功！")
        
        # 1. 创建 accounts_dev 表
        print("⏳ 正在创建/检查 accounts_dev 表...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts_dev (
                id SERIAL PRIMARY KEY,
                account_name TEXT UNIQUE NOT NULL,
                account_type TEXT NOT NULL CHECK (account_type IN ('cash', 'credit', 'savings', 'investment')),
                current_balance NUMERIC(12, 2) DEFAULT 0.00,
                currency TEXT DEFAULT 'CNY',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2. 创建 transactions_dev 表
        print("⏳ 正在创建/检查 transactions_dev 表...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions_dev (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                original_currency TEXT DEFAULT 'CNY',
                from_account TEXT REFERENCES accounts_dev(account_name),
                to_account TEXT REFERENCES accounts_dev(account_name),
                transaction_type TEXT NOT NULL CHECK (transaction_type IN ('expense', 'income', 'transfer', 'adjustment')),
                category TEXT NOT NULL,
                remarks TEXT,
                is_installment BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 3. 注入种子数据前，先将两个新拆分的账户注入（防止下面的外键更新出错）
        print("⏳ 正在注入预设账户种子数据...")
        for name, acc_type in PRESET_ACCOUNTS.items():
            currency = "USD" if "Visa" in name else "CNY"
            cur.execute("""
                INSERT INTO accounts_dev (account_name, account_type, current_balance, currency)
                VALUES (%s, %s, 0.00, %s)
                ON CONFLICT (account_name) DO NOTHING;
            """, (name, acc_type, currency))
            
        # 4. 安全地将数据库中旧的 'Alipay_Investment' 外键记录更新，并删除旧账户
        print("⏳ 正在处理旧的 'Alipay_Investment' 账户迁移...")
        # 将历史流水中关联旧账户的记录安全迁移到 'Alipay_Stable_Wealth'
        cur.execute("""
            UPDATE transactions_dev 
            SET from_account = 'Alipay_Stable_Wealth' 
            WHERE from_account = 'Alipay_Investment';
        """)
        cur.execute("""
            UPDATE transactions_dev 
            SET to_account = 'Alipay_Stable_Wealth' 
            WHERE to_account = 'Alipay_Investment';
        """)
        # 删除旧的账户记录
        cur.execute("DELETE FROM accounts_dev WHERE account_name = 'Alipay_Investment';")
        
        conn.commit()
        print("🎉 恭喜！数据库 DDL 建表与种子数据更新/迁移成功！")
        
        # 验证输出当前数据
        cur.execute("SELECT account_name, account_type, current_balance, currency FROM accounts_dev;")
        rows = cur.fetchall()
        print(f"\n📊 检查开发环境 accounts_dev 表（当前共 {len(rows)} 个账户）:")
        for row in rows:
            print(f"  - 账户: {row[0]:<28} | 类型: {row[1]:<10} | 余额: {row[2]:<8} | 币种: {row[3]}")
            
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ 运行过程中发生错误: {e}")
        if 'conn' in locals() and conn:
            conn.rollback()

if __name__ == "__main__":
    migrate()
