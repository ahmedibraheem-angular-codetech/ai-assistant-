"""Local recovery of quarantined or overwritten text files.

This is not a tool and it does not list file contents.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.permissions import require_local_admin
from app.files.changes import list_recoveries, restore_recovery
from app.files.exceptions import FileAccessError

router = APIRouter()


@router.get("/file-recoveries")
def recoveries(_local: None = Depends(require_local_admin)):
    rows = list_recoveries()
    return {"recoveries": rows, "count": len(rows)}


@router.post("/file-recoveries/{recovery_id}/restore")
def restore(recovery_id: str, _local: None = Depends(require_local_admin)):
    try:
        return restore_recovery(recovery_id)
    except FileAccessError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
