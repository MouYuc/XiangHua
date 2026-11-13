"""Path validation for the model/reference fields in the configuration.

``path_guard`` confines every caller-supplied path that reaches a filesystem
read. The configuration fields were the hole in that promise: ``PUT /config``
(and, before the request model was tightened, a ``POST /synthesis`` body) could
set ``ref_audio`` to any file on disk — turning the synthesiser into a
filesystem oracle that reads arbitrary audio back out — and could set
``gpt_model``/``gpt_sovits_root``, which are handed to ``torch.load`` and
``sys.path``/``import`` respectively.

Three deliberate escapes keep legitimate setups working:

* a value that is **unchanged** from the stored configuration is not re-checked,
  so the UI's autosave (which round-trips the whole config object) never fails
  on a root that was legitimately adopted at startup;
* an engine root **outside** the repository is accepted when it was found by the
  environment scanner, because that is exactly how a user installs GPT-SoVITS on
  another drive;
* a model / reference file is accepted when it lives **inside** the configured
  engine root (which is itself validated above), because that is where trained
  weights and sliced training audio end up — including for an engine tree on
  another drive, and for ``default_config()``'s own reference path.

Everything else has to live inside ``path_guard.allowed_roots()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services import path_guard

#: Config keys that point at a file the engine opens directly.
FILE_FIELDS = ("ref_audio", "gpt_model", "sovits_model")
#: Config keys that point at a directory that gets imported into ``sys.path``.
DIRECTORY_FIELDS = ("gpt_sovits_root",)

_ENGINE_MARKER = Path("GPT_SoVITS") / "TTS_infer_pack" / "TTS.py"


class ConfigPathError(ValueError):
    """A configuration path is outside what the operator allows."""


def _allowed_roots() -> list[Path]:
    return path_guard.allowed_roots()


def _inside(value: Path, roots: list[Path]) -> bool:
    for root in roots:
        if value == root or value.is_relative_to(root):
            return True
    return False


def _scanned_roots() -> set[str]:
    """Engine roots the environment scanner can discover on this machine."""
    from backend.infra.gptsovits_env import scan_gpt_sovits_roots

    try:
        return {str(Path(item).resolve()) for item in scan_gpt_sovits_roots(path_guard.REPO_ROOT)}
    except Exception:  # pragma: no cover - a broken scanner must not block saves
        return set()


def _under_root(value: Path, root: str) -> bool:
    """True when ``value`` lives inside ``root``; a blank/unresolvable root is False."""
    if not root:
        return False
    try:
        base = Path(root).expanduser().resolve()
    except OSError:  # pragma: no cover - unreachable on a normal filesystem
        return False
    return value == base or value.is_relative_to(base)


def _check_file(field: str, value: str, *, engine_root: str = "") -> None:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigPathError(f"{field} 指向的文件不存在：{candidate}") from exc
    if not resolved.is_file():
        raise ConfigPathError(f"{field} 不是文件：{candidate}")
    if _inside(resolved, _allowed_roots()):
        return
    # A model file is allowed anywhere under the configured engine root: that root
    # is itself validated by ``_check_directory``, and weights trained into a tree
    # on another drive legitimately live there — rejecting them made
    # "填写最新模型" succeed and then every later synthesis fail.
    if _under_root(resolved, engine_root):
        return
    raise ConfigPathError(
        f"{field} 超出允许范围：{candidate}。请把该文件所在目录加入 "
        "XIANGHUA_ALLOWED_PATH_ROOTS，或把模型放进 GPT-SoVITS 目录内。"
    )


def _check_directory(field: str, value: str) -> None:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigPathError(f"{field} 指向的目录不存在：{candidate}") from exc
    if not resolved.is_dir():
        raise ConfigPathError(f"{field} 不是目录：{candidate}")
    if not (resolved / _ENGINE_MARKER).is_file():
        raise ConfigPathError(
            f"{field} 不是可用的 GPT-SoVITS 目录（缺少 GPT_SoVITS/TTS_infer_pack/TTS.py）：{candidate}"
        )
    if _inside(resolved, _allowed_roots()) or str(resolved) in _scanned_roots():
        return
    raise ConfigPathError(
        f"{field} 超出允许范围：{candidate}。请先在设置页用「检测环境」选中该目录，"
        "或把它加入 XIANGHUA_ALLOWED_PATH_ROOTS。"
    )


def validate_config_update(payload: dict[str, Any], current: dict[str, Any]) -> None:
    """Raise :class:`ConfigPathError` if an explicitly set path escapes the allowlist.

    Only fields present in ``payload`` are considered, and a field whose value is
    identical to ``current`` is skipped: re-saving an already-adopted path is not
    a new permission.
    """
    # Directories first: when one payload switches the engine root *and* points a
    # model at it, the model must be judged against the root this payload adopts —
    # which has just been validated here — not against the old one.
    for field in DIRECTORY_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if value is None or not str(value).strip():
            continue
        if current.get(field) == value:
            continue
        _check_directory(field, str(value))

    engine_root = str(payload.get("gpt_sovits_root") or current.get("gpt_sovits_root") or "")
    for field in FILE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if value is None or not str(value).strip():
            continue
        if current.get(field) == value:
            continue
        _check_file(field, str(value), engine_root=engine_root)
