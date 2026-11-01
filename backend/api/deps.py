"""Shared HTTP-layer state and response shapers.

Routers import this *module* (``from backend.api import deps``) and call
``deps.something`` rather than importing the name, so a test can do
``patch.object(deps, "VOICE_CARDS", store)`` and have the route actually see
it. Bindings captured at import time would silently ignore the patch.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, UploadFile

from backend.infra.logging import get_logger
from backend.schemas import (
    ConfigUpdate,
)
from backend.services import (
    config_guard,
    core,
    media_ingest,
)
from backend.services.gpu_arbiter import GPU_ARBITER
from backend.services.job_registry import JOB_REGISTRY
from backend.services.request_guard import api_key_registry
from backend.services.synthesis_jobs import SYNTHESIS_JOBS
from backend.services.timbre import TIMBRE_BACKEND
from backend.services.voice_assets import VoiceCardStore
from backend.services.voice_prepare_jobs import VOICE_PREPARE_JOBS
from backend.services.voice_selection import apply_voice_selection, list_voice_options
from tools.train import (
    job_log_path,
    last_job_id,
    step_status,
    tail_log,
)

logger = get_logger("xianghua.api")


def _as_url(download_id: str) -> str:
    return f"/api/files/{download_id}" if download_id else ""


# Versioned VoiceCard registry; replaced wholesale in tests via patch.object.
VOICE_CARDS = VoiceCardStore()


def _merge_config(update: ConfigUpdate) -> dict[str, Any]:
    config = core.load_config()
    payload = update.model_dump(
        exclude_none=True,
        exclude={"text", "output_dir", "zip_output", "voice_card_id", "style_id"},
    )
    # Optional mixed controls use null as an explicit request to restore dynamic defaults.
    for field in ("mixed_eos_floor", "mixed_token_cap"):
        if field in update.model_fields_set:
            payload[field] = getattr(update, field)
    # Model/reference paths are the one place ``path_guard`` did not cover: they
    # reach ``open()``, ``torch.load()`` and ``sys.path``/``import``. Validate the
    # ones the caller actually changed; an unchanged value is a round-trip, not a
    # new permission.
    try:
        config_guard.validate_config_update(payload, config)
    except config_guard.ConfigPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.update(payload)
    return config


def _root_or_400(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("gpt_sovits_root") or "")).expanduser()
    if not (root / "GPT_SoVITS").is_dir():
        raise HTTPException(status_code=400, detail="GPT-SoVITS 根目录不可用，请先在设置页配置。")
    return root


def _job_message(step: str, message: str) -> dict[str, Any]:
    return {"message": message, "step": step_status(step), "log": _current_log()}


def _record_api_key_chars(http_request: Request, text: str) -> None:
    """Meter synthesised characters against the calling tenant's API key."""
    registry = api_key_registry()
    if registry is None:
        return
    try:
        token = http_request.headers.get("x-api-key") or ""
        if not token:
            auth = http_request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        record = registry.authenticate(token) if token else None
        if record:
            registry.record_chars(str(record.get("key_id") or ""), len(text or ""))
    except Exception:
        pass  # 计量失败绝不影响合成


def _registered_url(path_value: str | Path, *, strict: bool = False) -> str:
    """Download URL for one export artifact ("" when the file was not produced).

    A *missing* file is a legitimate empty result — a script with no dialogue has
    no SRT. A file that exists but cannot be registered is a real failure and
    raises, because the previous silent ``""`` produced ``200 {"message": "已导出"}``
    with nothing to click.
    """
    candidate = Path(str(path_value or ""))
    if not candidate.is_file():
        return ""
    try:
        return _as_url(core.register_download(candidate))
    except Exception as exc:
        # Export artifacts are the user's deliverable, so a failure there is
        # fatal. Per-line status snapshots are polled continuously; one bad row
        # must not take the whole progress view down.
        if strict:
            raise HTTPException(
                status_code=500,
                detail=f"导出文件已生成，但下载凭据登记失败：{exc}",
            ) from exc
        logger.warning("下载凭据登记失败，该条目暂不可播放：%s（%s）", candidate.name, exc)
        return ""


