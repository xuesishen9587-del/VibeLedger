import sys
import os
import psycopg2
from datetime import datetime

# 将 backend 路径加入到 sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import database

def run_tests():
    print("🧪 开始运行 VibeLedger 核心功能测试...")
    
    # 执行数据库 DDL 初始化/表升级
    database.init_db()
    
    # 1. 初始化测试用账户
    conn = database.get_db_connection()
    cur = conn.cursor()
    
    try:
        # 清理旧测试账户和流水
        cur.execute(f"DELETE FROM {database.TRANSACTIONS_TABLE} WHERE from_account IN ('Test_CNY_Debit', 'Test_USD_Credit') OR to_account IN ('Test_CNY_Debit', 'Test_USD_Credit');")
        cur.execute(f"DELETE FROM {database.ACCOUNTS_TABLE} WHERE account_name IN ('Test_CNY_Debit', 'Test_USD_Credit');")
        
        # 写入测试账户
        cur.execute(f"""
            INSERT INTO {database.ACCOUNTS_TABLE} (account_name, account_type, current_balance, currency, billing_day, due_day)
            VALUES 
                ('Test_CNY_Debit', 'cash', 1000.00, 'CNY', NULL, NULL),
                ('Test_USD_Credit', 'credit', -100.00, 'USD', 5, 25);
        """)
        conn.commit()
        print("✅ 测试账户 'Test_CNY_Debit' (1000.00 CNY) 和 'Test_USD_Credit' (-100.00 USD) 初始化成功！")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ 初始化测试账户失败: {e}")
        return
    finally:
        cur.close()
        conn.close()

    # --- 测试 1: 正常记录写入与 Tier 1 幂等键物理去重 ---
    print("\n--- 测试 1: 正常记录与 Tier 1 幂等去重 ---")
    try:
        # 第一次写入
        database.insert_record(
            date="2026-08-14",
            amount=50.00,
            original_currency="CNY",
            from_account="Test_CNY_Debit",
            to_account=None,
            transaction_type="expense",
            category="Grocery",
            remarks="测试购买咖啡",
            idempotency_key="req_key_coffee_1"
        )
        print("✅ 首次写入成功！")
        
        # 验证余额是否扣减
        conn = database.get_db_connection()
        cur = conn.cursor()
        bal, _ = database.get_account_balance_and_currency(cur, "Test_CNY_Debit")
        assert bal == 950.00, f"余额不正确，预期 950.00，实际: {bal}"
        print(f"✅ 余额原子扣减验证成功，当前余额: {bal}")
        
        # 第二次写入相同的 key（模拟网络重试）
        try:
            database.insert_record(
                date="2026-08-14",
                amount=50.00,
                original_currency="CNY",
                from_account="Test_CNY_Debit",
                to_account=None,
                transaction_type="expense",
                category="Grocery",
                remarks="测试购买咖啡",
                idempotency_key="req_key_coffee_1"
            )
            print("❌ 错误: 相同的幂等键没有被拦截！")
        except ValueError as ve:
            assert str(ve) == "DUPLICATE_TRANSACTION", f"预期异常 DUPLICATE_TRANSACTION，实际: {ve}"
            print("✅ 相同的幂等键被成功拦截并抛出 DUPLICATE_TRANSACTION 异常！")
            
        # 验证余额没有被二次扣减
        bal, _ = database.get_account_balance_and_currency(cur, "Test_CNY_Debit")
        assert bal == 950.00, f"重试后余额异常，实际: {bal}"
        print("✅ 重试去重余额防双扣验证成功！")
        
    except Exception as e:
        print(f"❌ 测试 1 失败: {e}")
    finally:
        cur.close()
        conn.close()

    # --- 测试 2: Tier 2 业务内容比对去重（防手动重复录入） ---
    print("\n--- 测试 2: Tier 2 业务内容去重 ---")
    try:
        # 手动换一个幂等键记账，但内容完全相同
        try:
            database.insert_record(
                date="2026-08-14",
                amount=50.00,
                original_currency="CNY",
                from_account="Test_CNY_Debit",
                to_account=None,
                transaction_type="expense",
                category="Grocery",
                remarks="测试购买咖啡",
                idempotency_key="req_key_coffee_2"
            )
            print("❌ 错误: 相同内容但不同幂等键的手动重复录入没有被拦截！")
        except ValueError as ve:
            assert str(ve) == "DUPLICATE_TRANSACTION", f"预期拦截，实际: {ve}"
            print("✅ 相同内容的手动重复录入被成功拦截！")
            
        # 换一个不同的备注，应该允许写入（合法相同消费）
        database.insert_record(
            date="2026-08-14",
            amount=50.00,
            original_currency="CNY",
            from_account="Test_CNY_Debit",
            to_account=None,
            transaction_type="expense",
            category="Grocery",
            remarks="测试购买第二杯咖啡", # 备注不同
            idempotency_key="req_key_coffee_3"
        )
        print("✅ 不同备注的同额消费写入成功！")
        
        conn = database.get_db_connection()
        cur = conn.cursor()
        bal, _ = database.get_account_balance_and_currency(cur, "Test_CNY_Debit")
        assert bal == 900.00, f"当前余额不符合预期 900.00，实际: {bal}"
        print(f"✅ 合法第二笔消费扣减成功，当前余额: {bal}")
        
    except Exception as e:
        print(f"❌ 测试 2 失败: {e}")
    finally:
        cur.close()
        conn.close()

    # --- 测试 3: 分期付款去重与清洗备注 ---
    print("\n--- 测试 3: 分期付款与分期备注比对去重 ---")
    try:
        # 插入 3 期分期
        database.insert_record(
            date="2026-08-14",
            amount=300.00,
            original_currency="CNY",
            from_account="Test_CNY_Debit",
            to_account=None,
            transaction_type="expense",
            category="Grocery",
            remarks="买路由器",
            is_installment=True,
            installment_months=3,
            idempotency_key="req_key_inst"
        )
        print("✅ 3 期分期付款首次写入成功！")
        
        # 尝试使用同一个 key 重试
        try:
            database.insert_record(
                date="2026-08-14",
                amount=300.00,
                original_currency="CNY",
                from_account="Test_CNY_Debit",
                to_account=None,
                transaction_type="expense",
                category="Grocery",
                remarks="买路由器",
                is_installment=True,
                installment_months=3,
                idempotency_key="req_key_inst"
            )
            print("❌ 错误: 分期付款重试未被幂等拦截！")
        except ValueError as ve:
            assert str(ve) == "DUPLICATE_TRANSACTION", f"预期拦截，实际: {ve}"
            print("✅ 分期付款重试被成功幂等拦截！")
            
        # 尝试使用全新的 key 录入相同内容
        try:
            database.insert_record(
                date="2026-08-14",
                amount=300.00,
                original_currency="CNY",
                from_account="Test_CNY_Debit",
                to_account=None,
                transaction_type="expense",
                category="Grocery",
                remarks="买路由器",
                is_installment=True,
                installment_months=3,
                idempotency_key="req_key_inst_new"
            )
            print("❌ 错误: 即使分期生成了【分期 i/N 期】前缀，手动录入也应该能被去重拦截！")
        except ValueError as ve:
            assert str(ve) == "DUPLICATE_TRANSACTION", f"预期拦截，实际: {ve}"
            print("✅ 备注剥离分期前缀去重成功，成功防范了手动重复录入！")
            
    except Exception as e:
        print(f"❌ 测试 3 失败: {e}")

    # --- 测试 4: 跨币种转账安全校验与智能账单推导 ---
    print("\n--- 测试 4: 跨币种转账安全校验与智能账单推导 ---")
    try:
        # 首先校验：如果缺失必要信息，且非全额还款，是否拦截？
        try:
            database.insert_record(
                date="2026-08-14",
                amount=100.00,
                original_currency="USD",
                from_account="Test_CNY_Debit",
                to_account="Test_USD_Credit",
                transaction_type="transfer",
                category="Balance_Correction",
                remarks="部分还款", # 备注不包含还清
                idempotency_key="req_x_rate_fail"
            )
            print("❌ 错误: 信息缺失的跨币种转账未被拦截！")
        except ValueError as ve:
            assert str(ve) == "CROSS_CURRENCY_MISSING_INFO", f"预期拦截，实际: {ve}"
            print("✅ 信息不全的跨币种转账被成功拦截，未发生 1:1 降级记错账！")
            
        # 验证备注明确人民币金额是否成功？
        database.insert_record(
            date="2026-08-14",
            amount=50.00,
            original_currency="USD",
            from_account="Test_CNY_Debit",
            to_account="Test_USD_Credit",
            transaction_type="transfer",
            category="Balance_Correction",
            remarks="还款信用卡50美元，实际扣除362.5元",
            idempotency_key="req_x_rate_pass1"
        )
        print("✅ 带有显式人民币扣款的跨币种还款录入成功！")
        
        # 验证全额还款自动反推账单逻辑
        # 之前对账单进行扣减：Test_USD_Credit 余额 -100.00 USD
        # 我们用备注写“全额还款”且不写美元数额
        database.insert_record(
            date="2026-08-14",
            amount=100.00, # 这里的 amount 表示转入 credit card (USD)
            original_currency="USD",
            from_account="Test_CNY_Debit",
            to_account="Test_USD_Credit",
            transaction_type="transfer",
            category="Balance_Correction",
            remarks="全额还清，实际扣除725元",
            idempotency_key="req_x_rate_pass2"
        )
        print("✅ 全额还款推导外币金额并录入成功！")
        
    except Exception as e:
        print(f"❌ 测试 4 失败: {e}")

    print("\n🎉 VibeLedger 核心功能测试运行结束！")

if __name__ == "__main__":
    run_tests()
