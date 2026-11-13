"""Shim: the GPU lease moved to :mod:`backend.infra.gpu` (infra layer).

Every public name is re-exported unchanged — ``GPU_ARBITER`` is the *same*
singleton object, so leases, status snapshots and ``reset_for_tests()`` keep
behaving identically regardless of which import path a caller used.
"""

from backend.infra.gpu import (  # noqa: F401 - shim re-exports
    DEFAULT_TIMEOUT_SECONDS,
    GPU_ARBITER,
    HISTORY_LIMIT,
    GpuArbiter,
    GpuBusyTimeout,
    Lease,
)
