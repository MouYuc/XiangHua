"""JobStore 契约测试：排序、limit、坏文件跳过与清理。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.job_store import JobStore  # noqa: E402


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = JobStore(self.root)

    def _save(self, job_id, updated_at: str) -> None:
        self.store.save({"job_id": job_id, "updated_at": updated_at, "status": "done"})

    def test_save_then_load_recent_orders_desc_and_applies_limit(self) -> None:
        self._save("job_a", "2026-10-24T09:00:00+08:00")
        self._save("job_b", "2026-10-25T11:00:00+08:00")
        self._save("job_c", "2026-10-25T10:00:00+08:00")
        recent = self.store.load_recent()
        self.assertEqual(
            [item["job_id"] for item in recent], ["job_b", "job_c", "job_a"]
        )
        limited = self.store.load_recent(limit=2)
        self.assertEqual(
            [item["job_id"] for item in limited], ["job_b", "job_c"]
        )

    def test_invalid_job_ids_are_skipped(self) -> None:
        self.store.save({"updated_at": "2026-10-25T10:00:00+08:00"})  # 无 job_id
        self.store.save({"job_id": "", "updated_at": "2026-10-25T10:00:00+08:00"})
        self.store.save({"job_id": 123, "updated_at": "2026-10-25T10:00:00+08:00"})
        self.store.save(None)  # type: ignore[arg-type]
        self.store.save({"job_id": "../escape", "updated_at": "x"})
        self.assertEqual(self.store.load_recent(), [])

    def test_overwrite_same_job_id_keeps_one_file(self) -> None:
        self._save("job_a", "2026-10-25T10:00:00+08:00")
        self._save("job_a", "2026-10-25T11:00:00+08:00")
        recent = self.store.load_recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["updated_at"], "2026-10-25T11:00:00+08:00")

    def test_corrupt_json_files_are_skipped(self) -> None:
        self._save("job_a", "2026-10-25T11:00:00+08:00")
        jobs_dir = self.root / "jobs"
        (jobs_dir / "broken.json").write_text("{not json", encoding="utf-8")
        recent = self.store.load_recent()
        self.assertEqual([item["job_id"] for item in recent], ["job_a"])

    def test_purge_keeps_newest_snapshots(self) -> None:
        for index in range(5):
            self._save(f"job_{index}", f"2026-10-25T10:00:0{index}:00+08:00")
        deleted = self.store.purge(keep=2)
        self.assertEqual(deleted, 3)
        remaining = self.store.load_recent()
        self.assertEqual([item["job_id"] for item in remaining], ["job_4", "job_3"])
        self.assertEqual(len(list((self.root / "jobs").glob("*.json"))), 2)
        # 已低于 keep 时不再删除
        self.assertEqual(self.store.purge(keep=200), 0)

    def test_load_recent_on_missing_directory_is_empty(self) -> None:
        self.assertEqual(self.store.load_recent(), [])
        self.assertEqual(self.store.purge(), 0)


if __name__ == "__main__":
    unittest.main()
