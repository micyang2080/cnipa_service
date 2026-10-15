# -*- coding: utf-8 -*-
import os
import unittest

os.environ["CNIPA_DISABLE_WORKER"] = "1"
os.environ["CNIPA_AUTH_DISABLED"] = "1"

from service import query_key as qk


class TestQueryKey(unittest.TestCase):
    def test_type2_normalize_appno(self):
        p = qk.build_query_payload(2, {"application_no": "CN200610004173.6"})
        self.assertEqual(p["application_no"], "200610004173.6")
        self.assertEqual(p["task_type"], 2)
        self.assertFalse(p["force_refresh"])

    def test_type2_force_same_key(self):
        a = qk.query_key(qk.build_query_payload(2, {"application_no": "200610004173.6"}))
        b = qk.query_key(
            qk.build_query_payload(2, {"application_no": "200610004173.6", "force_refresh": True})
        )
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_type1_stable_key(self):
        body = {
            "library_type": "invalid",
            "filters": {"application_no": "200610004173.6", "case_no": ""},
            "max_items": 50,
        }
        p = qk.build_query_payload(1, body)
        self.assertEqual(p["max_items"], 10)
        k1 = qk.query_key(p)
        k2 = qk.query_key(qk.build_query_payload(1, dict(body)))
        self.assertEqual(k1, k2)

    def test_type1_empty_filters(self):
        self.assertTrue(qk.filters_empty({}))
        self.assertTrue(qk.filters_empty({"case_no": "  "}))
        self.assertFalse(qk.filters_empty({"case_no": "4W107577"}))

    def test_library_reexam(self):
        p = qk.build_query_payload(
            1, {"library_type": "reexam", "filters": {"decision_no": "1"}}
        )
        self.assertEqual(p["library_type"], "reexam")


if __name__ == "__main__":
    unittest.main()
