from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import (
    style,
    agents,
    approvals,
    analytics,
    insights,
    voice,
    recoveries,
    chat,
    control,
    conversations,
    graph,
    health,
    learning,
    memories,
    permissions,
    projects,
    n8n,
    routines,
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
    from app.core.controls import load as load_controls

    load_controls()
    # WP-05A: take the serve lease and load the token before serving anything.
    lease = ServeLease.acquire()
    _app.state.auth_token = lease.token
    try:
        async with _serving(_app):
            scheduler = None
            if get_settings().study_scheduler_enabled:
                import asyncio

                from app.learning.study import scheduler_loop

                scheduler = asyncio.create_task(scheduler_loop())
            routines_task = None
            if get_settings().routine_scheduler_enabled:
                import asyncio

                from app.routines import scheduler_loop as routine_loop

                routines_task = asyncio.create_task(routine_loop())
            try:
                yield
            finally:
                for task in (scheduler, routines_task):
                    if task is not None:
                        task.cancel()
                        try:
                            await task
                        except BaseException:
                            pass
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
app.include_router(control.router, prefix=settings.api_prefix, tags=["Control"])
app.include_router(projects.router, prefix=settings.api_prefix, tags=["Projects"])
app.include_router(graph.router, prefix=settings.api_prefix, tags=["Knowledge graph"])
app.include_router(n8n.router, prefix=settings.api_prefix, tags=["n8n"])
app.include_router(routines.router, prefix=settings.api_prefix, tags=["Routines"])
app.include_router(learning.router, prefix=settings.api_prefix, tags=["Learning"])
app.include_router(analytics.router, prefix=settings.api_prefix, tags=["Analytics"])
app.include_router(insights.router, prefix=settings.api_prefix, tags=["Insights"])
app.include_router(voice.router, prefix=settings.api_prefix, tags=["Voice"])
app.include_router(style.router, prefix=settings.api_prefix, tags=["Style"])
