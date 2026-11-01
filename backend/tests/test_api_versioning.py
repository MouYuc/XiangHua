"""The /v1 contract must be a faithful twin of the internal /api surface."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module


class ApiVersioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = app_module.create_app()
        cls.client = TestClient(cls.app)

    def test_every_api_path_has_a_v1_twin(self) -> None:
        paths = set(self.app.openapi()["paths"])
        api = {path for path in paths if path.startswith("/api/")}
        v1 = {path for path in paths if path.startswith("/v1/")}
        self.assertGreater(len(api), 40, "route count dropped unexpectedly")
        self.assertEqual({path[4:] for path in api}, {path[3:] for path in v1})

    def test_each_path_exposes_the_same_methods_on_both_prefixes(self) -> None:
        document = self.app.openapi()["paths"]
        for path, operations in document.items():
            if not path.startswith("/api/"):
                continue
            twin = document.get("/v1/" + path[len("/api/"):])
            self.assertIsNotNone(twin, f"{path} has no /v1 twin")
            self.assertEqual(set(operations), set(twin), path)

    def test_health_responds_on_both_prefixes(self) -> None:
        for prefix in ("/api", "/v1"):
            response = self.client.get(f"{prefix}/health")
            self.assertEqual(response.status_code, 200, prefix)
            self.assertEqual(response.json(), {"status": "ok"})

    def test_config_and_overview_keep_their_shape_on_v1(self) -> None:
        config = self.client.get("/v1/config")
        self.assertEqual(config.status_code, 200)
        self.assertIn("gpt_sovits_root", config.json())
        self.assertIn("accent_level", config.json())
        overview = self.client.get("/v1/overview").json()
        self.assertIn("gpu", overview)
        self.assertIn("config", overview)

    def test_jobs_shape_on_v1(self) -> None:
        body = self.client.get("/v1/jobs").json()
        for key in ("jobs", "counts", "by_kind", "errors"):
            self.assertIn(key, body)

    def test_unknown_v1_path_is_a_json_404_not_the_webui(self) -> None:
        response = self.client.get("/v1/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response.headers.get("content-type", ""))
        body = response.json()
        self.assertEqual(body["code"], "NOT_FOUND")
        self.assertIn("接口不存在", body["detail"])

    def test_unknown_api_path_is_a_json_404_too(self) -> None:
        response = self.client.get("/api/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response.headers.get("content-type", ""))

    def test_errors_carry_a_stable_code_next_to_detail(self) -> None:
        response = self.client.post("/v1/jobs/nope/cancel")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "NOT_FOUND")
        self.assertTrue(body["detail"])

    def test_validation_errors_stay_fastapi_shaped(self) -> None:
        response = self.client.post("/v1/synthesis", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("detail", response.json())

    def test_internal_api_still_serves_the_webui(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
