# -*- coding: utf-8 -*-
"""tokenID 校验：对齐 kblib token_verify（GET {ipsec}/auth/authenticate）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

from service import config

_AUTH_TIMEOUT = 15.0
_lock = threading.Lock()
_cache: dict[str, "_CacheEntry"] = {}
_EXPIRED = 72000.0
_REFRESH = 120.0


@dataclass
class _CacheEntry:
    user_id: str
    user_name: str
    last_http: float
    check_time: float


def _pick_uid(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for k in ("userId", "userID", "user_id"):
        v = data.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _pick_name(data) -> str:
    if not isinstance(data, dict):
        return ""
    for k in ("fullname", "fullName", "username", "userName"):
        v = data.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _http_authenticate(token_id: str) -> Tuple[str, str, Optional[str], str]:
    base = config.ipsec_base_url()
    url = f"{base}/auth/authenticate"
    try:
        r = requests.get(url, params={"tokenID": token_id}, timeout=_AUTH_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException:
        return "transport", "身份验证服务不可用", None, ""
    except Exception:
        return "transport", "身份验证服务不可用", None, ""
    if not isinstance(payload, dict) or not payload.get("success"):
        return "invalid", str((payload or {}).get("message") or "身份无效。"), None, ""
    data = payload.get("data")
    uid = _pick_uid(data)
    if not uid:
        return "invalid", "身份无效。", None, ""
    return "ok", "验证成功", uid, _pick_name(data)


def verify_token(token_id: Optional[str]) -> Tuple[bool, str, Optional[str], str]:
    if config.auth_disabled():
        return True, "ok", "0", "auth_disabled"
    tid = (token_id or "").strip()
    if not tid:
        return False, "fail, tokenID 不能为空", None, ""

    now = time.time()
    with _lock:
        ent = _cache.get(tid)
        if ent:
            if now - ent.check_time > _EXPIRED:
                del _cache[tid]
            else:
                ent.check_time = now
                need_refresh = (now - ent.last_http) >= _REFRESH
                if not need_refresh:
                    return True, "ok", ent.user_id, ent.user_name
                cached = ent
        else:
            cached = None

    status, msg, uid, name = _http_authenticate(tid)
    if status == "ok" and uid:
        with _lock:
            _cache[tid] = _CacheEntry(user_id=uid, user_name=name, last_http=now, check_time=now)
        return True, "ok", uid, name

    if cached is not None and status == "transport":
        return True, "ok", cached.user_id, cached.user_name

    with _lock:
        _cache.pop(tid, None)
    return False, f"fail, {msg}" if not msg.startswith("fail") else msg, None, ""


def extract_token(req) -> Optional[str]:
    auth = (req.headers.get("Authorization") or req.headers.get("authorization") or "") or ""
    if isinstance(auth, str) and auth.lower().startswith("bearer "):
        t = auth[7:].strip()
        if t:
            return t
    for h in ("tokenID", "X-Token-ID", "tokenId"):
        v = req.headers.get(h)
        if v and str(v).strip():
            return str(v).strip()
    qs = req.args.get("tokenID") or req.args.get("tokenId")
    if qs and str(qs).strip():
        return str(qs).strip()
    if req.is_json:
        body = req.get_json(silent=True) or {}
        if isinstance(body, dict):
            for k in ("tokenID", "tokenId"):
                v = body.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
    return None
