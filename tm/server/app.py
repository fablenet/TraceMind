"""Entry point for the TraceMind TM server HTTP API."""

from fastapi import FastAPI

from tm.server.config import ServerConfig
from tm.server.routes_controller import create_controller_router, create_controller_v1_router
from tm.server.routes_init import create_init_router
from tm.server.routes_llm import create_llm_router
from tm.server.routes_artifacts import create_artifact_router
from tm.server.routes_meta import create_meta_router
from tm.server.routes_workspace import create_workspace_router
from tm.server.routes_network import create_network_router
from tm.server.routes_runs import create_runs_router
from tm.server.workspace_manager import WorkspaceManager


def create_app(config: ServerConfig | None = None) -> FastAPI:
    cfg = config or ServerConfig()
    manager = WorkspaceManager()
    app = FastAPI(title="TraceMind Controller Server API")
    app.include_router(create_init_router())
    app.include_router(create_workspace_router(manager))
    app.include_router(create_llm_router(manager))
    app.include_router(create_artifact_router(cfg, manager))
    app.include_router(create_meta_router())
    app.include_router(create_controller_router(cfg, manager))
    app.include_router(create_controller_v1_router(cfg, manager))
    app.include_router(create_network_router())
    app.include_router(create_runs_router(cfg, manager))
    return app


def _create_default_app() -> FastAPI:
    return create_app()


app = _create_default_app()
