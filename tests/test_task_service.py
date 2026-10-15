# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["CNIPA_DISABLE_WORKER"] = "1"
os.environ["CNIPA_AUTH_DISABLED"] = "1"

from service import task_service


def _task_row(tid=10, status=1, task_type=2, result=None):
    return {
        "task_id": tid,
        "status": status,
        "task_type": task_type,
        "progress": None,
        "error_code": None,
        "error_message": None,
        "result_json": result,
        "query_json": {},
    }


class TestTaskService(unittest.TestCase):
    def test_filter_empty(self):
        data, code = task_service.create_task({"task_type": 1, "filters": {}})
        self.assertEqual(code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_code"], "FILTER_EMPTY")

    def test_bad_task_type(self):
        data, code = task_service.create_task({"task_type": 9})
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "BAD_REQUEST")

    def test_type2_missing_appno(self):
        data, code = task_service.create_task({"task_type": 2})
        self.assertEqual(code, 400)

    @patch("service.task_service.db.conn")
    def test_create_insert(self, mock_conn):
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock(cursor=MagicMock(return_value=cur))
        ctx.__exit__.return_value = False
        mock_conn.return_value = ctx

        with patch("service.task_service.db.find_active_by_query_key", return_value=None), patch(
            "service.task_service.db.insert_task", return_value=99
        ), patch(
            "service.task_service.db.get_task", return_value=_task_row(99, 1, 2)
        ):
            data, code = task_service.create_task(
                {"task_type": 2, "application_no": "200610004173.6"}
            )
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["task_id"], 99)
        self.assertEqual(data["status"], 1)

    @patch("service.task_service.db.conn")
    def test_create_force_skips_dedup(self, mock_conn):
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock(cursor=MagicMock(return_value=cur))
        ctx.__exit__.return_value = False
        mock_conn.return_value = ctx
        with patch("service.task_service.db.find_active_by_query_key") as find, patch(
            "service.task_service.db.insert_task", return_value=100
        ), patch(
            "service.task_service.db.get_task", return_value=_task_row(100, 1, 2)
        ):
            data, code = task_service.create_task(
                {"task_type": 2, "application_no": "200610004173.6", "force_refresh": True}
            )
        find.assert_not_called()
        self.assertEqual(code, 200)
        self.assertEqual(data["task_id"], 100)
        self.assertEqual(data["status"], 1)

    @patch("service.task_service.db.conn")
    def test_create_dedup(self, mock_conn):
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock(cursor=MagicMock(return_value=cur))
        ctx.__exit__.return_value = False
        mock_conn.return_value = ctx
        existing = _task_row(7, 3, 2, {"success": True, "application_no": "200610004173.6"})
        with patch("service.task_service.db.find_active_by_query_key", return_value=existing), patch(
            "service.task_service.db.get_figures",
            return_value=[{"application_no": "200610004173.6", "has_figure": 1, "rel_path": "/200610004173.6/figure.png"}],
        ):
            data, code = task_service.create_task(
                {"task_type": 2, "application_no": "200610004173.6"}
            )
        self.assertEqual(code, 200)
        self.assertEqual(data["task_id"], 7)
        self.assertEqual(data["status"], 3)
        self.assertNotIn("figures", data)
        self.assertEqual(data["result"]["application_no"], "200610004173.6")
        self.assertEqual(data["result"]["has_figure"], 1)
        self.assertEqual(data["result"]["figure_path"], "/200610004173.6/figure.png")

    @patch("service.task_service.db.conn")
    def test_create_type1_missing_pdf_skips_dedup(self, mock_conn):
        """旧任务 status=3 有结果但盘上无 PDF → 不复用，新建任务。"""
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock(cursor=MagicMock(return_value=cur))
        ctx.__exit__.return_value = False
        mock_conn.return_value = ctx
        existing = _task_row(
            7, 3, 1,
            {
                "library_type": "invalid",
                "items": [{
                    "application_no": "202010200834.2",
                    "case_no": "4W118386",
                }],
                "details": [{
                    "application_no": "202010200834.2",
                    "case_no": "4W118386",
                    "decision_file_url": "/cnipa_file/202010200834.2/4W118386.pdf",
                }],
            },
        )
        with patch("service.task_service.db.find_active_by_query_key", return_value=existing), patch(
            "service.task_service.db.insert_task", return_value=101
        ) as ins, patch(
            "service.task_service.db.get_task", return_value=_task_row(101, 1, 1)
        ), patch("service.task_service._pdf_on_disk", return_value=False):
            data, code = task_service.create_task({
                "task_type": 1,
                "library_type": "invalid",
                "filters": {"application_no": "202010200834.2"},
            })
        ins.assert_called_once()
        self.assertEqual(code, 200)
        self.assertEqual(data["task_id"], 101)
        self.assertEqual(data["status"], 1)

    @patch("service.task_service.db.conn")
    def test_get_not_found(self, mock_conn):
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = MagicMock(cursor=MagicMock(return_value=cur))
        ctx.__exit__.return_value = False
        mock_conn.return_value = ctx
        with patch("service.task_service.db.get_task", return_value=None):
            data, code = task_service.get_task(1)
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
