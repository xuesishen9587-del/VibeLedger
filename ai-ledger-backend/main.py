import os
import base64
from typing import Literal, Optional, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import database
import traceback
from dotenv import load_dotenv

# 加载 .env 配置文件
load_dotenv()

# 初始化数据库
database.init_db()

app = FastAPI(title="Vibe Finance API 2.0", description="iOS Shortcuts Backend for Gemini Asset-Liability Center Bookkeeping")

# 初始化 Gemini 客户端 (从系统环境变量或 .env 读取 GEMINI_API_KEY)
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None

# 定义支持 Jun 字典
VALID_ACCOUNTS = Literal[
    # cash
    "WeChat_Pay", "Alipay_Cash", "ICBC_Debit", "BOC_Debit", "BOB_Debit", "ABC_Debit",
    # credit
    "Huabei", "ABC_CUP_Credit", "ICBC_CUP_Credit", "BOC_CUP_Credit", "ICBC_Visa_Credit", "CCB_Visa_Credit",
    # savings
    "ICBC_Savings", "BOC_Savings", "BOB_Savings", "Kunlun_Savings",
    # investment (Alipay_Investment has been split into Stable and Advanced)
    "Alipay_Stable_Wealth", "Alipay_Advanced_Investment", "Broker_Stocks", "ICBC_Wealth", "BOC_Wealth", "BOB_Wealth"
]

# 定义支持的交易分类字典
VALID_CATEGORIES = Literal[
    # 日常支出分类
    "Grocery", "Dine", "Child", "Home & Utilities", "Digital & Gadgets", 
    "Clothing", "Beauty", "Transportation", "Health", "Education", 
    "Gift & Socials", "Parents", "Fun & Games", "Trips & Occasions",
    # 主动收入分类
    "Salary", "Reimbursement", "Gift_Social_Income", "Professional_Fees", "Other_Income",
    # 投资理财收益分类 (根据资产风险类型细化)
    "Interest_Income", "Stable_Investment_Income", "Advanced_Investment_Income",
    # 系统与特殊调整分类
    "Balance_Correction", "FX_Loss"
]

# 1. 快捷指令请求体模型
class RecordRequest(BaseModel):
    image: str = Field(..., description="Base64 encoded string of the screenshot")
    note: str = Field("", description="User manual notes added via shortcut prompt")

class AccountBalance(BaseModel):
    account: VALID_ACCOUNTS = Field(..., description="The account name to be adjusted.")
    balance: float = Field(..., description="The absolute current balance/amount of this account.")

# 2. 结构化大模型解析的返回模型
class ParsedTransaction(BaseModel):
    date: str = Field(..., description="The date of the transaction in YYYY-MM-DD format. If not explicitly found in screenshot or notes, assume current date.")
    amount: Optional[float] = Field(None, description="The total transaction cost or amount. Optional for 'adjustment' type if 'adjustments' list is populated.")
    original_currency: str = Field("CNY", description="The currency of the transaction. E.g. CNY, USD.")
    transaction_type: Literal["expense", "income", "transfer", "adjustment"] = Field(
        ..., 
        description="Type of the transaction. 'expense' for spending, 'income' for earnings, 'transfer' for moving funds between accounts, 'adjustment' for bank card balance snapshots."
    )
    from_account: Optional[VALID_ACCOUNTS] = Field(
        None, 
        description="The account where the funds flow FROM. REQUIRED for 'expense' and 'transfer' types. Must be null for 'income'."
    )
    to_account: Optional[VALID_ACCOUNTS] = Field(
        None, 
        description="The account where the funds flow TO. REQUIRED for 'income', 'transfer' types. Optional for 'adjustment' if adjustments list is used."
    )
    category: VALID_CATEGORIES = Field(
        "Balance_Correction", 
        description="The category of this transaction. For transfer types use 'Balance_Correction'. For balance adjustment, use 'Balance_Correction'."
    )
    remarks: str = Field(..., description="A summary of what was bought combined with user notes or a summary of the accounts adjusted.")
    is_installment: bool = Field(False, description="Flag true if this is a credit card installment transaction (e.g. split into N months).")
    installment_months: int = Field(1, description="The number of months for the credit card installment. Defaults to 1.")
    adjustments: Optional[List[AccountBalance]] = Field(
        None,
        description="REQUIRED for 'adjustment' type if the screenshot or notes show current balances for one or more accounts. List all accounts and their absolute balances."
    )

