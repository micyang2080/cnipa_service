# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from service import config


def norm_rel_path(rel_path: Optional[str]) -> Optional[str]:
    """库内相对路径统一以 / 开头，如 /200610004173.6/figure.png。"""
    if rel_path is None:
        return None
    s = str(rel_path).replace("\\", "/").strip()
    if not s:
        return None
    return s if s.startswith("/") else "/" + s


@contextmanager
def conn(autocommit: bool = False):
    c = pymysql.connect(
        host=config.mysql_host(),
        port=config.mysql_port(),
        user=config.mysql_user(),
        password=config.mysql_password(),
        database=config.mysql_database(),
        charset="utf8mb4",
        autocommit=autocommit,
        cursorclass=DictCursor,
    )
    try:
        yield c
        if not autocommit:
            c.commit()
    except Exception:
        if not autocommit:
            c.rollback()
        raise
    finally:
        c.close()


def _loads(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8")
    if isinstance(v, str):
        return json.loads(v)
    return v


def row_to_task(row: dict) -> dict:
    if not row:
        return {}
    out = dict(row)
    out["query_json"] = _loads(out.get("query_json"))
    out["result_json"] = _loads(out.get("result_json"))
    return out


def find_active_by_query_key(cur, qk: str) -> Optional[dict]:
    cur.execute(
        "SELECT * FROM cnipa_task WHERE query_key=%s AND status IN (1,2,3,4) "
        "ORDER BY task_id DESC LIMIT 1 FOR UPDATE",
        (qk,),
    )
    row = cur.fetchone()
    return row_to_task(row) if row else None


def insert_task(cur, task_type: int, qk: str, query_json: dict) -> int:
    cur.execute(
        "INSERT INTO cnipa_task (task_type, query_key, query_json, status) "
        "VALUES (%s, %s, %s, 1)",
        (task_type, qk, json.dumps(query_json, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def get_task(cur, task_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM cnipa_task WHERE task_id=%s", (task_id,))
    row = cur.fetchone()
    return row_to_task(row) if row else None


def get_figures(cur, task_id: int) -> list:
    cur.execute(
        "SELECT application_no, has_figure, rel_path FROM cnipa_task_figure "
        "WHERE task_id=%s ORDER BY figure_id ASC",
        (task_id,),
    )
    rows = cur.fetchall() or []
    out = []
    for r in rows:
        out.append(
            {
                "application_no": r["application_no"],
                "has_figure": int(r["has_figure"]),
                "rel_path": norm_rel_path(r["rel_path"]),
            }
        )
    return out


def claim_next(cur) -> Optional[dict]:
    cur.execute(
        "SELECT * FROM cnipa_task WHERE status=1 ORDER BY task_id ASC LIMIT 1 FOR UPDATE"
    )
    row = cur.fetchone()
    if not row:
        return None
    tid = int(row["task_id"])
    cur.execute(
        "UPDATE cnipa_task SET status=2, started_at=%s, progress=%s "
        "WHERE task_id=%s AND status=1",
        (datetime.now(), "claimed", tid),
    )
    if cur.rowcount != 1:
        return None
    cur.execute("SELECT * FROM cnipa_task WHERE task_id=%s", (tid,))
    return row_to_task(cur.fetchone())


def set_progress(cur, task_id: int, progress: str):
    cur.execute(
        "UPDATE cnipa_task SET progress=%s WHERE task_id=%s AND status=2",
        (progress[:200], task_id),
    )


def finish_ok(cur, task_id: int, status: int, result: Any):
    cur.execute(
        "UPDATE cnipa_task SET status=%s, result_json=%s, progress=NULL, "
        "error_code=NULL, error_message=NULL, finished_at=%s "
        "WHERE task_id=%s AND status=2",
        (
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            datetime.now(),
            task_id,
        ),
    )


def finish_fail(cur, task_id: int, error_code: str, error_message: str):
    cur.execute(
        "UPDATE cnipa_task SET status=5, error_code=%s, error_message=%s, "
        "progress=NULL, finished_at=%s WHERE task_id=%s AND status IN (1,2)",
        ((error_code or "")[:64], (error_message or "")[:1000], datetime.now(), task_id),
    )


def cancel_task(cur, task_id: int) -> bool:
    cur.execute(
        "UPDATE cnipa_task SET status=5, error_code=%s, error_message=%s, "
        "progress=NULL, finished_at=%s WHERE task_id=%s AND status IN (1,2)",
        ("CANCELLED", "调用方取消", datetime.now(), task_id),
    )
    return int(cur.rowcount or 0) > 0


def is_running(cur, task_id: int) -> bool:
    cur.execute("SELECT status FROM cnipa_task WHERE task_id=%s", (task_id,))
    row = cur.fetchone()
    return bool(row) and int(row["status"]) == 2


def upsert_figure(cur, task_id: int, application_no: str, has_figure: int, rel_path: Optional[str]):
    cur.execute(
        "INSERT INTO cnipa_task_figure (task_id, application_no, has_figure, rel_path) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE has_figure=VALUES(has_figure), rel_path=VALUES(rel_path)",
        (task_id, application_no, has_figure, norm_rel_path(rel_path)),
    )


def repair_figures_from_disk(cur, task_id: int, application_nos: list) -> list:
    """盘上已有附图但库未入账时补写 rel_path，返回最新 figures。

    新名 {application_no}.png；兼容历史 figure.png。
    """
    from pathlib import Path

    root = Path(config.file_root())
    for raw in application_nos or []:
        app = str(raw or "").strip()
        if not app:
            continue
        # 规范化申请号（含点）
        try:
            from service import query_key as qk
            app = qk.dotted_appno(app) or app
        except Exception:
            pass
        hit = None
        for name in (f"{app}.png", "figure.png"):
            p = root / app / name
            if p.is_file() and p.stat().st_size > 0:
                hit = name
                break
        if not hit:
            continue
        rel = norm_rel_path(f"/{app}/{hit}")
        upsert_figure(cur, task_id, app, 1, rel)
    return get_figures(cur, task_id)


def expire_stale_running(cur, timeout_sec: int) -> int:
    cur.execute(
        "UPDATE cnipa_task SET status=5, error_code=%s, error_message=%s, "
        "finished_at=%s WHERE status=2 AND started_at IS NOT NULL "
        "AND TIMESTAMPDIFF(SECOND, started_at, %s) > %s",
        (
            "TASK_TIMEOUT",
            f"任务执行超过 {timeout_sec} 秒",
            datetime.now(),
            datetime.now(),
            timeout_sec,
        ),
    )
    return int(cur.rowcount or 0)


def find_latest_by_query_key(cur, qk: str) -> Optional[dict]:
    cur.execute(
        "SELECT * FROM cnipa_task WHERE query_key=%s ORDER BY task_id DESC LIMIT 1",
        (qk,),
    )
    row = cur.fetchone()
    return row_to_task(row) if row else None
