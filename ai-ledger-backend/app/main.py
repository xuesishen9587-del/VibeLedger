import os
from typing import Any
from fastapi import FastAPI, HTTPException, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.transactions import LedgerDomainError
from app.domain.auth import AuthError
from app.api.deps import get_db_connection
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
        import logging
        from app.db import get_connection
        from migrations.runner import get_migration_files

        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        gemini_status = "ok" if (gemini_api_key and gemini_api_key.strip()) else "unavailable"

        conn = None
        try:
            target_schema = os.environ.get("DB_SCHEMA")
            try:
                from app.config import get_settings
                s = get_settings()
                if s and s.db_schema:
                    target_schema = s.db_schema
            except Exception:
                pass
            conn = get_connection(schema=target_schema)
        except Exception as e:
            logging.getLogger("app.readiness").error(f"Readiness DB connection acquisition failed: {e}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "database": "unavailable",
                    "gemini": gemini_status
                }
            )

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = current_schema() AND table_name = 'schema_migrations'
                    );
                    """
                )
                has_migrations = cur.fetchone()[0]
                if not has_migrations:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "status": "unavailable",
                            "database": "schema_not_ready",
                            "gemini": gemini_status
                        }
                    )

                expected_files = get_migration_files()
                cur.execute("SELECT migration_name FROM schema_migrations;")
                applied_files = {row[0] for row in cur.fetchall()}

                missing = set(expected_files) - applied_files
                if missing or len(applied_files) < len(expected_files):
                    return JSONResponse(
                        status_code=503,
                        content={
                            "status": "unavailable",
                            "database": "schema_not_ready",
                            "gemini": gemini_status
                        }
                    )
        except Exception as e:
            logging.getLogger("app.readiness").error(f"Readiness check execution failed: {e}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "database": "unavailable",
                    "gemini": gemini_status
                }
            )
        finally:
            if conn and not conn.closed:
                conn.close()

        overall_status = "ok" if gemini_status == "ok" else "degraded"
        return {
            "status": overall_status,
            "database": "ok",
            "gemini": gemini_status
        }

    return app

app = create_app()
