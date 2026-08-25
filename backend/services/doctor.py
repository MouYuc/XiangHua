"""Automated environment scan and safe repair helpers for the WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services import core
from tools.env import derive_environment_config, scan_gpt_sovits_roots
from tools.train import apply_patch, missing_deps, patch_status, start_deps_job


def _root_is_valid(root: str) -> bool:
    path = Path(root).expanduser()
    return path.is_dir() and (path / "GPT_SoVITS").is_dir()


def run_doctor(repair: bool = False) -> dict[str, Any]:
    cfg = core.load_config()
    actions: list[dict[str, Any]] = []
    candidates = scan_gpt_sovits_roots(core.REPO_ROOT)
    selected = str(cfg.get("gpt_sovits_root") or "")
    root_valid = _root_is_valid(selected)

    if repair and (not root_valid or not str(cfg.get("gpt_model") or "").strip() or not str(cfg.get("sovits_model") or "").strip() or not str(cfg.get("ref_audio") or "").strip()):
        if candidates:
            selected = candidates[0]["path"]
            cfg.update(derive_environment_config(selected, cfg))
            cfg = core.save_config(cfg)
            root_valid = True
            actions.append({"kind": "config", "status": "fixed", "message": f"已自动选择环境：{selected}"})
        else:
            actions.append({"kind": "config", "status": "warn", "message": "未找到可自动选择的 GPT-SoVITS 环境。"})

    rows = core.environment_check(cfg)

    if repair and root_valid and not patch_status(selected):
        ok, message = apply_patch(selected)
        actions.append({"kind": "patch", "status": "fixed" if ok else "fail", "message": message})
        rows = core.environment_check(cfg)

    deps = missing_deps()
    if deps:
        if repair:
            job_id = start_deps_job(deps)
            actions.append({"kind": "deps", "status": "running", "job_id": job_id, "message": "已启动训练依赖安装。"})
        else:
            actions.append({"kind": "deps", "status": "warn", "message": "训练依赖缺失： " + ", ".join(deps)})

    summary = "环境已正常" if all(row["status"] == "ok" for row in rows) else "存在需要处理的环境项"
    if actions and repair:
        summary = "已执行自动修复，正在等待部分后台任务完成" if any(action.get("status") == "running" for action in actions) else "已执行自动修复"

    return {
        "repair": repair,
        "summary": summary,
        "repaired": any(action.get("status") == "fixed" for action in actions) or any(action.get("status") == "running" for action in actions),
        "selected": selected,
        "rows": rows,
        "actions": actions,
        "config": cfg,
    }
