from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import (
    agents,
    approvals,
    recoveries,
    chat,
    conversations,
    health,
    memories,
    permissions,
    tools,
    workspaces,
)
from app.core.auth import LocalAuthMiddleware, ServeLease
from app.core.config import get_settings
from app.core.database import init_database

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    host = get_settings().api_host.strip().casefold()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "The API must stay on localhost. Remote access is not supported."
        )
    init_database()
    # WP-05A: take the serve lease and load the token before serving anything.
    lease = ServeLease.acquire()
    _app.state.auth_token = lease.token
    try:
        async with _serving(_app):
            yield
    finally:
        _app.state.auth_token = None
        lease.release()


@asynccontextmanager
async def _serving(_app: FastAPI):
    from app.approvals.reconcile import reconcile_linked_approvals
    from app.files.journal import reconcile_file_journal

    reconcile_file_journal()
    reconcile_linked_approvals("startup")
    from app.commands.audit import reconcile_command_audits

    reconcile_command_audits()
    from app.knowledge.store import reconcile_staging

    reconcile_staging()
    from app.mcp.loader import install_sighup, recover_configuration, restore_sighup

    recover_configuration()
    from app.approvals.mcp_policy import settle_mcp_approvals
    from app.mcp.payload import cleanup_abandoned_payload_keys

    cleanup_abandoned_payload_keys()
    settle_mcp_approvals()
    previous_sighup = install_sighup()
    try:
        yield
    finally:
        from app.mcp.session import shutdown_all

        try:
            shutdown_all()
        finally:
            restore_sighup(previous_sighup)


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Local-first personal AI assistant backend",
    lifespan=lifespan,
)
# Outermost layer: every request except the health check needs the Bearer token.
app.add_middleware(LocalAuthMiddleware, api_prefix=settings.api_prefix)

app.include_router(health.router, prefix=settings.api_prefix, tags=["System"])
app.include_router(chat.router, prefix=settings.api_prefix, tags=["Chat"])
app.include_router(agents.router, prefix=settings.api_prefix, tags=["Agents"])
app.include_router(conversations.router, prefix=settings.api_prefix, tags=["Conversations"])
app.include_router(memories.router, prefix=settings.api_prefix, tags=["Memories"])
app.include_router(tools.router, prefix=settings.api_prefix, tags=["Tools"])
app.include_router(workspaces.router, prefix=settings.api_prefix, tags=["Workspaces"])
app.include_router(permissions.router, prefix=settings.api_prefix, tags=["Permissions"])
app.include_router(approvals.router, prefix=settings.api_prefix, tags=["Approvals"])
app.include_router(recoveries.router, prefix=settings.api_prefix, tags=["Recoveries"])
