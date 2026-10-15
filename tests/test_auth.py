# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["CNIPA_DISABLE_WORKER"] = "1"
os.environ["CNIPA_AUTH_DISABLED"] = "0"

from service import auth


class TestAuth(unittest.TestCase):
    def setUp(self):
        auth._cache.clear()

    @patch("service.auth.config.auth_disabled", return_value=True)
    def test_disabled(self, _):
        ok, msg, uid, _name = auth.verify_token("")
        self.assertTrue(ok)
        self.assertEqual(uid, "0")

    @patch("service.auth.config.auth_disabled", return_value=False)
    def test_empty_token(self, _):
        ok, msg, uid, _name = auth.verify_token("")
        self.assertFalse(ok)
        self.assertIn("tokenID", msg)

    @patch("service.auth.config.auth_disabled", return_value=False)
    @patch("service.auth.config.ipsec_base_url", return_value="https://example.test/ip_sec/interface")
    @patch("service.auth.requests.get")
    def test_http_ok(self, mock_get, _url, _dis):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "success": True,
            "data": {"userId": "42", "fullname": "张三"},
        }
        mock_get.return_value = resp
        ok, msg, uid, name = auth.verify_token("tok-abc")
        self.assertTrue(ok)
        self.assertEqual(uid, "42")
        self.assertEqual(name, "张三")
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertIn("/auth/authenticate", args[0])
        self.assertEqual(kwargs["params"]["tokenID"], "tok-abc")

    @patch("service.auth.config.auth_disabled", return_value=False)
    @patch("service.auth.config.ipsec_base_url", return_value="https://example.test/ip_sec/interface")
    @patch("service.auth.requests.get")
    def test_http_invalid(self, mock_get, _url, _dis):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"success": False, "message": "身份无效。"}
        mock_get.return_value = resp
        ok, msg, uid, _name = auth.verify_token("bad")
        self.assertFalse(ok)
        self.assertIsNone(uid)


if __name__ == "__main__":
    unittest.main()
