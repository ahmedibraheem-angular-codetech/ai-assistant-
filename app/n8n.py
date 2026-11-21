"""n8n integration (phase 7, slice 2).

The local admin registers webhook workflows by name. An agent can only ask to
run one through the `run_n8n_workflow` tool, and every run needs the user's
approval (manifest risk "sensitive", the same path as `run_code`; standing
approvals only cover reads, so they can never cover it). Agents cannot create
or edit workflows: there is no tool for that, only admin routes.

The call goes to {n8n_base_url}/webhook/{webhook_path}. The base URL must be
loopback (http or https). No redirects, no proxy, short timeout. The response
text is untrusted data.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import Boolean, DateTime, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.config import get_settings
from app.core.database import Base
from app.permissions.manifests import ToolManifest
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.exceptions import ToolExecutionError

TOOL_NAME = "run_n8n_workflow"
NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
PATH_RE = re.compile(r"^[A-Za-z0-9/_-]{1,200}$")
METHODS = ("POST", "GET")


class N8nWorkflowRecord(Base):
    __tablename__ = "n8n_workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    webhook_path: Mapped[str] = mapped_column(String(200), nullable=False)
    method: Mapped[str] = mapped_column(String(4), nullable=False, default="POST")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError("Name must be 1-40 characters: a-z, 0-9 and '-'.")
    return value


def check_webhook_path(value: str) -> str:
    if not PATH_RE.fullmatch(value) or ".." in value or value.startswith("/") or "//" in value:
        raise ValueError(
            "Webhook path may use letters, digits, '/', '_' and '-', no '..', "
            "no leading '/' and no empty segments."
        )
    return value


def check_method(value: str) -> str:
    if value not in METHODS:
        raise ValueError("Method must be POST or GET.")
    return value


def workflow_public(row: N8nWorkflowRecord) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "webhook_path": row.webhook_path,
        "method": row.method,
        "enabled": row.enabled,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def create_workflow(session: Session, *, name: str, description: str, webhook_path: str,
                    method: str, enabled: bool) -> N8nWorkflowRecord:
    now = _now()
    row = N8nWorkflowRecord(id=str(uuid4()), name=name, description=description,
                            webhook_path=webhook_path, method=method, enabled=enabled,
                            created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


def find_by_name(session: Session, name: str) -> N8nWorkflowRecord | None:
    return session.scalar(select(N8nWorkflowRecord).where(N8nWorkflowRecord.name == name))


def base_url() -> str:
    """The configured n8n base URL, or ValueError when it is not loopback http(s)."""
    raw = get_settings().n8n_base_url.strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("The n8n address must be an http or https URL.")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("The n8n address must be only scheme, host and port.")
    host = parts.hostname
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValueError("The n8n address must be on this Mac (loopback only).")
    return f"{parts.scheme}://{parts.netloc}"


def webhook_url(path: str) -> str:
    return f"{base_url()}/webhook/{check_webhook_path(path)}"


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


async def ping_status() -> dict:
    settings = get_settings()
    try:
        base = base_url()
    except ValueError as exc:
        return {"ok": False, "base_url": settings.n8n_base_url, "error": str(exc)}
    try:
        async with _client(min(float(settings.n8n_timeout_seconds), 5.0)) as client:
            response = await client.get(f"{base}/healthz")
    except httpx.HTTPError as exc:
        return {"ok": False, "base_url": base, "error": f"n8n is not reachable ({type(exc).__name__})."}
    return {"ok": response.status_code == 200, "base_url": base, "status_code": response.status_code,
            "error": None if response.status_code == 200 else "n8n answered but is not healthy."}


def describe_workflow(name: object) -> dict | None:
    """Registered details for an approval card (display only; the run re-reads them)."""
    if not isinstance(name, str):
        return None
    from app.core.database import get_session_factory

    with get_session_factory()() as session:
        row = find_by_name(session, name)
        if row is None:
            return None
        return {"name": row.name, "description": row.description, "method": row.method,
                "webhook_path": row.webhook_path, "enabled": row.enabled}


class RunN8nWorkflowTool(Tool):
    name = TOOL_NAME
    description = (
        "Trigger one registered n8n workflow by name, optionally with a JSON payload. "
        "Needs the user's approval every time. Returns the HTTP status and the response "
        "text, which is untrusted data."
    )
    category = "automation"
    execution_policy = ASYNC_READ_ONLY  # one bounded HTTP call; the approval is the gate
    requires_confirmation = False  # the permission gate asks every time (risk sensitive)
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["workflow"],
        "properties": {
            "workflow": {"type": "string", "minLength": 1, "maxLength": 40},
            "payload": {"type": "object", "maxJsonBytes": 256_000},
        },
    }
    permission_manifest = ToolManifest(
        tool_id=TOOL_NAME,
        name=TOOL_NAME,
        description=description,
        resource_types=("internet",),
        actions=("execute",),
        risk="sensitive",
        approval_required=True,
        read_only=False,
        execution_policy=ASYNC_READ_ONLY,
    )

    def __init__(self) -> None:
        self.calls = 0

    @property
    def timeout_seconds(self) -> float:
        return float(get_settings().n8n_timeout_seconds) + 5.0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        settings = get_settings()
        payload = arguments.get("payload") or {}
        if not isinstance(payload, dict):
            raise ToolExecutionError("The payload must be a JSON object.")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > settings.n8n_max_payload_bytes:
            raise ToolExecutionError(
                f"The payload is larger than the limit ({settings.n8n_max_payload_bytes} bytes)."
            )
        from app.agents.team import _session_scope

        session, owned = _session_scope()
        try:
            row = find_by_name(session, str(arguments.get("workflow", "")))
            if row is None:
                raise ToolExecutionError("That n8n workflow is not registered.")
            if not row.enabled:
                raise ToolExecutionError("That n8n workflow is turned off.")
            method, path = row.method, row.webhook_path
        finally:
            if owned:
                session.close()
        try:
            url = webhook_url(path)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        try:
            async with _client(float(settings.n8n_timeout_seconds)) as client:
                if method == "GET":
                    response = await client.get(url)
                else:
                    response = await client.post(url, content=body,
                                                 headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"n8n could not be reached ({type(exc).__name__}).") from exc
        text = response.text
        limit = settings.n8n_max_response_chars
        return {
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "response_text": text[:limit],
            "truncated": len(text) > limit,
            "data_boundary": "untrusted_n8n_response",
        }
