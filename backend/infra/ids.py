"""Collision-free identifiers for background jobs.

Four job managers used ``f"{prefix}_{int(time.time() * 1000)}"``. Two jobs
started in the same millisecond therefore received *the same id*, and because
each manager keeps ``self._jobs[job_id] = job``, the second job silently replaced
the first: both workers then wrote progress into whichever snapshot the dict
held, one job vanished from ``/api/jobs``, and "导出整集" could export the other
run's lines. Nothing raised.

The fix is a monotonically increasing counter combined with the clock, so ids
stay human-readable (``synth_20261101_162842_0007``) while being unique within a
process. ``uuid4`` is kept as a fallback suffix for the rare case of a process
that resets its counter (fork) while sharing durable job storage.
"""

from __future__ import annotations

import threading
import time

_counter = 0
_counter_lock = threading.Lock()


def new_job_id(prefix: str) -> str:
    """A process-unique job id, ordered by creation time.

    Format: ``<prefix>_<YYYYmmdd_HHMMSS>_<seq>``. The sequence is zero-padded so
    lexical sort matches creation order (``/api/jobs`` sorts on it).
    """
    global _counter
    with _counter_lock:
        _counter += 1
        sequence = _counter
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{sequence:06d}"
