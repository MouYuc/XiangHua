"""ApiKeyRegistry 契约测试：token 哈希、限速、配额与持久化。"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.api_keys import ApiKeyError, ApiKeyRegistry  # noqa: E402


def _today() -> str:
    return date.today().isoformat()


class CreateAndAuthenticateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "api_keys.json"
        self.registry = ApiKeyRegistry(self.path)

    def test_create_returns_token_once_and_never_persists_it(self) -> None:
        created = self.registry.create_key("tenant-a", label="prod")
        self.assertTrue(created["token"].startswith("xh_"))
        self.assertEqual(len(created["token"]), 3 + 48)
        self.assertTrue(created["key_id"].startswith("k_"))
        self.assertNotIn("key_hash", created)
        self.assertEqual(created["tenant"], "tenant-a")
        self.assertEqual(created["label"], "prod")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["keys"][0]["key_id"], created["key_id"])
        self.assertIn("key_hash", stored["keys"][0])
        self.assertNotIn("token", stored["keys"][0])

    def test_authenticate_accepts_real_token_and_rejects_others(self) -> None:
        created = self.registry.create_key("tenant-a")
        record = self.registry.authenticate(created["token"])
        self.assertIsNotNone(record)
        self.assertEqual(record["key_id"], created["key_id"])
        self.assertNotIn("key_hash", record)
        self.assertIsNone(self.registry.authenticate("xh_" + "0" * 48))
        self.assertIsNone(self.registry.authenticate(""))

    def test_empty_tenant_is_rejected(self) -> None:
        with self.assertRaises(ApiKeyError):
            self.registry.create_key("   ")

    def test_registry_reload_keeps_keys_authenticatable(self) -> None:
        created = self.registry.create_key("tenant-a")
        reloaded = ApiKeyRegistry(self.path)
        record = reloaded.authenticate(created["token"])
        self.assertIsNotNone(record)
        self.assertEqual(record["key_id"], created["key_id"])


class QuotaAndRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "api_keys.json"
        self.registry = ApiKeyRegistry(self.path)

    def test_rpm_limit_blocks_fourth_burst(self) -> None:
        created = self.registry.create_key(
            "tenant-a", daily_request_quota=100, rpm_limit=3
        )
        for _ in range(3):
            ok, reason = self.registry.check_and_count(created["key_id"])
            self.assertTrue(ok, reason)
        ok, reason = self.registry.check_and_count(created["key_id"])
        self.assertFalse(ok)
        self.assertIn("每分钟", reason)
        self.assertIn("3", reason)

    def test_daily_quota_blocks_third_request(self) -> None:
        created = self.registry.create_key(
            "tenant-a", daily_request_quota=2, rpm_limit=100
        )
        for _ in range(2):
            ok, reason = self.registry.check_and_count(created["key_id"])
            self.assertTrue(ok, reason)
        ok, reason = self.registry.check_and_count(created["key_id"])
        self.assertFalse(ok)
        self.assertIn("今日", reason)
        self.assertIn("配额", reason)
        self.assertIn("明天重置", reason)

    def test_usage_date_rollover_resets_counters(self) -> None:
        created = self.registry.create_key(
            "tenant-a", daily_request_quota=1, rpm_limit=100
        )
        ok, reason = self.registry.check_and_count(created["key_id"])
        self.assertTrue(ok, reason)
        blocked, _ = self.registry.check_and_count(created["key_id"])
        self.assertFalse(blocked)  # quota=1 已用尽

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["usage"][created["key_id"]]["date"] = yesterday
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        ok, reason = self.registry.check_and_count(created["key_id"])
        self.assertTrue(ok, reason)
        usage = self.registry.usage(created["key_id"])
        self.assertEqual(usage["date"], _today())
        self.assertEqual(usage["requests"], 1)

    def test_record_chars_accumulates_and_persists(self) -> None:
        created = self.registry.create_key("tenant-a")
        self.registry.check_and_count(created["key_id"])
        self.registry.record_chars(created["key_id"], 5)
        self.registry.record_chars(created["key_id"], 7)
        usage = self.registry.usage(created["key_id"])
        self.assertEqual(usage["chars"], 12)
        self.assertEqual(usage["requests"], 1)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["usage"][created["key_id"]]["chars"], 12)

    def test_unknown_key_usage_is_zero_view(self) -> None:
        usage = self.registry.usage("k_nope")
        self.assertEqual(usage["requests"], 0)
        self.assertEqual(usage["chars"], 0)
        ok, _ = self.registry.check_and_count("k_nope")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