# 3. 视觉提示词 (System Prompt) 升级
SYSTEM_PROMPT = """
You are a precise financial accounting assistant. Your job is to analyze a financial screenshot (receipt, bank transfer, payroll slip, or account balance page) AND a text note provided by the user, then extract the structured transaction data.

STRICT RULES ON TRANSACTION TYPE:
1. EXPENSE: Standard consumption receipts (e.g. WeChat Pay/Alipay merchant payment). Required: 'from_account' (source wallet/card), 'to_account' must be null.
2. INCOME: Earnings (salary slips, bank interest notices, transfers received). Required: 'to_account' (destination wallet/card), 'from_account' must be null. Choose the correct category (e.g., Salary for salary, Interest_Income/Stable_Investment_Income/Advanced_Investment_Income for investment returns, Gift_Social_Income for red packets received, Professional_Fees for writing/lecturing/consulting).
3. TRANSFER: Repayments (debit card to credit card), bank transfers between owned cards. Required: both 'from_account' and 'to_account'. Set category to 'Balance_Correction'.
4. ADJUSTMENT: Screenshots showing the current balance of one or more accounts. Required: set 'transaction_type' to 'adjustment', set 'category' to 'Balance_Correction', and populate the 'adjustments' list with all visible accounts and their absolute balances.

CREDIT CARD LIABILITY ADJUSTMENT RULE (CRITICAL):
For credit card accounts (e.g., Huabei, ABC_CUP_Credit, ICBC_CUP_Credit, BOC_CUP_Credit, ICBC_Visa_Credit, CCB_Visa_Credit):
- If the screenshot or note shows an outstanding balance, a bill to pay, or debt (e.g. "欠款 ¥3,000", "应还款 3,000", "已用额度 3,000"), the value in the adjustments list MUST be output as a NEGATIVE number (e.g. -3000.00).
- Credit card balances represent liabilities and must be negative or zero. Do not record debt as a positive asset balance.

ACCOUNT DICTIONARY matching rules:
Map any identified payment method or bank name to one of:
- cash: WeChat_Pay, Alipay_Cash, ICBC_Debit, BOC_Debit, BOB_Debit, ABC_Debit
- credit: Huabei, ABC_CUP_Credit, ICBC_CUP_Credit, BOC_CUP_Credit, ICBC_Visa_Credit, CCB_Visa_Credit
- savings: ICBC_Savings, BOC_Savings, BOB_Savings, Kunlun_Savings
- investment: Alipay_Stable_Wealth (for low risk wealth management), Alipay_Advanced_Investment (for high risk mutual funds/stocks on Alipay), Broker_Stocks, ICBC_Wealth, BOC_Wealth, BOB_Wealth

BANK-SPECIFIC NOMENCLATURE & AGGREGATION RULES FOR ADJUSTMENTS:
1. BOB (北京银行) Screen:
   - Identify "理财" (Wealth Management) and "基金" (Funds). SUM them together and assign the total to "BOB_Wealth".
   - "活期" -> "BOB_Debit"
   - "存款" -> "BOB_Savings"
   - IGNORE "总资产" (Total Assets).
2. BOC (中国银行) Screen:
   - Identify "定期存款" (Time Deposit), "柜台债券/债" (OTC Bonds), and "储蓄国债" (Treasury Bonds). SUM them together and assign the total to "BOC_Savings".
   - "理财" -> "BOC_Wealth"
   - "活期存款" -> "BOC_Debit"
   - IGNORE "总资产" (Total Assets).
3. ICBC (工商银行) Screen:
   - Identify "储蓄国债" (Treasury Bonds), "定期" (Time Deposit), and "柜台债券/债" (OTC Bonds). SUM them together and assign the total to "ICBC_Savings".
   - "理财" -> "ICBC_Wealth"
   - "活期" -> "ICBC_Debit"
   - IGNORE "总资产" (Total Assets).
4. Alipay (支付宝) Screen:
   - "余额" or "活期资产" -> "Alipay_Cash"
   - "稳健理财" -> "Alipay_Stable_Wealth"
   - "进阶理财" -> "Alipay_Advanced_Investment"
   - IGNORE "总资产" (Total Assets).
5. Brokerage Stocks (Broker_Stocks) Screen:
   - Identify "账户资产", "总资产" or "资产总值" (which includes stock market value + cash) instead of just "证券市值" (stock value only) to record as the balance for "Broker_Stocks".

INSTALLMENT DETECTIONS:
If the user note contains "分期 N期" (e.g., "分期 12期" or "分期 3期"), set 'is_installment' = true and extract the number of months into 'installment_months'.

Output MUST strictly match the required JSON structure.
"""

