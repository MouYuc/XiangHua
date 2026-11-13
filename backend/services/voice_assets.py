"""Versioned voice assets: VoiceCard and StylePack data contracts.

Implements the contracts sketched in ``docs/ARCHITECTURE.md`` §3.2–3.4:

- **VoiceCard** — a named, versioned timbre asset. Only the timbre (reference
  audio + base models) is captured from the current configuration; pronunciation
  lexicon, accent level and style are stored as *defaults* that each synthesis
  request may override. Versions are append-only: re-ingesting a voice and
  saving appends a new version, history is never rewritten, and any historical
  version can be re-activated.
- **StylePack** — prosody presets mapped onto existing synthesis knobs
  (``speed`` / ``pause_second``) so style stays out of the timbre layer.
- **Accent** — reuses ``pronunciation.py``'s ``off/light/medium/heavy`` levels;
  ``accent_pack_id`` is persisted for the future standalone accent packs.

Storage layout (append-only, one JSON per card)::

    <CONFIG_DIR>/voice_cards/<voice_id>/card.json

The quality-gate philosophy applies here too: a card whose reference audio is
missing is reported via ``timbre_available=false`` but is still loadable, so
users are informed rather than blocked.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.domain.pronunciation import (
    LEXICON_ZH_BASE,
    accent_intensity_for,
    build_pronunciation_plan,
)
from backend.domain.text import SUPPORTED_SYNTHESIS_LANGUAGES
from backend.infra.paths import CONFIG_DIR, REPO_ROOT

STYLE_PACK_FILE = REPO_ROOT / "assets" / "style_packs.json"
DEFAULT_VOICE_CARDS_DIR = CONFIG_DIR / "voice_cards"
CARD_SCHEMA_VERSION = 1
TIMBRE_ENCODER = "gpt-sovits-v2pro-zeroshot"
_STATUS_VALUES = {"active", "retired"}
# voice_id becomes a directory name; separators (/, \, :) must never reach the
# filesystem, including percent-decoded Windows backslashes.
VOICE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class VoiceAssetError(ValueError):
    """User-actionable voice asset failure; ``http_status`` feeds the API."""

    http_status = 400


class VoiceCardNotFoundError(VoiceAssetError):
    http_status = 404


class StylePackNotFoundError(VoiceAssetError):
    http_status = 400


@dataclass(frozen=True)
class StylePack:
    style_id: str
    display_name: str
    description: str
    pace: float
    pitch_range: str
    energy: str
    pause_scale: float
    emotion: str
    synthesis: dict[str, float]

    def public_dict(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "display_name": self.display_name,
            "description": self.description,
            "pace": self.pace,
            "pitch_range": self.pitch_range,
            "energy": self.energy,
            "pause_scale": self.pause_scale,
            "emotion": self.emotion,
            "synthesis": dict(self.synthesis),
        }


def load_style_packs(path: Path | None = None) -> list[StylePack]:
    source = path or STYLE_PACK_FILE
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VoiceAssetError(f"无法读取风格包文件：{source}") from exc
    except ValueError as exc:
        raise VoiceAssetError(f"风格包文件不是有效 JSON：{source}") from exc
    packs: list[StylePack] = []
    seen: set[str] = set()
    for raw in payload.get("packs") or []:
        if not isinstance(raw, dict):
            raise VoiceAssetError("style_packs.json 的 packs 成员必须是对象。")
        style_id = str(raw.get("style_id") or "").strip()
        if not style_id or style_id in seen:
            raise VoiceAssetError("风格包 style_id 不能为空或重复。")
        seen.add(style_id)
        synthesis_raw = raw.get("synthesis") or {}
        synthesis = {
            str(key): float(value)
            for key, value in synthesis_raw.items()
            if key in {"speed", "pause_second"}
        }
        packs.append(
            StylePack(
                style_id=style_id,
                display_name=str(raw.get("display_name") or style_id).strip(),
                description=str(raw.get("description") or "").strip(),
                pace=float(raw.get("pace") or 1.0),
                pitch_range=str(raw.get("pitch_range") or "medium"),
                energy=str(raw.get("energy") or "medium"),
                pause_scale=float(raw.get("pause_scale") or 1.0),
                emotion=str(raw.get("emotion") or "neutral"),
                synthesis=synthesis,
            )
        )
    if not packs:
        raise VoiceAssetError("style_packs.json 至少需要一个风格包。")
    return packs


def resolve_style_pack(style_id: str | None, fallback: str = "natural") -> StylePack:
    packs = load_style_packs()
    by_id = {pack.style_id: pack for pack in packs}
    selected = style_id or fallback
    if selected not in by_id:
        raise StylePackNotFoundError(
            f"风格包不存在：{selected}（可选：{', '.join(by_id)}）"
        )
    return by_id[selected]


def apply_style_pack(
    config: dict[str, Any],
    style: StylePack,
    explicit_fields: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Overlay a style's synthesis knobs; keys the request set explicitly win."""
    updated = dict(config)
    for key, value in style.synthesis.items():
        if key not in explicit_fields:
            updated[key] = value
    updated["style_id"] = style.style_id
    return updated


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_voice_id() -> str:
    return f"vc_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


