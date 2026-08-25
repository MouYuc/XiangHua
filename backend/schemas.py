"""Request models for the local XiangHua API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConfigUpdate(BaseModel):
    gpt_sovits_root: str | None = None
    gpt_model: str | None = None
    sovits_model: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    top_k: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    temperature: float | None = Field(default=None, gt=0, le=2)
    sample_steps: int | None = Field(default=None, ge=1)
    speed: float | None = Field(default=None, gt=0, le=3)
    pause_second: float | None = Field(default=None, ge=0, le=10)
    auto_lightmid: bool | None = None
    theme: str | None = None


class SynthesisRequest(ConfigUpdate):
    text: str


class BatchRequest(ConfigUpdate):
    text: str
    output_dir: str | None = None
    zip_output: bool = False


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


class DoctorRequest(BaseModel):
    repair: bool = False
