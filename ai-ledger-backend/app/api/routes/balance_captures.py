from fastapi import APIRouter, Depends
from app.api.deps import get_auth_context, get_db_connection
from app.api.routes.expenses import CreateExpenseRequest, capture_response
from app.services.balance_extractor import BalanceExtractor
from app.services import balance_capture, capture_receipts

router=APIRouter(prefix="/api/v1/balance-captures",tags=["Balance capture"])

def get_balance_model():
    return BalanceExtractor()

@router.post("")
def capture(payload:CreateExpenseRequest, actor=Depends(get_auth_context), conn=Depends(get_db_connection), model=Depends(get_balance_model)):
    return capture_response(balance_capture.process(capture_receipts.connection_factory(conn),actor,payload.model_dump(),model))
