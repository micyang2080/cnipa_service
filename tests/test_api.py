# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

os.environ["CNIPA_DISABLE_WORKER"] = "1"
os.environ["CNIPA_AUTH_DISABLED"] = "1"

from service.app import app


class TestApi(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_health(self):
        r = self.client.get("/cnipa/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_create_filter_empty(self):
        r = self.client.post(
            "/cnipa/v1/tasks",
            json={"task_type": 1, "filters": {}, "tokenID": "x"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "FILTER_EMPTY")

    @patch("service.task_service.create_task")
    def test_create_ok(self, mock_create):
        mock_create.return_value = (
            {
                "ok": True,
                "task_id": 1,
                "status": 1,
                "task_type": 2,
                "progress": None,
                "error_code": None,
                "error_message": None,
                "result": None,
                "figures": [],
            },
            200,
        )
        r = self.client.post(
            "/cnipa/v1/tasks",
            json={"task_type": 2, "application_no": "200610004173.6", "tokenID": "x"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["task_id"], 1)

    @patch("service.task_service.get_task")
    def test_get_task(self, mock_get):
        mock_get.return_value = (
            {
                "ok": True,
                "task_id": 5,
                "status": 3,
                "task_type": 2,
                "progress": None,
                "error_code": None,
                "error_message": None,
                "result": {"success": True, "application_no": "200610004173.6"},
                "figures": [
                    {
                        "application_no": "200610004173.6",
                        "has_figure": 1,
                        "rel_path": "200610004173.6/figure.png",
                    }
                ],
            },
            200,
        )
        r = self.client.get("/cnipa/v1/tasks/5", headers={"tokenID": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], 3)

    @patch("service.auth.config.auth_disabled", return_value=False)
    @patch("service.auth.verify_token", return_value=(False, "fail, 身份无效。", None, ""))
    def test_auth_401(self, _v, _d):
        # 重新挂载前需要确保 before_request 走真实校验
        r = self.client.post("/cnipa/v1/tasks", json={"task_type": 2, "application_no": "1"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["code"], 4010)


if __name__ == "__main__":
    unittest.main()
