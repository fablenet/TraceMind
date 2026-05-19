"""Routes for managing LLM prompt templates and configuration registry."""

from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from tm.llm.config_registry import LlmConfigRegistry
from tm.server.workspace_manager import WorkspaceManager
from tm.workspace.manifest import Workspace


class PromptTemplateInfo(BaseModel):
    version: str
    title: str
    description: str | None = None


class LlmConfigInfo(BaseModel):
    config_id: str
    model: str
    prompt_template_version: str
    prompt_version: str
    created_at: str
    model_id: str | None = None
    model_version: str | None = None


class LlmConfigCreateRequest(BaseModel):
    model: str
    prompt_template_version: str
    prompt_version: str | None = None
    model_id: str | None = None
    model_version: str | None = None


def _resolve_workspace(workspace_manager: WorkspaceManager, workspace_id: str | None) -> Workspace:
    try:
        return workspace_manager.get(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _prompt_templates_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "controllers" / "decide" / "prompt_templates"


def _load_prompt_templates() -> list[PromptTemplateInfo]:
    path = _prompt_templates_dir()
    if not path.exists():
        return []
    templates: list[PromptTemplateInfo] = []
    for entry in sorted(path.glob("*.md")):
        data = entry.read_text(encoding="utf-8").splitlines()
        version = entry.stem
        description: str | None = None
        title: str | None = None
        for line in data:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("prompt_template_version"):
                parts = stripped.split(":", 1)
                if len(parts) > 1:
                    version = parts[1].strip()
                continue
            if stripped.startswith("#") and title is None:
                title = stripped.lstrip("#").strip()
                continue
            if description is None:
                description = stripped
                continue
        templates.append(PromptTemplateInfo(version=version, title=title or version, description=description))
    return templates


def _template_versions() -> set[str]:
    return {template.version for template in _load_prompt_templates()}


def create_llm_router(manager: WorkspaceManager) -> APIRouter:
    router = APIRouter(prefix="/api/v1/llm", tags=["llm"])

    @router.get("/prompt-templates", response_model=list[PromptTemplateInfo])
    def list_prompt_templates() -> list[PromptTemplateInfo]:
        return _load_prompt_templates()

    @router.get("/configs", response_model=list[LlmConfigInfo])
    def list_configs(
        workspace_id: str | None = Query(None, description="Workspace that owns the config"),
    ) -> list[LlmConfigInfo]:
        workspace = _resolve_workspace(manager, workspace_id)
        registry = LlmConfigRegistry(workspace.paths.llm_configs)
        return [LlmConfigInfo(**entry.to_dict()) for entry in registry.list()]

    @router.post("/configs", response_model=LlmConfigInfo)
    def create_config(
        payload: LlmConfigCreateRequest,
        workspace_id: str | None = Query(None, description="Workspace that owns the config"),
    ) -> LlmConfigInfo:
        workspace = _resolve_workspace(manager, workspace_id)
        versions = _template_versions()
        if payload.prompt_template_version not in versions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"prompt template '{payload.prompt_template_version}' is not available",
            )
        registry = LlmConfigRegistry(workspace.paths.llm_configs)
        entry = registry.add(
            model=payload.model,
            prompt_template_version=payload.prompt_template_version,
            prompt_version=payload.prompt_version,
            model_id=payload.model_id,
            model_version=payload.model_version,
        )
        return LlmConfigInfo(**entry.to_dict())

    return router
