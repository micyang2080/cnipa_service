# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from service import db
from service import query_key as qk

# 与 cnipa_contract.CASE_OUT_KEYS 对齐（服务侧不依赖 python/ 导入）
_CASE_KEYS = (
    "source", "review_type",
    "case_no", "decision_no",
    "issue_date", "decision_date",
    "invention_title", "petitioner", "patentee", "application_no",
    "request_date", "legal_basis", "decision_summary", "decision_result",
    "panel_members", "figure_path", "detail_url",
    "patent_type", "ipc_class", "agency",
    "decision_file_url",
)

# 详情优先覆盖列表的字段（列表里常空或较弱）
_DETAIL_PREFER = (
    "decision_date", "request_date", "legal_basis",
    "decision_summary", "decision_result", "panel_members",
    "invention_title", "petitioner", "patentee", "decision_no",
)


def _ck(row: dict) -> tuple:
    return (
        qk.dotted_appno(row.get("application_no")) or "",
        str(row.get("case_no") or "").strip().upper(),
    )


def _pick(a: Any, b: Any) -> Any:
    """b 非空则用 b，否则 a。"""
    if b is None:
        return a if a is not None else ""
    if isinstance(b, str) and not b.strip():
        return a if a is not None else ""
    return b


def _empty_case() -> dict:
    return {k: "" for k in _CASE_KEYS}


def _pdf_on_disk(app: str, case: str) -> bool:
    app = qk.dotted_appno(app) or ""
    case = str(case or "").strip()
    if not app or not case:
        return False
    try:
        from pathlib import Path
        from service import config
        p = Path(config.file_root()) / app / f"{case}.pdf"
        return p.is_file() and p.stat().st_size > 0
    except Exception:
        return False


def _decision_file_url(app: str, case: str, existing: str = "") -> str:
    """仅当盘上确有 PDF 才返回 URL；不信任 result_json 里的过期路径。"""
    del existing  # 兼容旧调用，不以库内 URL 为准
    app = qk.dotted_appno(app) or ""
    case = str(case or "").strip()
    if _pdf_on_disk(app, case):
        return f"/cnipa_file/{app}/{case}.pdf"
    return ""


def _case_keys_from_result(raw: Any) -> list:
    """从 result_json 收集 (application_no, case_no)，去重。"""
    if not isinstance(raw, dict):
        return []
    seen = set()
    out = []
    for key in ("details", "items"):
        for row in raw.get(key) or []:
            if not isinstance(row, dict):
                continue
            app = qk.dotted_appno(row.get("application_no")) or ""
            case = str(row.get("case_no") or "").strip()
            if not app or not case:
                continue
            k = (app, case.upper())
            if k in seen:
                continue
            seen.add(k)
            out.append((app, case))
    return out


def _task1_reuse_ok(task: dict) -> bool:
    """task_type=1 可复用：status=3 且每条决定 PDF 已在交付盘；其它状态不复用。"""
    st = int(task.get("status") or 0)
    if st != 3:
        return False
    keys = _case_keys_from_result(task.get("result_json"))
    if not keys:
        return False
    return all(_pdf_on_disk(app, case) for app, case in keys)


def _running_task_alive(task: dict, max_sec: int = 120) -> bool:
    """status=2 且仍在合理执行窗口内 → 视为同一次运行可去重。"""
    if int(task.get("status") or 0) != 2:
        return False
    started = task.get("started_at")
    if started is None:
        return False
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return False
    try:
        age = (datetime.now() - started.replace(tzinfo=None)).total_seconds()
    except Exception:
        return False
    return age < max_sec


