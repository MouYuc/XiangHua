"""Route contracts for the unified job surface and the GPU status snapshot."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module  # noqa: E402


class JobRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.create_app())

    def test_list_jobs_returns_the_aggregate_shape(self) -> None:
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("jobs", "counts", "by_kind", "errors"):
            self.assertIn(key, body)
        self.assertIsInstance(body["jobs"], list)
        self.assertIsInstance(body["errors"], list)

    def test_unknown_job_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/jobs/does_not_exist").status_code, 404)
        self.assertEqual(self.client.post("/api/jobs/does_not_exist/cancel").status_code, 404)

    def test_cancel_on_a_non_cancelable_job_is_409(self) -> None:
        fake = SimpleNamespace(
            get=lambda job_id: {"job_id": job_id, "kind": "synthesis", "cancelable": False},
            cancel=lambda job_id: {"cancelled": False, "reason": "该类任务暂不支持取消。"},
        )
        with patch.object(app_module, "JOB_REGISTRY", fake):
            response = self.client.post("/api/jobs/synth_1/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertIn("不支持", response.json()["detail"])

    def test_overview_exposes_gpu_status(self) -> None:
        data = self.client.get("/api/overview").json()
        self.assertIn("gpu", data)
        self.assertIn("busy", data["gpu"])
        self.assertIn("queue", data["gpu"])


if __name__ == "__main__":
    unittest.main()
