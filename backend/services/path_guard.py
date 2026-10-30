"""Confinement for caller-supplied filesystem paths.

Several endpoints accept a path that is supposed to point at one of *this
project's* assets: grading a reference clip, converting a synthesis result,
picking a GPT-SoVITS environment. Before this module existed those paths were
resolved verbatim, which turned the API into a filesystem oracle (and, via the
timbre pipeline, into an exfiltration channel for any audio on disk).

The rule is one allowlist: by default only the repository root counts, plus
whatever extra roots the operator declares in ``XIANGHUA_ALLOWED_PATH_ROOTS``
(os pathsep- or comma-separated). Everything the product legitimately touches —
uploads, outputs, the engine tree, custom_data — lives under the repo root.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.services.core import REPO_ROOT


class PathConfinementError(ValueError):
    """A caller-supplied path points outside the allowed roots."""


def allowed_roots() -> list[Path]:
    roots = [REPO_ROOT.resolve()]
    extra = os.environ.get("XIANGHUA_ALLOWED_PATH_ROOTS", "")
    for item in extra.replace(";", os.pathsep).split(os.pathsep):
        item = item.strip().strip('"')
        if not item:
            continue
        try:
            roots.append(Path(item).expanduser().resolve())
        except OSError:
            continue
    return roots


def _confined(value: str | Path, *, must_exist: bool) -> Path:
    candidate = Path(str(value)).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        # 解不开的路径（例如不可达的 UNC 主机）直接按越界处理。
        raise PathConfinementError(
            f"路径无法解析：{candidate}。"
        ) from exc
    for root in allowed_roots():
        if resolved == root or resolved.is_relative_to(root):
            if not must_exist or resolved.exists():
                return resolved
            raise PathConfinementError(
                f"路径不存在：{candidate}（允许范围：仓库目录）。"
            )
    raise PathConfinementError(
        f"路径超出允许范围（仓库目录）：{candidate}。如需访问其他目录，"
        "请把该目录加入环境变量 XIANGHUA_ALLOWED_PATH_ROOTS。"
    )


def confined_file(value: str | Path) -> Path:
    """Resolve a caller-supplied file path that must stay inside allowed roots."""
    return _confined(value, must_exist=True)


def confined_directory(value: str | Path) -> Path:
    """Resolve a caller-supplied directory path inside allowed roots."""
    resolved = _confined(value, must_exist=True)
    if not resolved.is_dir():
        raise PathConfinementError(f"不是目录：{value}")
    return resolved
