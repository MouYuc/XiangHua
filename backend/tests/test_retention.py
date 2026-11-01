"""Disk retention policy.

The rule under test is the conservative one: synthesised run directories older
than the policy allows may go, but never a directory the history or download
registry still points at, never a category folder like ``output/vc``, and never
anything at all unless ``apply=True`` is passed explicitly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core, retention


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        patcher = patch.object(core, "OUTPUT_DIR", self.output)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_dir(self, name: str, *, age_days: float, size: int = 1024) -> Path:
        directory = self.output / name
        directory.mkdir()
        (directory / "001.wav").write_bytes(b"x" * size)
        stamp = time.time() - age_days * 86400
        os.utime(directory, (stamp, stamp))
        return directory

    def test_old_run_directories_are_reported_and_kept_on_dry_run(self) -> None:
        old = self._run_dir("20260101_120000_aaaaaaaa", age_days=40)
        fresh = self._run_dir("20260901_120000_bbbbbbbb", age_days=1)
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            plan = retention.scan(max_age_days=30)

        paths = [entry.path for entry in plan.entries]
        self.assertEqual(paths, [str(old)])
        self.assertTrue(fresh.is_dir())
        self.assertGreater(plan.reclaimable_bytes, 0)

    def test_apply_deletes_only_the_planned_directories(self) -> None:
        old = self._run_dir("20260101_120000_aaaaaaaa", age_days=40)
        fresh = self._run_dir("20260901_120000_bbbbbbbb", age_days=1)
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            result = retention.prune(max_age_days=30, apply=True)

        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())
        self.assertEqual(result["removed"], 1)
        self.assertGreater(result["freed_bytes"], 0)

    def test_directories_still_referenced_by_history_are_kept(self) -> None:
        old = self._run_dir("20260101_120000_aaaaaaaa", age_days=90)
        kept = old / "001.wav"
        history = [{"file": str(kept)}]
        with (
            patch.object(core, "load_history", return_value=history),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            result = retention.prune(max_age_days=1, apply=True)

        self.assertTrue(old.is_dir())
        self.assertEqual(result["removed"], 0)

    def test_directories_still_referenced_by_the_download_registry_are_kept(self) -> None:
        old = self._run_dir("20260101_120000_aaaaaaaa", age_days=90)
        downloads = {"a" * 32: {"path": str(old / "001.wav")}}
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value=downloads),
        ):
            result = retention.prune(max_age_days=1, apply=True)

        self.assertTrue(old.is_dir())
        self.assertEqual(result["removed"], 0)

    def test_category_folders_are_never_touched(self) -> None:
        """``output/vc`` belongs to the timbre pipeline, not to synthesis runs."""
        vc = self.output / "vc"
        vc.mkdir()
        (vc / "converted.wav").write_bytes(b"x" * 10)
        stamp = time.time() - 400 * 86400
        os.utime(vc, (stamp, stamp))
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            result = retention.prune(max_age_days=1, apply=True)

        self.assertTrue(vc.is_dir())
        self.assertEqual(result["removed"], 0)

    def test_count_cap_evicts_the_oldest_even_when_young(self) -> None:
        dirs = [
            self._run_dir(f"2026010{index}_120000_0000000{index}", age_days=index + 1)
            for index in range(1, 4)
        ]
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            plan = retention.scan(max_age_days=365, max_output_dirs=1)

        planned = {entry.path for entry in plan.entries}
        self.assertEqual(planned, {str(dirs[1]), str(dirs[2])})

    def test_missing_output_directory_is_not_an_error(self) -> None:
        with patch.object(core, "OUTPUT_DIR", self.root / "does-not-exist"):
            plan = retention.scan()
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.reclaimable_bytes, 0)

    def test_policy_is_echoed_back_for_audit(self) -> None:
        with (
            patch.object(core, "load_history", return_value=[]),
            patch.object(core, "_load_downloads", return_value={}),
        ):
            result = retention.prune(max_age_days=7, max_output_dirs=3, apply=False)
        self.assertFalse(result["applied"])
        self.assertEqual(result["policy"]["max_age_days"], 7)
        self.assertEqual(result["policy"]["max_output_dirs"], 3)


if __name__ == "__main__":
    unittest.main()
