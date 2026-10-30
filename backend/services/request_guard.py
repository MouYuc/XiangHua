"""ASGI guards in front of the API surface: host/origin, /v1 API key, and a
bounded synthesis queue.

Why a middleware and not per-route dependencies: the policies below are
cross-cutting (every ``/api`` and ``/v1`` route), and the synthesis queue must
reject *before* the request body is parsed or a job is created. The middleware
is a plain ASGI callable — no ``BaseHTTPMiddleware`` — so streaming responses
(SSE) pass through untouched.

Policy summary (all knobs are environment variables):

- ``XIANGHUA_ALLOWED_HOSTS``  comma-separated hostnames the ``Host`` header may
  carry. Default ``localhost,127.0.0.1,[::1],testserver``. ``testserver`` keeps
  Starlette's TestClient working; DNS-rebinding requests arrive with an
  attacker hostname and get a 403.
- ``XIANGHUA_ALLOW_REMOTE_HOSTS=1`` disables the host check (LAN deployment).
- ``XIANGHUA_API_KEY``  when set, every ``/v1`` request must carry it as
  ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. ``/api`` stays
  keyless for the bundled WebUI but is still origin-checked.
- ``XIANGHUA_API_KEYS_FILE``  multi-tenant mode: a JSON registry managed via
  ``POST /api/api-keys``. Each key carries a tenant, a daily request quota and
  a per-minute rate limit; quota hits answer 429 with the registry's reason.
- ``XIANGHUA_SYNTHESIS_QUEUE_MAX``  max queued+running synthesis jobs (default
  8) before new ``POST /synthesis`` requests get 429.
- ``XIANGHUA_SYNTHESIS_MAX_BODY_BYTES``  request-body cap for ``POST
  /synthesis`` (default 1 MiB of JSON — a script with book-length text is
  never legitimate).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,[::1],testserver"
DEFAULT_SYNTHESIS_QUEUE_MAX = 8
DEFAULT_SYNTHESIS_MAX_BODY_BYTES = 1024 * 1024


def _env_int(name: str, default: int, floor: int = 1) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        return default


def _json_payload(status: int, detail: str, code: str) -> bytes:
    return json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode("utf-8")


def api_key_registry() -> Any:
    """The configured multi-tenant registry (or None) — shared with app routes."""
    return RequestGuardMiddleware._registry()


class RequestGuardMiddleware:
    """Reject non-local hosts, foreign origins, unauthenticated /v1 calls and
    synthesis floods before the application sees them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _allowed_hosts() -> set[str]:
        raw = os.environ.get("XIANGHUA_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    @staticmethod
    def _host_allowed(host_header: str | None) -> bool:
        if os.environ.get("XIANGHUA_ALLOW_REMOTE_HOSTS") == "1":
            return True
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):  # IPv6 literal like [::1]:7860
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]  # strip port
        return host in RequestGuardMiddleware._allowed_hosts()

    @staticmethod
    def _origin_allowed(origin_header: str | None) -> bool:
        """Foreign web pages must not drive the local API (CSRF).

        Any port on a loopback hostname is fine: the dev Vite server (:5173)
        and the bundled UI (:7860) both qualify. Absent Origin (curl, the
        WebUI's own same-origin fetches on GET) is fine too.
        """
        if not origin_header:
            return True
        origin = origin_header.strip().lower()
        if not origin.startswith(("http://", "https://")):
            return False
        rest = origin.split("://", 1)[1]
        host = rest.split("/")[0]
        if host.startswith("["):
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]
        return host in {"localhost", "127.0.0.1", "[::1]"} or host in RequestGuardMiddleware._allowed_hosts()

    @staticmethod
    def _extract_token(headers: list[tuple[bytes, bytes]]) -> str:
        for name, value in headers:
            decoded = name.decode("latin-1").lower()
            if decoded == "x-api-key":
                return value.decode("latin-1").strip()
            if decoded == "authorization":
                token = value.decode("latin-1").strip()
                if token.lower().startswith("bearer "):
                    return token[7:].strip()
        return ""

    _REGISTRY_CACHE: dict[str, Any] = {}

    @classmethod
    def _registry(cls) -> Any:
        """The multi-tenant key registry, or None when not configured.

        Cached per configured path so env changes between tests are honoured.
        """
        path = os.environ.get("XIANGHUA_API_KEYS_FILE", "").strip()
        if not path:
            return None
        cached = cls._REGISTRY_CACHE.get(path)
        if cached is None:
            from backend.services.api_keys import ApiKeyRegistry

            cached = ApiKeyRegistry(Path(path))
            cls._REGISTRY_CACHE[path] = cached
        return cached

    @classmethod
    def _api_key_verdict(cls, headers: list[tuple[bytes, bytes]]) -> tuple[int, str, str] | None:
        """None when the request may pass; otherwise (status, detail, code)."""
        registry = cls._registry()
        single_key = os.environ.get("XIANGHUA_API_KEY", "")
        if registry is None and not single_key:
            return None
        token = cls._extract_token(headers)
        if not token:
            return 401, "缺少或错误的 API Key，请携带 Authorization: Bearer <key>。", "UNAUTHORIZED"
        if registry is not None:
            record = registry.authenticate(token)
            if record is None:
                return 401, "API Key 无效。", "UNAUTHORIZED"
            ok, reason = registry.check_and_count(str(record.get("key_id") or ""))
            if not ok:
                return 429, reason, "RATE_LIMITED"
            return None
        if token != single_key:
            return 401, "缺少或错误的 API Key，请携带 Authorization: Bearer <key>。", "UNAUTHORIZED"
        return None

    @staticmethod
    def _synthesis_flood_blocked(scope: dict[str, Any], headers: list[tuple[bytes, bytes]]) -> tuple[int, str, str] | None:
        """Queue-cap and body-cap gate for POST /synthesis (both prefixes)."""
        if scope.get("method") != "POST":
            return None
        path = scope.get("path", "")
        if not (path == "/api/synthesis" or path == "/v1/synthesis"):
            return None
        content_length = 0
        for name, value in headers:
            if name == b"content-length":
                try:
                    content_length = int(value.decode("latin-1"))
                except ValueError:
                    content_length = 0
        if content_length > _env_int("XIANGHUA_SYNTHESIS_MAX_BODY_BYTES", DEFAULT_SYNTHESIS_MAX_BODY_BYTES):
            return 413, "台词文本过大，请分批提交。", "PAYLOAD_TOO_LARGE"
        # 队列上限：进门前就挡掉，避免无限入队占死 GPU。
        from backend.services.synthesis_jobs import SYNTHESIS_JOBS

        if SYNTHESIS_JOBS.active_count() >= _env_int("XIANGHUA_SYNTHESIS_QUEUE_MAX", DEFAULT_SYNTHESIS_QUEUE_MAX):
            return 429, "合成队列已满，请等待当前任务完成后再提交。", "RATE_LIMITED"
        return None

    # --------------------------------------------------------------- ASGI

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(("/api/", "/v1/")):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []

        def header(name: str) -> str | None:
            target = name.encode("latin-1")
            for key, value in headers:
                if key == target:
                    return value.decode("latin-1")
            return None

        if not self._host_allowed(header("host")):
            await self._reject(send, 403, "请求的 Host 不在允许列表。", "FORBIDDEN_HOST")
            return
        if not self._origin_allowed(header("origin")):
            await self._reject(send, 403, "不允许的跨源请求。", "FORBIDDEN_ORIGIN")
            return
        if path.startswith("/v1/"):
            verdict = self._api_key_verdict(headers)
            if verdict is not None:
                status, detail, code = verdict
                await self._reject(send, status, detail, code)
                return
        flood = self._synthesis_flood_blocked(scope, headers)
        if flood is not None:
            status, detail, code = flood
            await self._reject(send, status, detail, code)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, status: int, detail: str, code: str) -> None:
        payload = _json_payload(status, detail, code)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
