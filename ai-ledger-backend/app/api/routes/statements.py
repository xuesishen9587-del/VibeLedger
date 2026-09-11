from uuid import UUID
from fastapi import APIRouter, Depends, UploadFile, File, Form
from app.api.deps import get_db_connection, require_browser_auth, require_idempotency_key
from app.api.routes.expenses import capture_response
from app.domain.spending import fail
from app.services import statement_import, capture_receipts
from app.services.statement_document import StatementDocumentParser, MAX_BYTES

router=APIRouter(prefix="/api/v1/accounts",tags=["Statement imports"])

def get_statement_parser():
    return StatementDocumentParser()

@router.post("/{account_id}/statement-imports")
def upload(account_id:UUID, file:UploadFile=File(...), password:str | None=Form(None,max_length=200),
           actor=Depends(require_browser_auth),conn=Depends(get_db_connection),key=Depends(require_idempotency_key),parser=Depends(get_statement_parser)):
    try:
        content=file.file.read(MAX_BYTES+1)
        if len(content)>MAX_BYTES:
            fail("STATEMENT_LIMIT_EXCEEDED","The PDF exceeds 20 MiB.")
        return capture_response(statement_import.upload(capture_receipts.connection_factory(conn),actor,account_id,key,content,password,parser))
    finally:
        file.file.close()
