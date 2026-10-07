"""Request models for the local XiangHua API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SynthesisLanguage = Literal["zh", "yue", "en", "ja", "ko"]


class ConfigUpdate(BaseModel):
    gpt_sovits_root: str | None = None
    gpt_model: str | None = None
    sovits_model: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    text_lang: SynthesisLanguage | None = None
    prompt_lang: SynthesisLanguage | None = None
    top_k: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    temperature: float | None = Field(default=None, gt=0, le=2)
    sample_steps: int | None = Field(default=None, ge=1)
    speed: float | None = Field(default=None, gt=0, le=3)
    pause_second: float | None = Field(default=None, ge=0, le=10)
    voice_profile_id: str | None = None
    accent_id: str | None = None
    mixed_eos_floor: int | None = Field(default=None, ge=0)
    mixed_token_cap: int | None = Field(default=None, ge=0)
    auto_lightmid: bool | None = None
    theme: str | None = None
    orb_synthesis_token_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    orb_settled_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    orb_playback_token_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator(
        "orb_synthesis_token_color",
        "orb_settled_color",
        "orb_playback_token_color",
        mode="before",
    )
    @classmethod
    def normalize_orb_color(cls, value: str | None) -> str | None:
        return str(value).upper() if value is not None else None


class SynthesisRequest(ConfigUpdate):
    text: str


class EnvironmentSelectRequest(BaseModel):
    path: str


class TranscriptUpdate(BaseModel):
    rows: list[dict[str, Any]]


class ModelRunRequest(BaseModel):
    model: str = "large-v3"


class FeatureRequest(BaseModel):
    experiment: str = "myvoice"


class TrainRequest(FeatureRequest):
    s1_epochs: int = Field(default=15, ge=1)
    s2_epochs: int = Field(default=12, ge=1)


class StopRequest(BaseModel):
    job_id: str | None = None


class TimbreConvertRequest(BaseModel):
    """Convert an audio file that is already on disk into a profile's timbre.

    The WebUI uses this to move a synthesis result into the user's own voice
    without downloading and re-uploading it. Exactly one of ``source_path`` or
    ``download_id`` has to resolve to a readable file.
    """

    source_path: str | None = None
    download_id: str | None = None
    profile_id: str = "current"
    topk: int = Field(default=4, ge=1, le=64)
    device: str | None = None


class TimbreProfileRequest(BaseModel):
    profile_id: str = "current"
    device: str | None = None


class DoctorRequest(BaseModel):
    repair: bool = False