def _merge_case(item: Optional[dict], detail: Optional[dict], fig_path: str) -> dict:
    out = _empty_case()
    item = item if isinstance(item, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    for k in _CASE_KEYS:
        if k in ("figure_path", "decision_file_url"):
            continue
        iv, dv = item.get(k), detail.get(k)
        if k in _DETAIL_PREFER:
            out[k] = _pick(iv, dv)
        else:
            out[k] = _pick(dv, iv)  # 列表特有字段（issue_date/ipc 等）优先 item
    app = qk.dotted_appno(out.get("application_no") or item.get("application_no")
                          or detail.get("application_no"))
    out["application_no"] = app or ""
    rt = out.get("review_type")
    if rt in ("", None):
        rt = item.get("review_type", detail.get("review_type", ""))
    try:
        out["review_type"] = int(rt) if rt not in ("", None) else ""
    except (TypeError, ValueError):
        out["review_type"] = rt if rt is not None else ""
    if not out.get("source"):
        out["source"] = "cnipa"
    out["figure_path"] = fig_path or ""
    existing_url = str(
        detail.get("decision_file_url") or item.get("decision_file_url") or ""
    ).strip()
    out["decision_file_url"] = _decision_file_url(
        app or "", str(out.get("case_no") or detail.get("case_no") or item.get("case_no") or ""),
        existing_url,
    )
    return out


def _fig_map(figures: Optional[list], apps: Optional[list] = None) -> dict:
    from service.db import norm_rel_path

    m: dict[str, str] = {}
    for f in figures or []:
        if not isinstance(f, dict):
            continue
        app = qk.dotted_appno(f.get("application_no")) or ""
        if not app:
            continue
        if int(f.get("has_figure") or 0) == 1 and f.get("rel_path"):
            rp = norm_rel_path(str(f["rel_path"]))
            if rp:
                m[app] = rp
    # 库未记上但盘上已有图（历史任务）→ 查询时补上
    try:
        from pathlib import Path
        from service import config
        root = Path(config.file_root())
    except Exception:
        return m
    for app in apps or []:
        a = qk.dotted_appno(app) or ""
        if not a or a in m:
            continue
        for name in (f"{a}.png", "figure.png"):
            p = root / a / name
            if p.is_file() and p.stat().st_size > 0:
                m[a] = f"/{a}/{name}"
                break
    return m


def shape_result(task: dict, figures: Optional[list] = None) -> Optional[dict]:
    """把库里的 result_json + figures 收成简洁结构（只影响查询返回，不改库）。"""
    raw = task.get("result_json")
    ttype = int(task.get("task_type") or 0)

    if ttype == 2:
        app = ""
        if isinstance(raw, dict):
            app = qk.dotted_appno(raw.get("application_no")) or ""
        if not app and figures:
            app = qk.dotted_appno((figures[0] or {}).get("application_no")) or ""
        fmap = _fig_map(figures, [app] if app else [])
        fp = fmap.get(app, "")
        return {
            "application_no": app,
            "has_figure": 1 if fp else 0,
            "figure_path": fp,
        }

    if not isinstance(raw, dict):
        return {"library_type": "invalid", "total": 0, "truncated": False, "cases": []}

    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    details = raw.get("details") if isinstance(raw.get("details"), list) else []
    apps = []
    for row in list(items) + list(details):
        if isinstance(row, dict):
            a = qk.dotted_appno(row.get("application_no"))
            if a and a not in apps:
                apps.append(a)
    fmap = _fig_map(figures, apps)
    dmap = {_ck(d): d for d in details if isinstance(d, dict)}
    seen = set()
    cases = []

    for it in items:
        if not isinstance(it, dict):
            continue
        key = _ck(it)
        seen.add(key)
        app = qk.dotted_appno(it.get("application_no")) or ""
        cases.append(_merge_case(it, dmap.get(key), fmap.get(app, "")))

    for d in details:
        if not isinstance(d, dict):
            continue
        key = _ck(d)
        if key in seen:
            continue
        app = qk.dotted_appno(d.get("application_no")) or ""
        cases.append(_merge_case(None, d, fmap.get(app, "")))

    return {
        "library_type": raw.get("library_type") or "invalid",
        "total": raw.get("total") if raw.get("total") is not None else len(cases),
        "truncated": bool(raw.get("truncated")),
        "cases": cases,
    }


def task_response(task: dict, figures: Optional[list] = None) -> dict:
    status = int(task.get("status") or 0)
    out = {
        "ok": True,
        "task_id": int(task["task_id"]),
        "status": status,
        "task_type": int(task.get("task_type") or 0),
        "progress": task.get("progress"),
        "error_code": task.get("error_code"),
        "error_message": task.get("error_message"),
        "result": None,
    }
    if status in (3, 4):
        out["result"] = shape_result(task, figures)
    elif status == 5:
        out["result"] = None
    return out


def create_task(body: dict) -> tuple[dict, int]:
    try:
        task_type = int(body.get("task_type") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "BAD_REQUEST", "error_message": "task_type 无效"}, 400

    if task_type not in (1, 2):
        return {"ok": False, "error_code": "BAD_REQUEST", "error_message": "task_type 须为 1 或 2"}, 400

    if task_type == 1 and qk.filters_empty(body.get("filters") or {}):
        return {
            "ok": False,
            "error_code": "FILTER_EMPTY",
            "error_message": "filters 至少需要一个非空条件",
            "status": 5,
            "task_id": None,
            "result": None,
        }, 200

    if task_type == 2 and not str(body.get("application_no") or "").strip():
        return {
            "ok": False,
            "error_code": "BAD_REQUEST",
            "error_message": "application_no 必填",
        }, 400

    payload = qk.build_query_payload(task_type, body)
    if task_type == 2 and not payload.get("application_no"):
        return {
            "ok": False,
            "error_code": "BAD_REQUEST",
            "error_message": "application_no 无效",
        }, 400

    key = qk.query_key(payload)
    force = bool(payload.get("force_refresh"))

    with db.conn() as c:
        cur = c.cursor()
        if not force:
            existing = db.find_active_by_query_key(cur, key)
            if existing:
                st = int(existing.get("status") or 0)
                tid_ex = int(existing["task_id"])
                # 僵尸 status=2（进程已死、浏览器空转）：作废以便新建任务
                if st == 2 and not _running_task_alive(existing):
                    db.finish_fail(
                        cur, tid_ex, "STALE_RUN",
                        "上次任务长时间无进展，已作废；请重新提交",
                    )
                    existing = None
                if existing:
                    st = int(existing.get("status") or 0)
                    tid_ex = int(existing["task_id"])
                # 排队可复用；执行中仅短窗口内复用；成功仅 type1 且 PDF 齐
                reuse = existing is not None and (
                    st == 1
                    or (st == 2 and _running_task_alive(existing))
                    or (
                        st == 3
                        and (task_type != 1 or _task1_reuse_ok(existing))
                    )
                    or (
                        st == 4
                        and task_type != 1
                    )
                )
                if reuse and existing:
                    figs = db.get_figures(cur, tid_ex)
                    if st in (3, 4):
                        apps = _apps_from_task(existing)
                        need = []
                        have = {
                            qk.dotted_appno(f.get("application_no")): f
                            for f in figs if isinstance(f, dict)
                        }
                        for a in apps:
                            f = have.get(a) or {}
                            if int(f.get("has_figure") or 0) != 1 or not f.get("rel_path"):
                                need.append(a)
                        if need:
                            figs = db.repair_figures_from_disk(cur, tid_ex, need)
                    return task_response(existing, figs), 200
        tid = db.insert_task(cur, task_type, key, payload)
        task = db.get_task(cur, tid)
        return task_response(task, []), 200


def _apps_from_task(task: dict) -> list:
    apps = []
    ttype = int(task.get("task_type") or 0)
    raw = task.get("result_json")
    if ttype == 2:
        if isinstance(raw, dict):
            a = qk.dotted_appno(raw.get("application_no"))
            if a:
                apps.append(a)
        q = task.get("query_json") if isinstance(task.get("query_json"), dict) else {}
        a2 = qk.dotted_appno(q.get("application_no"))
        if a2 and a2 not in apps:
            apps.append(a2)
        return apps
    if not isinstance(raw, dict):
        return apps
    for key in ("items", "details"):
        for row in raw.get(key) or []:
            if isinstance(row, dict):
                a = qk.dotted_appno(row.get("application_no"))
                if a and a not in apps:
                    apps.append(a)
    return apps


def get_task(task_id: int) -> tuple[dict, int]:
    with db.conn(autocommit=True) as c:
        cur = c.cursor()
        task = db.get_task(cur, task_id)
        if not task:
            return {"ok": False, "error_code": "NOT_FOUND", "error_message": "任务不存在"}, 404
        figs = db.get_figures(cur, task_id)
        if int(task.get("status") or 0) in (3, 4):
            apps = _apps_from_task(task)
            # 有盘无库：查询时补账，避免 figures.rel_path 一直 null
            need = []
            have = {
                qk.dotted_appno(f.get("application_no")): f
                for f in figs if isinstance(f, dict)
            }
            for a in apps:
                f = have.get(a) or {}
                if int(f.get("has_figure") or 0) != 1 or not f.get("rel_path"):
                    need.append(a)
            if need:
                figs = db.repair_figures_from_disk(cur, task_id, need)
        return task_response(task, figs), 200


def lookup_task(body: dict) -> tuple[dict, int]:
    try:
        task_type = int(body.get("task_type") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "BAD_REQUEST", "error_message": "task_type 无效"}, 400
    if task_type not in (1, 2):
        return {"ok": False, "error_code": "BAD_REQUEST", "error_message": "task_type 须为 1 或 2"}, 400
    payload = qk.build_query_payload(task_type, body)
    key = qk.query_key(payload)
    with db.conn(autocommit=True) as c:
        cur = c.cursor()
        task = db.find_latest_by_query_key(cur, key)
        if not task:
            return {"ok": False, "error_code": "NOT_FOUND", "error_message": "无匹配任务"}, 404
        figs = db.get_figures(cur, int(task["task_id"]))
        if int(task.get("status") or 0) in (3, 4):
            apps = _apps_from_task(task)
            need = []
            have = {
                qk.dotted_appno(f.get("application_no")): f
                for f in figs if isinstance(f, dict)
            }
            for a in apps:
                f = have.get(a) or {}
                if int(f.get("has_figure") or 0) != 1 or not f.get("rel_path"):
                    need.append(a)
            if need:
                figs = db.repair_figures_from_disk(cur, int(task["task_id"]), need)
        return task_response(task, figs), 200
