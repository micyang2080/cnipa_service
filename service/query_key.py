# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_PY = Path(__file__).resolve().parent.parent / "python"
if str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

import cnipa_cache  # noqa: E402
import cnipa_contract  # noqa: E402

FILTER_KEYS = cnipa_cache.CACHE_FILTER_KEYS
RESULT_CAP = 10


def _norm_page(page) -> int:
    try:
        n = int(page)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def _norm_max_items(max_items) -> int:
    try:
        n = int(max_items) if max_items is not None else RESULT_CAP
    except (TypeError, ValueError):
        n = RESULT_CAP
    return min(max(1, n), RESULT_CAP)


def _norm_force(force_refresh) -> bool:
    if isinstance(force_refresh, bool):
        return force_refresh
    if force_refresh is None:
        return False
    return str(force_refresh).strip().lower() in ("1", "true", "yes")


def build_query_payload(task_type: int, body: dict) -> dict:
    force = _norm_force(body.get("force_refresh"))
    if int(task_type) == 2:
        app = cnipa_contract.dotted_appno(body.get("application_no") or "")
        return {
            "task_type": 2,
            "application_no": app,
            "force_refresh": force,
        }
    filters = cnipa_cache.norm_filters(body.get("filters") or {})
    lib = body.get("library_type") or "invalid"
    lib = "reexam" if cnipa_contract.review_type_of(lib) == 1 else "invalid"
    return {
        "task_type": 1,
        "library_type": lib,
        "filters": filters,
        "max_items": _norm_max_items(body.get("max_items")),
        "page": _norm_page(body.get("page")),
        "force_refresh": force,
    }


def filters_empty(filters: dict) -> bool:
    return not bool(cnipa_cache.norm_filters(filters or {}))


def query_key(payload: dict) -> str:
    # force_refresh 不入键：同条件去重；force 时由 create 跳过去重并透传 UIA
    body = {k: v for k, v in payload.items() if k != "force_refresh"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dotted_appno(value: Any) -> str:
    return cnipa_contract.dotted_appno(value)
