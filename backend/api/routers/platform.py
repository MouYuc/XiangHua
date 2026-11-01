"""Platform concerns: API keys, provenance verification, files, history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from backend.api import deps
from backend.schemas import (
    ApiKeyCreateRequest,
    ProvenanceVerifyRequest,
)
from backend.services import (
    core,
    provenance,
)
from backend.services.path_guard import PathConfinementError, confined_file
from backend.services.request_guard import api_key_registry

router = APIRouter()


@router.post("/provenance/verify")
def verify_provenance_route(request: ProvenanceVerifyRequest) -> dict[str, Any]:
    source: Path | None = None
    if request.download_id:
        source = core.resolve_download(request.download_id)
    if source is None and request.path:
        try:
            source = confined_file(request.path)
        except PathConfinementError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    if source is None or not source.is_file():
        raise HTTPException(status_code=404, detail="找不到要校验的音频文件。")
    return provenance.verify_provenance(source)

# ------------------------------------------------------ 租户 API Key ----
# 管理 XIANGHUA_API_KEYS_FILE 指向的多租户密钥注册表；token 只在创建时返回一次。

@router.get("/api-keys")
def list_api_keys() -> dict[str, Any]:
    registry = api_key_registry()
    if registry is None:
        raise HTTPException(
            status_code=409,
            detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
        )
    return {"keys": registry.list_keys()}

@router.post("/api-keys")
def create_api_key(request: ApiKeyCreateRequest) -> dict[str, Any]:
    registry = api_key_registry()
    if registry is None:
        raise HTTPException(
            status_code=409,
            detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
        )
    try:
        record = registry.create_key(
            request.tenant,
            label=request.label,
            daily_request_quota=request.daily_request_quota,
            rpm_limit=request.rpm_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message": (
            f"已为租户「{record['tenant']}」创建 API Key。"
            "令牌只显示这一次，请立即保存。"
        ),
        "key": record,
    }

@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str) -> Response:
    registry = api_key_registry()
    if registry is None:
        raise HTTPException(
            status_code=409,
            detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
        )
    if not registry.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="API Key 不存在。")
    return Response(status_code=204)

@router.get("/history")
def history() -> list[dict[str, Any]]:
    entries = core.load_history_with_downloads()
    for entry in entries:
        entry.pop("file", None)
        entry["url"] = deps._as_url(str(entry.get("download_id") or ""))
    return entries

@router.delete("/history", status_code=204)
def delete_history() -> Response:
    core.clear_history()
    return Response(status_code=204)

@router.get("/files")
def deprecated_file_query() -> None:
    raise HTTPException(status_code=404, detail="请使用下载链接。")

@router.get("/files/{download_id}")
def serve_file(download_id: str) -> FileResponse:
    candidate = core.resolve_download(download_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="下载文件不存在或已不可用。")
    return FileResponse(candidate, filename=candidate.name)