@app.post("/api/record")
async def record_transaction(payload: RecordRequest):
    if not client:
        raise HTTPException(status_code=500, detail="Gemini API Key is not configured on the server.")
    
    try:
        # 解码 base64 图像
        image_bytes = base64.b64decode(payload.image)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        
        # 组装用户 input
        user_prompt = f"User's manual note/context: {payload.note}"
        contents = [image_part, user_prompt]
        
        # 调用 Gemini API 并使用 Pydantic 约束输出
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite", 
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ParsedTransaction,
            ),
        )
        
        parsed_data = response.parsed
        
        if not parsed_data:
            raise HTTPException(status_code=422, detail="AI failed to parse the image content structure safely.")
        
        # 处理特别的分期标识备注注入
        note_lower = payload.note.lower()
        import re
        installment_match = re.search(r'分期\s*(\d+)\s*(?:期|月)', note_lower)
        if installment_match:
            parsed_data.is_installment = True
            parsed_data.installment_months = int(installment_match.group(1))
            
        # 根据交易类型分流处理落库
        if parsed_data.transaction_type == 'adjustment':
            # 上帝平账逻辑：根据绝对数值水位调平（支持多账户列表）
            adjusted_details = []
            if parsed_data.adjustments and len(parsed_data.adjustments) > 0:
                for adj in parsed_data.adjustments:
                    database.apply_adjustment(
                        account_name=adj.account,
                        target_balance=adj.balance,
                        date=parsed_data.date,
                        remarks=parsed_data.remarks
                    )
                    adjusted_details.append(f"【{adj.account}】为 ￥{adj.balance:.2f} 元")
                msg = f"成功上帝批量平账：" + "，".join(adjusted_details) + "。"
            elif parsed_data.to_account and parsed_data.amount is not None:
                database.apply_adjustment(
                    account_name=parsed_data.to_account,
                    target_balance=parsed_data.amount,
                    date=parsed_data.date,
                    remarks=parsed_data.remarks
                )
                msg = f"成功上帝单账户平账：【{parsed_data.to_account}】当前水位校准为 ￥{parsed_data.amount:.2f} 元。"
            else:
                raise HTTPException(status_code=422, detail="Adjustment type requires either adjustments list or to_account and amount.")
        else:
            # 普通收支与转账
            if parsed_data.amount is None:
                raise HTTPException(status_code=422, detail="Transaction amount is required for non-adjustment types.")
            database.insert_record(
                date=parsed_data.date,
                amount=parsed_data.amount,
                original_currency=parsed_data.original_currency,
                from_account=parsed_data.from_account,
                to_account=parsed_data.to_account,
                transaction_type=parsed_data.transaction_type,
                category=parsed_data.category,
                remarks=parsed_data.remarks,
                is_installment=parsed_data.is_installment,
                installment_months=parsed_data.installment_months
            )
            
            # 格式化成功返回消息
            if parsed_data.transaction_type == 'transfer':
                msg = f"成功转账：【{parsed_data.from_account}】-> 【{parsed_data.to_account}】 ￥{parsed_data.amount:.2f} 元。备注：{parsed_data.remarks}"
                if parsed_data.original_currency != 'CNY':
                    msg = f"成功转账：【{parsed_data.from_account}】-> 【{parsed_data.to_account}】 {parsed_data.amount:.2f} {parsed_data.original_currency}。备注：{parsed_data.remarks}"
            elif parsed_data.transaction_type == 'income':
                msg = f"成功记账(收入)：【{parsed_data.to_account}】 收入 ￥{parsed_data.amount:.2f} 元。备注：{parsed_data.remarks}"
            else:
                inst_txt = f" (分期 {parsed_data.installment_months} 期)" if parsed_data.is_installment else ""
                msg = f"成功记账(支出)：【{parsed_data.from_account}】 消费 ￥{parsed_data.amount:.2f} 元{inst_txt}。分类：{parsed_data.category}。备注：{parsed_data.remarks}"
                
        return {
            "status": "success",
            "message": msg
        }
        
    except Exception as e:
        print("====== [ERROR] FastAPI 后端发生崩溃 ======")
        traceback.print_exc()
        print("=========================================")
        return {"status": "error", "message": f"云端崩溃原因: {str(e)}"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}