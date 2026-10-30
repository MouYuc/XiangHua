"""Multi-tenant API keys with daily quotas and per-minute rate limits.

The registry is one JSON file (``{"keys": [...], "usage": {...}}``) so a
single-process deployment needs no database. Keys are stored as SHA-256
hashes; the plain token is returned exactly once by ``create_key``. Daily
request counters and accumulated character counts persist to disk; the
per-minute sliding window is process memory (a restart clears the burst
limit, which is the acceptable trade-off for a file-backed registry).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

_TOKEN_PREFIX = "xh_"
_TOKEN_HEX_LENGTH = 24
_KEY_ID_HEX_LENGTH = 8
_RPM_WINDOW_SECONDS = 60.0


class ApiKeyError(ValueError):
    """User-facing API key failure with a Chinese message."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class ApiKeyRegistry:
    """File-backed API key registry with usage counters and rate limiting."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        # key_id -> deque[monotonic timestamps] of accepted requests
        self._rpm_windows: dict[str, deque[float]] = {}

    # ---------------------------------------------------------------- storage

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"keys": [], "usage": {}}
        if not isinstance(data, dict):
            return {"keys": [], "usage": {}}
        if not isinstance(data.get("keys"), list):
            data["keys"] = []
        if not isinstance(data.get("usage"), dict):
            data["usage"] = {}
        return data

    def _save_atomic(self, data: dict[str, Any]) -> None:
        # Unique tmp name + os.replace + short PermissionError backoff, the
        # same Windows-safe pattern used across the config writers.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    # ----------------------------------------------------------------- keys

    def create_key(
        self,
        tenant: str,
        label: str = "",
        daily_request_quota: int = 2000,
        rpm_limit: int = 30,
    ) -> dict[str, Any]:
        """Create a key; the plain token is returned only this one time."""
        tenant = str(tenant or "").strip()
        if not tenant:
            raise ApiKeyError("必须提供租户名（tenant）。")
        try:
            daily_request_quota = int(daily_request_quota)
            rpm_limit = int(rpm_limit)
        except (TypeError, ValueError) as exc:
            raise ApiKeyError("日配额与每分钟限速必须是整数。") from exc
        if daily_request_quota < 0 or rpm_limit < 0:
            raise ApiKeyError("日配额与每分钟限速不能为负数。")
        token = _TOKEN_PREFIX + secrets.token_hex(_TOKEN_HEX_LENGTH)
        record: dict[str, Any] = {
            "key_id": f"k_{secrets.token_hex(_KEY_ID_HEX_LENGTH)}",
            "key_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "tenant": tenant,
            "label": str(label or ""),
            "daily_request_quota": daily_request_quota,
            "rpm_limit": rpm_limit,
            "created_at": _now_iso(),
        }
        with self._lock:
            data = self._load()
            data["keys"].append(record)
            self._save_atomic(data)
        public = dict(record)
        public.pop("key_hash")
        public["token"] = token
        return public

    def authenticate(self, token: str) -> dict[str, Any] | None:
        """Return the stored record for ``token`` (without the hash) or None."""
        if not token:
            return None
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self._lock:
            data = self._load()
        for record in data["keys"]:
            if not isinstance(record, dict):
                continue
            if record.get("key_hash") == token_hash:
                public = dict(record)
                public.pop("key_hash", None)
                return public
        return None

    # ------------------------------------------------------------- metering

    def check_and_count(self, key_id: str) -> tuple[bool, str]:
        """Rate-limit and quota-check one request, counting it when accepted."""
        with self._lock:
            data = self._load()
            record = next(
                (
                    item
                    for item in data["keys"]
                    if isinstance(item, dict) and item.get("key_id") == key_id
                ),
                None,
            )
            if record is None:
                return False, "API Key 不存在。"
            today = _today()
            stored_usage = data["usage"].get(key_id)
            if not isinstance(stored_usage, dict) or stored_usage.get("date") != today:
                stored_usage = {"date": today, "requests": 0, "chars": 0}

            now = time.monotonic()
            window = self._rpm_windows.setdefault(key_id, deque())
            while window and now - window[0] >= _RPM_WINDOW_SECONDS:
                window.popleft()
            rpm_limit = int(record.get("rpm_limit") or 0)
            if rpm_limit > 0 and len(window) >= rpm_limit:
                return False, f"每分钟请求数已达上限 {rpm_limit}。"
            daily_request_quota = int(record.get("daily_request_quota") or 0)
            if (
                daily_request_quota > 0
                and int(stored_usage.get("requests") or 0) >= daily_request_quota
            ):
                return False, f"今日请求数已达配额 {daily_request_quota}，明天重置。"

            window.append(now)
            stored_usage["requests"] = int(stored_usage.get("requests") or 0) + 1
            data["usage"][key_id] = stored_usage
            self._save_atomic(data)
            return True, ""

    def record_chars(self, key_id: str, count: int) -> None:
        """Accumulate synthesised character usage for one key."""
        with self._lock:
            data = self._load()
            if not any(
                isinstance(item, dict) and item.get("key_id") == key_id
                for item in data["keys"]
            ):
                return
            today = _today()
            stored_usage = data["usage"].get(key_id)
            if not isinstance(stored_usage, dict) or stored_usage.get("date") != today:
                stored_usage = {"date": today, "requests": 0, "chars": 0}
            stored_usage["chars"] = int(stored_usage.get("chars") or 0) + max(0, int(count))
            data["usage"][key_id] = stored_usage
            self._save_atomic(data)

    def list_keys(self) -> list[dict[str, Any]]:
        """Public key rows (no hashes) with today's usage attached."""
        with self._lock:
            data = self._load()
        rows: list[dict[str, Any]] = []
        for record in data["keys"]:
            if not isinstance(record, dict):
                continue
            public = dict(record)
            public.pop("key_hash", None)
            public["usage"] = self.usage(str(public.get("key_id") or ""))
            rows.append(public)
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows

    def revoke_key(self, key_id: str) -> bool:
        """Remove one key and its usage counters; True when it existed."""
        key_id = str(key_id or "").strip()
        if not key_id:
            return False
        with self._lock:
            data = self._load()
            remaining = [
                item
                for item in data["keys"]
                if not (isinstance(item, dict) and item.get("key_id") == key_id)
            ]
            if len(remaining) == len(data["keys"]):
                return False
            data["keys"] = remaining
            data["usage"].pop(key_id, None)
            self._rpm_windows.pop(key_id, None)
            self._save_atomic(data)
            return True

    def usage(self, key_id: str) -> dict[str, Any]:
        """Return today's counters for ``key_id`` (zeros for unknown keys)."""
        with self._lock:
            data = self._load()
        stored_usage = data["usage"].get(key_id)
        if isinstance(stored_usage, dict):
            return dict(stored_usage)
        return {"date": _today(), "requests": 0, "chars": 0}