def exported_output_dir(snapshot: dict[str, Any], outputs: list[dict[str, Any]]) -> Path:
    """Directory an episode export belongs in: the job's folder, else the first file's.

    ``Path("")`` is ``.`` and therefore truthy, so the old
    ``Path(...) or fallback`` never actually fell back — dialogue jobs (which
    used to store an empty ``output_dir``) wrote ``episode.srt`` into whatever
    directory the server process was started from.
    """
    raw = str(snapshot.get("output_dir") or "").strip()
    if raw:
        return Path(raw)
    first = next((item.get("file") for item in outputs if item.get("file")), "")
    return Path(str(first)).parent if first else Path.cwd()


def _public_export_result(result: dict[str, Any]) -> dict[str, Any]:
    """Attach download URLs to an episode export without exposing raw paths.

    Raises 500 when a produced artifact cannot be made downloadable, so the
    caller never sees "导出成功" next to an empty link.
    """
    data = dict(result)
    data["srt_url"] = _registered_url(data.get("srt") or "", strict=True)
    data["full_url"] = _registered_url(data.get("full") or "", strict=True)
    stems = data.get("stems") or {}
    # Raw paths stay server-side; only per-speaker download URLs go out.
    data.pop("stems", None)
    data["stem_urls"] = {speaker: _registered_url(path, strict=True) for speaker, path in stems.items()}

    produced = {"整集音轨": (data.get("full"), data["full_url"]), "字幕": (data.get("srt"), data["srt_url"])}
    missing = [name for name, (path, url) in produced.items() if path and not url]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"导出结果缺少可下载的{'、'.join(missing)}，请重试导出。",
        )
    return data


def _public_dialogue_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(snapshot or {})
    outputs: list[dict[str, Any]] = []
    for item in data.get("outputs") or []:
        row = dict(item)
        row.pop("file", None)
        row["url"] = _registered_url(str(item.get("file") or ""))
        outputs.append(row)
    data["outputs"] = outputs
    return data


def _public_synthesis_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(snapshot or {})
    outputs: list[dict[str, Any]] = []
    for item in data.get("outputs") or []:
        row = dict(item)
        row.pop("file", None)
        row["url"] = _as_url(str(row.get("download_id") or ""))
        outputs.append(row)
    data["outputs"] = outputs
    # Why take selection may be inactive right now ("模型不可用: ..."). It used to
    # be recorded on the judge and never returned, so silent quality degradation
    # was invisible.
    data["readback"] = core.READBACK_JUDGE.status()
    return data


def _public_synthesis_event(event: dict[str, Any]) -> dict[str, Any]:
    data = dict(event)
    if data.get("type") == "snapshot":
        data["snapshot"] = _public_synthesis_snapshot(data.get("snapshot"))
    return data