class VoiceCardStore:
    """Append-only VoiceCard registry rooted at ``root`` (default CONFIG_DIR)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_VOICE_CARDS_DIR

    # ------------------------------------------------------------------ load

    def _card_path(self, voice_id: str) -> Path:
        if not VOICE_ID_PATTERN.fullmatch(voice_id or ""):
            raise VoiceCardNotFoundError(f"声线卡不存在：{voice_id}")
        return self.root / voice_id / "card.json"

    def _load(self, voice_id: str) -> dict[str, Any]:
        path = self._card_path(voice_id)
        if not path.is_file():
            raise VoiceCardNotFoundError(f"声线卡不存在：{voice_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VoiceAssetError(f"声线卡文件不可读：{voice_id}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("versions"), list):
            raise VoiceAssetError(f"声线卡文件结构无效：{voice_id}")
        return payload

    def list_cards(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        cards: list[dict[str, Any]] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                cards.append(self._load(entry.name))
            except VoiceAssetError:
                # A damaged card must not hide the others; the registry is a view.
                cards.append(
                    {
                        "schema_version": CARD_SCHEMA_VERSION,
                        "voice_id": entry.name,
                        "display_name": entry.name,
                        "language": "zh",
                        "status": "unknown",
                        "license_id": None,
                        "consent_id": None,
                        "created_at": "",
                        "updated_at": "",
                        "versions": [],
                        "active_version": "",
                        "corrupt": True,
                    }
                )
        cards.sort(key=lambda card: str(card.get("created_at") or ""))
        return cards

    def get(self, voice_id: str) -> dict[str, Any]:
        return self._load(voice_id)

    # ---------------------------------------------------------------- create

    def create(
        self,
        config: dict[str, Any],
        display_name: str,
        language: str = "zh",
        license_id: str | None = None,
        consent_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        display_name = str(display_name or "").strip()
        if not display_name:
            raise VoiceAssetError("声线卡必须填写 display_name。")
        if language not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise VoiceAssetError(f"声线卡语言不支持：{language}")
        if not str(config.get("ref_audio") or "").strip():
            raise VoiceAssetError(
                "当前配置没有参考音频，请先上传一段自然说话音频再保存声线卡。"
            )
        now = _now_iso()
        first = self._version_from_config(config, notes, now)
        first["version"] = f"{now[:10]}.1"
        card: dict[str, Any] = {
            "schema_version": CARD_SCHEMA_VERSION,
            "voice_id": _new_voice_id(),
            "display_name": display_name,
            "language": language,
            "status": "active",
            "license_id": license_id or None,
            "consent_id": consent_id or None,
            "created_at": now,
            "updated_at": now,
            "versions": [first],
        }
        card["active_version"] = first["version"]
        _atomic_write_json(self._card_path(card["voice_id"]), card)
        return card

    def _version_from_config(
        self, config: dict[str, Any], notes: str | None, now: str
    ) -> dict[str, Any]:
        accent_level = str(config.get("accent_level") or "off")
        style_id = str(config.get("style_id") or "natural")
        return {
            "version": "",
            "created_at": now,
            "timbre": {
                "ref_audio": str(config.get("ref_audio") or ""),
                "ref_text": str(config.get("ref_text") or ""),
                "gpt_model": str(config.get("gpt_model") or ""),
                "sovits_model": str(config.get("sovits_model") or ""),
                "prompt_lang": str(config.get("prompt_lang") or "zh"),
                "encoder": TIMBRE_ENCODER,
            },
            "defaults": {
                "lexicon_id": LEXICON_ZH_BASE,
                "accent_pack_id": build_pronunciation_plan("", accent_level).accent_pack_id,
                "accent_level": accent_level,
                "accent_intensity": accent_intensity_for(accent_level),
                "style_id": style_id,
            },
            "source": "config",
            "notes": notes or "",
        }

    # ---------------------------------------------------------------- update

    def add_version(
        self, voice_id: str, config: dict[str, Any], notes: str | None = None
    ) -> dict[str, Any]:
        card = self._load(voice_id)
        if not str(config.get("ref_audio") or "").strip():
            raise VoiceAssetError(
                "当前配置没有参考音频，请先上传一段自然说话音频再新增版本。"
            )
        base_date = str(card.get("versions", [{}])[0].get("version") or "")[:10]
        date_prefix = base_date or time.strftime("%Y-%m-%d")
        version = f"{date_prefix}.{len(card['versions']) + 1}"
        entry = self._version_from_config(config, notes, _now_iso())
        entry["version"] = version
        card["versions"].append(entry)
        card["active_version"] = version
        card["updated_at"] = _now_iso()
        _atomic_write_json(self._card_path(voice_id), card)
        return card

    def update(
        self,
        voice_id: str,
        display_name: str | None = None,
        status: str | None = None,
        license_id: str | None = None,
        consent_id: str | None = None,
        active_version: str | None = None,
    ) -> dict[str, Any]:
        card = self._load(voice_id)
        if display_name is not None:
            trimmed = str(display_name).strip()
            if not trimmed:
                raise VoiceAssetError("display_name 不能为空。")
            card["display_name"] = trimmed
        if status is not None:
            if status not in _STATUS_VALUES:
                raise VoiceAssetError(f"status 只能是 {'/'.join(sorted(_STATUS_VALUES))}。")
            card["status"] = status
        if license_id is not None:
            card["license_id"] = license_id or None
        if consent_id is not None:
            card["consent_id"] = consent_id or None
        if active_version is not None:
            known = {version["version"] for version in card["versions"]}
            if active_version not in known:
                raise VoiceAssetError(
                    f"版本不存在：{active_version}（可选：{', '.join(sorted(known))}）"
                )
            card["active_version"] = active_version
        card["updated_at"] = _now_iso()
        _atomic_write_json(self._card_path(voice_id), card)
        return card

    # ------------------------------------------------------------- applying

    def active_version(self, voice_id: str) -> dict[str, Any]:
        card = self.get(voice_id)
        return self._active_version_of(card)

    def _active_version_of(self, card: dict[str, Any]) -> dict[str, Any]:
        active = str(card.get("active_version") or "")
        for version in card.get("versions") or []:
            if version.get("version") == active:
                return version
        versions = card.get("versions") or []
        if not versions:
            raise VoiceAssetError(f"声线卡没有任何版本：{card.get('voice_id')}")
        return versions[-1]


def card_public_dict(card: dict[str, Any]) -> dict[str, Any]:
    """Public view: card metadata plus the active version's timbre/defaults."""
    versions = card.get("versions") or []
    active = str(card.get("active_version") or "")
    active_entry = next(
        (v for v in versions if v.get("version") == active), versions[-1] if versions else None
    )
    ref_audio = str((active_entry or {}).get("timbre", {}).get("ref_audio") or "")
    return {
        "schema_version": card.get("schema_version", CARD_SCHEMA_VERSION),
        "voice_id": card.get("voice_id"),
        "display_name": card.get("display_name"),
        "language": card.get("language", "zh"),
        "status": card.get("status", "active"),
        "license_id": card.get("license_id"),
        "consent_id": card.get("consent_id"),
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
        "active_version": active,
        "version_count": len(versions),
        "timbre_available": bool(ref_audio) and Path(ref_audio).is_file(),
        "active_timbre": (active_entry or {}).get("timbre"),
        "active_defaults": (active_entry or {}).get("defaults"),
        "versions": [
            {
                "version": v.get("version"),
                "created_at": v.get("created_at"),
                "defaults": v.get("defaults"),
                "notes": v.get("notes", ""),
            }
            for v in versions
        ],
    }


