"""Job registry adapter for training jobs.

Training state is split across ``backend.services.training`` (running jobs,
``STEPS`` order, ``stop_job``) and ``.guangpu_webui/train_state.json`` (read
via ``load_state()``). A running job produces its own record; the most recent
non-pending step in the persisted state is listed under a synthetic
``trainstate_*`` id unless a running job already covers that step. The training
module is imported lazily so ``patch.object(train_backend, ...)`` in tests is
always seen.
"""

from __future__ import annotations

from typing import Any

from backend.services.job_registry_common import (
    KIND_TRAINING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    _first_progress,
    _record,
    _text,
    normalize_status,
)


def collect_training(registry: Any) -> list[dict[str, Any]]:
    from backend.services import training as train_backend

    state = train_backend.load_state() or {}
    steps = state.get("steps") if isinstance(state, dict) else {}
    if not isinstance(steps, dict):
        steps = {}
    order = {step: index for index, step in enumerate(getattr(train_backend, "STEPS", []) or [])}

    records: list[dict[str, Any]] = []
    covered_steps: set[str] = set()
    for entry in train_backend.running_jobs() or []:
        if not isinstance(entry, dict):
            continue
        job_id = _text(entry.get("job_id"))
        step = _text(entry.get("step"))
        if not job_id:
            continue
        covered_steps.add(step)
        step_state = steps.get(step) if isinstance(steps.get(step), dict) else {}
        records.append(_training_record(job_id, step, step_state, running_job=entry))

    latest = _latest_training_step(steps, order)
    if latest is not None:
        step, step_state = latest
        # A running job already produced a record above; do not list the
        # same task twice under a synthetic id.
        if step not in covered_steps:
            records.append(
                _training_record(f"trainstate_{step}", step, step_state, running_job=None)
            )
    return records


def _latest_training_step(
    steps: dict[str, Any], order: dict[str, int]
) -> tuple[str, dict[str, Any]] | None:
    latest: tuple[tuple[int, str, int], str, dict[str, Any]] | None = None
    for step, step_state in steps.items():
        if not isinstance(step_state, dict):
            continue
        status = normalize_status(step_state.get("status"), kind=KIND_TRAINING)
        if status == STATUS_UNKNOWN:
            # Pending/never-started steps are history, not a recent task.
            continue
        timestamp = _text(step_state.get("started")) or _text(step_state.get("ended"))
        key = (
            2 if status == STATUS_RUNNING else 1,
            timestamp,
            order.get(step, -1),
        )
        if latest is None or key > latest[0]:
            latest = (key, step, step_state)
    if latest is None:
        return None
    return latest[1], latest[2]


def _training_record(
    job_id: str,
    step: str,
    step_state: dict[str, Any],
    *,
    running_job: dict[str, Any] | None,
) -> dict[str, Any]:
    progress_meta = step_state.get("progress")
    if not isinstance(progress_meta, dict):
        progress_meta = {}
    status = normalize_status(step_state.get("status"), kind=KIND_TRAINING)
    if running_job is not None:
        status = STATUS_RUNNING
    progress = _first_progress(progress_meta.get("overall_percent"), percent=True)
    if progress is None:
        progress = _first_progress(progress_meta.get("global_percent"), percent=True)
    if progress is None:
        progress = _first_progress(progress_meta.get("stage_percent"), percent=True)
    if status == STATUS_DONE:
        progress = 1.0

    stage = _text(progress_meta.get("stage"))
    step_title = _text(step_state.get("title")) or _text(
        (running_job or {}).get("title")
    ) or step
    if step == "train" and stage in {"s1", "s2"}:
        title = f"训练 {stage}"
    else:
        title = step_title or f"训练 {step}"
    started_at = _text(step_state.get("started"))
    updated_at = _text(step_state.get("ended")) or started_at
    error = ""
    if status == STATUS_FAILED:
        error = _text(step_state.get("note")) or "训练任务失败，请查看训练日志。"
    return _record(
        job_id=job_id,
        kind=KIND_TRAINING,
        title=title,
        status=status,
        progress=progress,
        stage=stage or step_title,
        error=error,
        started_at=started_at,
        updated_at=updated_at,
        cancelable=running_job is not None and status == STATUS_RUNNING,
        source=(
            "backend.services.training.running_jobs()"
            if running_job is not None
            else "backend.services.training.load_state()"
        ),
    )
