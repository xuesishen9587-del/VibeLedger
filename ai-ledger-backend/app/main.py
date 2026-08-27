from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from app.domain.transactions import LedgerDomainError
from app.domain.auth import AuthError
from app.api.errors import (
    ledger_domain_exception_handler,
    auth_domain_exception_handler,
    validation_exception_handler,
    http_exception_handler,
    global_exception_handler
)
from app.api.routes.expenses import router as expenses_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.accounts import router as accounts_router
from app.api.routes.categories import router as categories_router
from app.api.routes.transactions import router as transactions_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.credit_cards import router as credit_cards_router
from app.api.routes.installments import router as installments_router
from app.api.routes.snapshots import router as snapshots_router
from app.api.routes.reconciliation import router as reconciliation_router, candidates_router as reconciliation_candidates_router
from app.api.routes.statements import router as statements_router
from app.api.routes.investments import router as investments_router
from app.api.routes.devices import router as devices_router
from app.api.routes.work_queue import router as work_queue_router
from app.api.routes.audit import router as audit_router

def create_app() -> FastAPI:
    """
    Creates and configures the target VibeLedger FastAPI application.
    """
    app = FastAPI(
        title="VibeLedger API",
        description="Target API for VibeLedger Personal Finance Ledger",
        version="1.0.0"
    )

    # Register Exception Handlers
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(LedgerDomainError, ledger_domain_exception_handler)
    app.add_exception_handler(AuthError, auth_domain_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, global_exception_handler)

    # Register Routers
    app.include_router(expenses_router)
    app.include_router(ingestion_router)
    app.include_router(accounts_router)
    app.include_router(categories_router)
    app.include_router(transactions_router)
    app.include_router(dashboard_router)
    app.include_router(credit_cards_router)
    app.include_router(installments_router)
    app.include_router(snapshots_router)
    app.include_router(reconciliation_router)
    app.include_router(reconciliation_candidates_router)
    app.include_router(statements_router)
    app.include_router(investments_router)
    app.include_router(devices_router)
    app.include_router(work_queue_router)
    app.include_router(audit_router)

    @app.get("/health", tags=["Health"])
    @app.get("/api/v1/health", tags=["Health"])
    def health_check():
        return {"status": "ok", "service": "vibeledger-api", "version": "1.0.0"}

    @app.get("/ready", tags=["Health"])
    def readiness_check():
        return {"status": "ok", "database": "ok"}

    return app

app = create_app()