def apply_voice_card(
    config: dict[str, Any],
    card: dict[str, Any],
    explicit_fields: frozenset[str] | set[str] = frozenset(),
    style_id: str | None = None,
) -> dict[str, Any]:
    """Overlay a card's active version onto a synthesis config.

    Precedence: explicit request fields > card defaults > current config.
    A retired card refuses new synthesis (revocation semantics).
    """
    if str(card.get("status") or "active") != "active":
        raise VoiceAssetError(
            f"声线卡已停用：{card.get('display_name')}，不能用于新的合成。"
        )
    versions = card.get("versions") or []
    active = str(card.get("active_version") or "")
    entry = next((v for v in versions if v.get("version") == active), None)
    if entry is None:
        entry = versions[-1] if versions else None
    if entry is None:
        raise VoiceAssetError(f"声线卡没有任何版本：{card.get('voice_id')}")

    updated = dict(config)
    timbre = entry.get("timbre") or {}
    for key in ("gpt_model", "sovits_model", "ref_audio", "ref_text", "prompt_lang"):
        value = str(timbre.get(key) or "").strip()
        if value:
            updated[key] = value

    defaults = entry.get("defaults") or {}
    if "accent_level" not in explicit_fields and defaults.get("accent_level"):
        updated["accent_level"] = defaults["accent_level"]

    chosen_style = style_id or defaults.get("style_id")
    if chosen_style:
        pack = resolve_style_pack(str(chosen_style))
        updated = apply_style_pack(updated, pack, explicit_fields)

    updated["voice_card_id"] = card.get("voice_id")
    updated["voice_card_version"] = entry.get("version")
    return updated
