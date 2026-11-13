"""C2PA-style provenance sidecars for synthesised audio.

Every output file gets a sibling ``<stem>.provenance.json`` manifest that
records what was generated, from which voice card, under which license and
consent, plus an HMAC-SHA256 signature made with the server key. The sidecar
is deliberately best-effort: a provenance failure must never break or slow
the synthesis path (``write_provenance`` swallows card-lookup errors and
leaves ``license_id``/``consent_id`` as ``None`` when the card is missing).

The server key lives at ``CONFIG_DIR/provenance.key`` as 64 hex characters
(32 bytes). It is created on first use; tests may patch
``PROVENANCE_KEY_FILE`` or pass ``key_path`` to keep the real config dir
untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.infra.paths import CONFIG_DIR

PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_KEY_FILE = CONFIG_DIR / "provenance.key"
SIGNATURE_ALGORITHM = "hmac-sha256"
_SIGNATURE_FIELDS = ("signature", "signature_algorithm")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_or_create_server_key(path: Path | None = None) -> bytes:
    """Return the 32-byte HMAC key, creating the hex key file on first use.

    An existing file that is unreadable, not valid hex, or not exactly
    32 bytes is treated as absent and regenerated — a corrupt key file can
    not verify anything, so a fresh key is the only sane recovery.
    """
    key_path = Path(path) if path is not None else PROVENANCE_KEY_FILE
    try:
        raw = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
        if len(raw) == 32:
            return raw
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = key_path.with_name(f"{key_path.name}.tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(key.hex(), encoding="utf-8")
    os.replace(tmp, key_path)
    return key


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    audio_path: Path,
    text: str,
    cfg: dict[str, Any],
    license_id: str | None = None,
    consent_id: str | None = None,
) -> dict[str, Any]:
    """Describe one synthesised audio file for the signed sidecar."""
    audio_path = Path(audio_path)
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "tool": "xianghua",
        "generated_at": _now_iso(),
        "file_name": audio_path.name,
        "file_bytes": audio_path.stat().st_size,
        "file_sha256": _file_sha256(audio_path),
        "text_sha256": hashlib.sha256(str(text).encode("utf-8")).hexdigest(),
        "voice_card_id": cfg.get("voice_card_id"),
        "voice_card_version": cfg.get("voice_card_version"),
        "accent_level": cfg.get("accent_level"),
        "text_lang": cfg.get("text_lang"),
        "license_id": license_id,
        "consent_id": consent_id,
    }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def sign_manifest(manifest: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Return a signed copy of ``manifest``; the input dict is not mutated."""
    signed = dict(manifest)
    signature = hmac.new(
        key, _canonical_json(signed).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    signed["signature"] = signature
    signed["signature_algorithm"] = SIGNATURE_ALGORITHM
    return signed


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    # Same pattern as voice_assets._atomic_write_json: unique tmp name next to
    # the target plus a short backoff so Windows antivirus/indexing scans do
    # not turn os.replace into a sporadic WinError 5.
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


def _card_license_and_consent(voice_card_id: str) -> tuple[str | None, str | None]:
    """Look up license/consent on the voice card; any failure means (None, None).

    The import is deliberately deferred: core synthesis code must be able to
    call ``write_provenance`` without paying for the voice-asset import, and a
    missing or damaged card must never fail provenance writing.
    """
    try:
        from backend.services.voice_assets import VoiceCardStore

        card = VoiceCardStore().get(voice_card_id)
        return card.get("license_id"), card.get("consent_id")
    except Exception:
        return None, None


def write_provenance(
    audio_path: Path,
    text: str,
    cfg: dict[str, Any],
    license_id: str | None = None,
    consent_id: str | None = None,
    key_path: Path | None = None,
) -> Path:
    """Build, sign, and atomically write the sidecar next to ``audio_path``."""
    audio_path = Path(audio_path)
    voice_card_id = cfg.get("voice_card_id")
    if (license_id is None or consent_id is None) and voice_card_id:
        card_license, card_consent = _card_license_and_consent(str(voice_card_id))
        if license_id is None:
            license_id = card_license
        if consent_id is None:
            consent_id = card_consent
    manifest = build_manifest(
        audio_path, text, cfg, license_id=license_id, consent_id=consent_id
    )
    signed = sign_manifest(manifest, get_or_create_server_key(key_path))
    sidecar = audio_path.with_name(f"{audio_path.stem}.provenance.json")
    _atomic_write_json(sidecar, signed)
    return sidecar


def verify_provenance(audio_path: Path, sidecar_path: Path | None = None) -> dict[str, Any]:
    """Verify a sidecar against the current server key and the audio file.

    Checks run in order — sidecar exists, JSON parses, signature matches the
    content, file SHA-256 matches the audio — and the first failure wins, with
    a concrete Chinese reason.
    """
    audio_path = Path(audio_path)
    sidecar = (
        Path(sidecar_path)
        if sidecar_path is not None
        else audio_path.with_name(f"{audio_path.stem}.provenance.json")
    )
    if not sidecar.is_file():
        return {"valid": False, "reason": f"溯源凭据不存在：{sidecar.name}", "manifest": None}
    try:
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"valid": False, "reason": "溯源凭据不是有效的 JSON。", "manifest": None}
    if not isinstance(manifest, dict):
        return {"valid": False, "reason": "溯源凭据结构无效。", "manifest": None}
    signature = manifest.get("signature")
    if not signature:
        return {"valid": False, "reason": "溯源凭据缺少签名。", "manifest": manifest}
    unsigned = {key: value for key, value in manifest.items() if key not in _SIGNATURE_FIELDS}
    expected = hmac.new(
        get_or_create_server_key(),
        _canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(str(signature), expected):
        return {
            "valid": False,
            "reason": "签名校验失败：凭据内容与服务端签名不一致（内容可能被修改）。",
            "manifest": manifest,
        }
    if not audio_path.is_file():
        return {
            "valid": False,
            "reason": f"音频文件不存在：{audio_path.name}",
            "manifest": manifest,
        }
    if _file_sha256(audio_path) != manifest.get("file_sha256"):
        return {
            "valid": False,
            "reason": "文件校验失败：音频文件的 SHA-256 与凭据记录不一致，文件可能已被修改。",
            "manifest": manifest,
        }
    return {"valid": True, "reason": "校验通过。", "manifest": manifest}
