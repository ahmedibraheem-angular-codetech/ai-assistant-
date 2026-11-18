"""Local approval decisions. Agents and model payloads cannot call these routes."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_approval_service
from app.api.routes.permissions import require_local_admin
from app.approvals.service import (
    ApprovalConflict,
    ApprovalNotFound,
    ApprovalNotReviewable,
    ApprovalService,
    to_public,
)

router = APIRouter()


def _public_list(records) -> dict:
    items = [to_public(record) for record in records]
    return {"approvals": items, "count": len(items)}


@router.get("/approvals")
def list_pending(
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    return _public_list(service.list_pending())


@router.get("/approvals/history")
def approval_history(
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    return _public_list(service.history())


@router.get("/approvals/{request_id}")
def get_approval(
    request_id: str,
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    try:
        return to_public(service.get(request_id))
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/approvals/{request_id}/approve")
async def approve_request(
    request_id: str,
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    try:
        body = to_public(await service.approve(request_id))
        body["output"] = service.last_output
        return body
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": exc.status}) from exc
    except ApprovalNotReviewable as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": "NOT_REVIEWABLE"}) from exc


@router.post("/approvals/{request_id}/reject")
def reject_request(
    request_id: str,
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    try:
        return to_public(service.reject(request_id))
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": exc.status}) from exc


@router.post("/approvals/{request_id}/cancel")
def cancel_request(
    request_id: str,
    _local: None = Depends(require_local_admin),
    service: ApprovalService = Depends(get_approval_service),
):
    try:
        return to_public(service.cancel(request_id))
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": exc.status}) from exc