def _public_timbre_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Attach playable URLs to a finished conversion result.

    The result files live under ``output/vc/webui``; registering them as downloads is
    what lets the browser play them without exposing the filesystem path. Mix
    replacement produces several files, and all of them are useful: the finished song, the
    isolated converted vocal (so the user can hear what actually replaced the voice), and
    the extracted original vocal (so the user can judge the separator).
    """
    data = dict(snapshot or {})
    result = data.get("result")
    if isinstance(result, dict) and result.get("file"):
        row = dict(result)
        path = Path(str(row.pop("file")))
        row["url"] = _as_url(core.register_download(path)) if path.is_file() else ""
        for key, url_key in (
            ("converted_vocal", "converted_vocal_url"),
            ("vocal_stem", "vocal_stem_url"),
        ):
            candidate = Path(str(row.pop(key, "") or ""))
            row[url_key] = _as_url(core.register_download(candidate)) if candidate.is_file() else ""
        data["result"] = row
    return data


def _current_log(job_id: str | None = None) -> dict[str, str]:
    job_id = job_id or last_job_id()
    if not job_id:
        return {"job_id": "", "text": "（还没有任务，日志将显示在这里）"}
    status = step_status(job_id.split("_")[0])
    log = status.get("log") or job_log_path(job_id)
    return {"job_id": job_id, "text": tail_log(log, 80) or f"（任务 {job_id} 刚启动，日志暂为空）"}


def _transcript_path(root: Path) -> Path:
    return root / "custom_data" / "asr" / "sliced_whisper.list"


def _valid_experiment(value: str) -> str:
    name = (value or "myvoice").strip() or "myvoice"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise HTTPException(status_code=400, detail="实验名只能包含字母、数字、下划线或连字符。")
    return name


def _media_service(config: dict[str, Any] | None = None) -> media_ingest.MediaIngestService:
    cfg = config or core.load_config()
    root = _root_or_400(cfg)
    checkpoint = root / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"
    return media_ingest.MediaIngestService(root, checkpoint=checkpoint if checkpoint.is_file() else None)


def _public_media_session(service: media_ingest.MediaIngestService, session: dict[str, Any]) -> dict[str, Any]:
    """Attach download URLs without exposing the session directory to the browser."""
    data = copy.deepcopy(session)
    session_dir = service.session_dir(session["session_id"])
    segments = data.get("segments") or []
    for segment in segments:
        path = session_dir / str(segment.get("path") or "")
        segment["audio_url"] = _as_url(core.register_download(path)) if path.is_file() else ""
    for speaker in data.get("speakers") or []:
        sample = next(
            (item for item in segments if item.get("speaker_id") == speaker.get("speaker_id")),
            None,
        )
        speaker["sample_url"] = (sample or {}).get("audio_url", "")
    dataset = data.get("dataset")
    if isinstance(dataset, dict) and dataset.get("dataset_id"):
        dataset_id = str(dataset["dataset_id"])
        for key, filename in (
            ("list", "dataset.list"),
            ("manifest", "manifest.json"),
            ("checksums", "checksums.sha256"),
        ):
            path = session_dir / str(dataset.get(key) or "")
            dataset[f"{key}_url"] = (
                f"/api/media/sessions/{session['session_id']}/dataset/{dataset_id}/{filename}"
                if path.is_file()
                else ""
            )
    return data


_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "INVALID_ARGUMENT",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _error_code(status: int) -> str:
    """Stable machine-readable code for an HTTP status."""
    return _ERROR_CODES.get(int(status), f"HTTP_{int(status)}")


def _upload_filename(file: UploadFile, fallback: str = "upload") -> str:
    """Recover a UTF-8 multipart filename that Starlette decoded as latin-1.

    Starlette falls back to latin-1 whenever the part carries no explicit charset,
    so a Chinese name sent by curl/browser shows up as ``ºúÌÒ...`` in session
    metadata and on disk. Round-tripping the string through latin-1 recovers the
    original bytes; a name that is already real UTF-8 contains characters outside
    latin-1 and is returned unchanged instead of being mangled.
    """
    raw = file.filename or fallback
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
# Routers reach everything here as ``deps.<name>`` — never by importing the name
# into their own module. That indirection is the whole point: it lets a test do
# ``patch.object(deps, "JOB_REGISTRY", fake)`` and have the live route observe it.
# ``__all__`` also stops ruff's F401 pass from deleting the singletons, which are
# only referenced through the module object.
__all__ = [
    # process-wide singletons and services
    "GPU_ARBITER",
    "JOB_REGISTRY",
    "SYNTHESIS_JOBS",
    "TIMBRE_BACKEND",
    "VOICE_CARDS",
    "VOICE_PREPARE_JOBS",
    # response shaping / request helpers
    "_as_url",
    "_current_log",
    "_error_code",
    "_job_message",
    "_media_service",
    "_merge_config",
    "_public_dialogue_snapshot",
    "_public_export_result",
    "_public_media_session",
    "_public_synthesis_event",
    "_public_synthesis_snapshot",
    "_public_timbre_snapshot",
    "_record_api_key_chars",
    "_root_or_400",
    "_transcript_path",
    "_upload_filename",
    "_valid_experiment",
    "api_key_registry",
    "apply_voice_selection",
    "exported_output_dir",
    "list_voice_options",
]
