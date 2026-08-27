from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_actor
from app.db import transaction
from app.domain.transactions import (
    BatchNotFoundError,
    BatchVersionConflictError,
    InvalidRequestError
)
from app.services.reference_fx_service import ReferenceFxService
import app.services.snapshot_service as snapshot_service
import app.services.statement_service as statement_service
import app.services.reconciliation_service as reconciliation_service
import app.repositories.reconciliation as reconciliation_repo

router = APIRouter(prefix="/api/v1/reconciliation-batches", tags=["Reconciliation"])
candidates_router = APIRouter(prefix="/api/v1/reconciliation-candidates", tags=["Reconciliation Candidates"])


class BatchCommitRequest(BaseModel):
    row_version: Optional[int] = Field(None, description="Optional optimistic concurrency version")


class CandidateAcceptRequest(BaseModel):
    target_transaction_id: Optional[UUID] = Field(None, description="Explicit target transaction to match")


class CandidatePatchRequest(BaseModel):
    payload: Dict[str, Any] = Field(..., description="Updated candidate payload data")


class CandidateRejectRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for rejection")


# --- Batch Endpoints ---

@router.get("/{batch_id}", summary="Get Reconciliation Batch Summary")
def get_reconciliation_batch_endpoint(
    batch_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves the reconciliation batch workflow status and summary.
    Dispatches to statement_service or snapshot_service based on batch_type.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != device["household_id"]:
        raise BatchNotFoundError(batch_id)

    if batch["batch_type"] == "statement":
        return statement_service.get_statement_batch_summary(
            conn=conn,
            batch_id=batch_id,
            household_id=device["household_id"]
        )

    return snapshot_service.get_reconciliation_batch_summary(
        conn=conn,
        batch_id=batch_id,
        household_id=device["household_id"]
    )


@router.get("/{batch_id}/preview", summary="Get Reconciliation Preview")
def get_reconciliation_preview_endpoint(
    batch_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Returns read-only preview calculation for a reconciliation batch.
    Mutates zero database state.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != device["household_id"]:
        raise BatchNotFoundError(batch_id)

    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()

    if batch["batch_type"] == "statement":
        return statement_service.get_statement_batch_preview(
            conn=conn,
            batch_id=batch_id,
            household_id=device["household_id"],
            fx_service=fx_svc
        )

    return snapshot_service.get_reconciliation_preview(
        conn=conn,
        batch_id=batch_id,
        household_id=device["household_id"],
        fx_service=fx_svc
    )


@router.get("/{batch_id}/statement-lines", summary="List Statement Lines for Batch")
def get_statement_lines_endpoint(
    batch_id: UUID,
    match_status: Optional[str] = Query(None, description="Filter by match status"),
    line_type: Optional[str] = Query(None, description="Filter by line type"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Returns filtered statement lines for inspection.
    """
    return statement_service.list_statement_lines_for_batch(
        conn=conn,
        batch_id=batch_id,
        household_id=device["household_id"],
        match_status=match_status,
        line_type=line_type
    )


@router.post("/{batch_id}/commit", summary="Atomic Reconciliation Commit")
def commit_reconciliation_batch_endpoint(
    batch_id: UUID,
    payload: Optional[BatchCommitRequest] = None,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Atomically commits a reconciliation batch in a single transaction.
    Dispatches to statement reconciliation commit or snapshot commit.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    row_ver = payload.row_version if payload is not None else None

    with transaction(conn):
        batch = reconciliation_repo.lock_reconciliation_batch(conn, batch_id)
        if not batch or batch["household_id"] != device["household_id"]:
            raise BatchNotFoundError(batch_id)

        if batch["status"] in ("processing", "failed", "rejected"):
            raise InvalidRequestError(f"Reconciliation batch is in status '{batch['status']}' and cannot be committed.")

        if batch["batch_type"] not in ("statement", "snapshot"):
            raise InvalidRequestError(f"Unsupported batch type '{batch['batch_type']}'.")

        if batch["batch_type"] == "statement":
            if row_ver is not None and row_ver != batch["row_version"]:
                raise BatchVersionConflictError("The reconciliation batch changed. Reload before committing.")

            res = reconciliation_service.commit_statement_batch(
                conn=conn,
                batch_id=batch_id,
                user_id=device.get("user_id"),
                device_id=device.get("device_id"),
                fx_service=fx_svc
            )

            # If fresh re-evaluation under lock triggered needs_review:
            if res.get("status") == "needs_review":
                return res

            return {
                "status": "committed",
                "batch_id": str(batch_id),
                "summary": {
                    "matched_count": res["matched_count"],
                    "created_count": res["created_count"],
                    "adjustment_amount": str(res["adjustment_amount"]) if res.get("adjustment_amount") is not None else None
                }
            }

        # Snapshot batch commit
        return snapshot_service.commit_reconciliation_batch(
            conn=conn,
            batch_id=batch_id,
            device=device,
            row_version=row_ver,
            fx_service=fx_svc
        )


# --- Candidate Review Endpoints ---

@candidates_router.post("/{candidate_id}/accept", summary="Accept Reconciliation Candidate")
def accept_reconciliation_candidate_endpoint(
    candidate_id: UUID,
    payload: Optional[CandidateAcceptRequest] = None,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Accepts a reconciliation candidate proposal.
    Optionally binds an explicit target_transaction_id.
    """
    fx_svc = getattr(candidates_router, "_reference_fx_service", None) or ReferenceFxService()
    target_tx = payload.target_transaction_id if payload is not None else None

    with transaction(conn):
        return statement_service.accept_candidate(
            conn=conn,
            candidate_id=candidate_id,
            household_id=device["household_id"],
            user_id=device.get("user_id"),
            target_transaction_id=target_tx,
            device_id=device.get("device_id"),
            fx_service=fx_svc
        )


@candidates_router.patch("/{candidate_id}", summary="Edit Reconciliation Candidate Payload")
def patch_reconciliation_candidate_endpoint(
    candidate_id: UUID,
    payload: CandidatePatchRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Edits a candidate payload and recomputes batch readiness.
    """
    fx_svc = getattr(candidates_router, "_reference_fx_service", None) or ReferenceFxService()

    with transaction(conn):
        return statement_service.patch_candidate(
            conn=conn,
            candidate_id=candidate_id,
            household_id=device["household_id"],
            payload=payload.payload,
            user_id=device.get("user_id"),
            device_id=device.get("device_id"),
            fx_service=fx_svc
        )


@candidates_router.post("/{candidate_id}/reject", summary="Reject Reconciliation Candidate")
def reject_reconciliation_candidate_endpoint(
    candidate_id: UUID,
    payload: Optional[CandidateRejectRequest] = None,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Rejects a reconciliation candidate proposal and marks the statement line ignored.
    """
    fx_svc = getattr(candidates_router, "_reference_fx_service", None) or ReferenceFxService()
    reason = payload.reason if payload is not None else None

    with transaction(conn):
        return statement_service.reject_candidate(
            conn=conn,
            candidate_id=candidate_id,
            household_id=device["household_id"],
            reason=reason,
            user_id=device.get("user_id"),
            device_id=device.get("device_id"),
            fx_service=fx_svc
        )
