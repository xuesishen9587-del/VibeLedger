from fastapi import FastAPI, HTTPException
from app.domain.transactions import LedgerDomainError
from app.api.errors import (
    ledger_domain_exception_handler,
    http_exception_handler,
    global_exception_handler
)
from app.api.routes.expenses import router as expenses_router
from app.api.routes.ingestion import router as ingestion_router

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
    app.add_exception_handler(LedgerDomainError, ledger_domain_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, global_exception_handler)

    # Register Routers
    app.include_router(expenses_router)
    app.include_router(ingestion_router)

    @app.get("/api/v1/health", tags=["Health"])
    def health_check():
        return {"status": "healthy", "service": "vibeledger-api", "version": "1.0.0"}

    return app

app = create_app()
