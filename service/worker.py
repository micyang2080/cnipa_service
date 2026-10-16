# -*- coding: utf-8 -*-
"""单 worker 串行消费队列（对齐 external 一机一任务；claim 用 status 1→2）。"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from service import config
from service import db
from service import query_key as qk
from service import runner_invoke

log = logging.getLogger("cnipa.worker")

RESULT_CAP = 10


def _py_import(name: str):
    import sys

    root = config.python_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    return __import__(name)


def _dev_skip(q: dict) -> frozenset:
    return config.dev_skip_stages(q.get("dev_skip_stages"))


def _search_cache(lib: str, filters: dict, max_items: int):
    return _py_import("cnipa_cache").load_search(lib, filters, max_items)


def _detail_cache(lib: str, application_no: str, case_no: str):
    return _py_import("cnipa_cache").load_detail(lib, application_no, case_no)


def _figure_write_name(application_no: str) -> str:
    """新落盘名：{申请号}.png（一申请号一图；多决定书共用，不按案号分文件）。"""
    return f"{application_no}.png"


def _figure_candidates(application_no: str) -> list:
    """盘上查找顺序：新名优先，兼容历史 figure.png。"""
    root = Path(config.file_root()) / application_no
    return [root / _figure_write_name(application_no), root / "figure.png"]


def _find_existing_figure(application_no: str) -> Optional[Path]:
    for p in _figure_candidates(application_no):
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def _rel_for_app(application_no: str) -> str:
    existing = _find_existing_figure(application_no)
    if existing is not None:
        try:
            rel = "/" + existing.relative_to(Path(config.file_root())).as_posix()
        except ValueError:
            rel = f"/{application_no}/{existing.name}"
        return db.norm_rel_path(rel) or rel
    name = _figure_write_name(application_no)
    return db.norm_rel_path(f"/{application_no}/{name}") or f"/{application_no}/{name}"


def _abs_for_app(application_no: str) -> str:
    """默认写入路径（新名）；已有旧 figure.png 时复用其路径避免双份。"""
    existing = _find_existing_figure(application_no)
    if existing is not None:
        return str(existing)
    return str(Path(config.file_root()) / application_no / _figure_write_name(application_no))


def _any_detail_needs_uia(library_type: str, items: list) -> bool:
    """任一条缺详情 JSON 或交付盘 PDF → 必须进页点卡片（不能仅靠检索文件缓存）。"""
    cache_mod = _py_import("cnipa_cache")
    for item in items:
        if not isinstance(item, dict):
            continue
        app_d = qk.dotted_appno(item.get("application_no"))
        case_d = str(item.get("case_no") or "").strip()
        if not app_d or not case_d:
            continue
        cache_mod.ensure_decision_pdf_on_file_root(app_d, case_d)
        if not cache_mod.detail_bundle_ready(library_type, app_d, case_d):
            return True
    return False


def _disk_pdf(application_no: str, case_no: str) -> Optional[Path]:
    app = qk.dotted_appno(application_no) or ""
    case = str(case_no or "").strip()
    if not app or not case:
        return None
    p = Path(config.file_root()) / app / f"{case}.pdf"
    try:
        if p.is_file() and p.stat().st_size > 0:
            return p
    except OSError:
        return None
    return None


def _ensure_parent(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _copy_figure_if_needed(src: str, dst: str) -> bool:
    if not src or not os.path.isfile(src):
        return False
    src_abs = os.path.abspath(src)
    dst_abs = os.path.abspath(dst)
    if src_abs == dst_abs:
        return True
    _ensure_parent(dst_abs)
    tmp = dst_abs + ".tmp"
    shutil.copy2(src_abs, tmp)
    os.replace(tmp, dst_abs)
    return True


def _status_snapshot() -> dict:
    ok, data, err = runner_invoke.check_status(timeout_sec=120)
    if data:
        return data
    return {
        "success": False,
        "ready": False,
        "message": err or "status check failed",
        "error": "ENV_CHECK_FAIL",
    }


def ensure_desktop_ready(cur, task_id: int) -> tuple[bool, str, str]:
    """type=1 执行前：优先切回已有 cpquery 标签；无 Chrome 才启动+登录。"""
    from service import chrome_bootstrap

    db.set_progress(cur, task_id, "env_check")
    st = _status_snapshot()
    if st.get("ready"):
        return True, "", ""

    # 已有浏览器但前台在 pss/其它 tab：只切标签，绝不为「标签不在前台」新开窗
    if st.get("chrome_found"):
        db.set_progress(cur, task_id, "activate_cpquery_tab")
        ok_sw, sw_detail = chrome_bootstrap.activate_cpquery_tab(25.0)
        log.info("activate_cpquery_tab ok=%s detail=%s", ok_sw, sw_detail)
        time.sleep(1.5)
        st = _status_snapshot()
        if st.get("ready"):
            return True, "", ""
        # 已切到/存在 cpquery 且状态显示已登录：交给检索自愈，勿再开 SSO
        if ok_sw and st.get("logged_in"):
            log.info("desktop: logged_in after tab switch, skip login")
            return True, "", ""
        if ok_sw and st.get("cpquery_usable") and st.get("a11y_ok"):
            log.info("desktop: cpquery guest session usable, skip SSO login")
            return True, "", ""
        # 有 Chrome 但未登录/查询页未就绪：必须走 auto_login（复用当前窗），
        # 不可再 return True——否则 search/detail 会在 SSO 登录页空转。
        log.info(
            "desktop: chrome present, will auto_login "
            "(ready=%s logged_in=%s usable=%s ok_sw=%s)",
            st.get("ready"), st.get("logged_in"), st.get("cpquery_usable"),
            ok_sw,
        )

    # 完全没有 Chrome 才启动专用 profile（无 --new-window 叠第二实例）
    if not st.get("chrome_found"):
        db.set_progress(cur, task_id, "launch_chrome")
        ok_launch, detail = chrome_bootstrap.launch_chrome_for_login()
        if not ok_launch:
            return False, "CHROME_LAUNCH_FAIL", detail
        if not chrome_bootstrap.wait_chrome_window(45.0):
            return False, "CHROME_LAUNCH_FAIL", "已启动 Chrome 但超时未见窗口"
        chrome_bootstrap.wait_login_edits(60.0)
        st = _status_snapshot()
        if st.get("ready"):
            return True, "", ""
        if st.get("cpquery_usable") and st.get("a11y_ok") and st.get("chrome_found"):
            log.info("desktop: cpquery usable without SSO, skip auto_login")
            return True, "", ""

    db.set_progress(cur, task_id, "auto_login")
    lok, ldata, lerr = runner_invoke.login({}, timeout_sec=300)
    if lok and ldata.get("success"):
        st2 = _status_snapshot()
        if st2.get("ready"):
            return True, "", ""
        # 登录成功但前台仍非就绪：再切一次 cpquery，并等内容通道
        chrome_bootstrap.activate_cpquery_tab(15.0)
        time.sleep(1.0)
        st3 = _status_snapshot()
        if st3.get("ready"):
            return True, "", ""
        # 已登录但 a11y 未就绪：多等几轮，避免空树直接进 search 点不了左侧菜单
        if st3.get("logged_in") or st3.get("chrome_found"):
            for _ in range(6):
                time.sleep(2.0)
                st4 = _status_snapshot()
                if st4.get("ready") or st4.get("a11y_ok"):
                    return True, "", ""
            # a11y 仍假但已登录：交 search 自愈（ensure_page_a11y + 点左侧菜单）
            if st3.get("logged_in") or st4.get("logged_in"):
                log.warning("desktop: logged_in but a11y_ok=false, proceed to search self-heal")
                return True, "", ""
        msg = st3.get("message") or st2.get("message") or "登录后环境仍未就绪"
        return False, "ENV_NOT_READY", str(msg)

    if (ldata or {}).get("error") == "NEED_CREDENTIALS":
        return False, "NEED_CREDENTIALS", str(
            ldata.get("message") or "服务机 account.ini 未配置国知局账密"
        )

    code = str((ldata or {}).get("error") or "LOGIN_FAIL")
    msg = str((ldata or {}).get("message") or lerr or st.get("message") or code)
    return False, code[:64], msg[:1000]


PSS_FIGURE_URL = "https://pss-system.cponline.cnipa.gov.cn/conventionalSearch"


def ensure_chrome_for_figure(cur, task_id: int) -> tuple[bool, str, str]:
    """type=2 只抓附图：只要有可交互 Chrome 即可，禁止进 cpquery/复审无效登录链。

    附图在 pss-system；登录墙由 fetch_abstract_figure runner 自处理。
    旧逻辑误调 ensure_desktop_ready → 重登 SSO、停在案件查询/复审无效，从不打开 PSS。
    昨留下的「常规检索」tab：有则切过去复用，不要停在最新的复审无效前台。
    """
    from service import chrome_bootstrap

    db.set_progress(cur, task_id, "env_check_figure")
    st = _status_snapshot()
    if st.get("chrome_found"):
        db.set_progress(cur, task_id, "activate_pss_tab")
        ok_sw, sw_detail = chrome_bootstrap.activate_pss_tab(20.0)
        log.info("figure env: chrome present, activate_pss_tab ok=%s detail=%s",
                 ok_sw, sw_detail)
        if not ok_sw:
            # 仅当完全没有 PSS 标签时才 CLI 开页，避免堆多个「常规检索」
            has_tab = "no_pss_tab" not in str(sw_detail)
            if not has_tab:
                db.set_progress(cur, task_id, "open_pss_url")
                chrome_bootstrap.open_chrome_url(PSS_FIGURE_URL)
                chrome_bootstrap.activate_pss_tab(15.0)
        return True, "", ""

    db.set_progress(cur, task_id, "launch_chrome_pss")
    ok_launch, detail = chrome_bootstrap.launch_chrome_for_login(url=PSS_FIGURE_URL)
    if not ok_launch:
        return False, "CHROME_LAUNCH_FAIL", detail
    if not chrome_bootstrap.wait_chrome_window(45.0):
        return False, "CHROME_LAUNCH_FAIL", "已启动 Chrome 但超时未见窗口"
    return True, "", ""


def _run_figure(application_no: str, force_refresh: bool) -> tuple[int, Optional[str], Optional[str], str]:
    """返回 (has_figure, rel_path, error_code, message)。"""
    app = qk.dotted_appno(application_no)
    if not app:
        return 0, None, "BAD_REQUEST", ""
    save_path = _abs_for_app(app)
    _ensure_parent(save_path)
    # 盘上已有图：直接入账（含 force_refresh；要重抓请先删 cnipa_file 下 png）
    if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
        return 1, _rel_for_app(app), None, ""
    existing = _find_existing_figure(app)
    if existing is not None:
        return 1, _rel_for_app(app), None, ""
    ok, data, err = runner_invoke.fetch_figure(
        {"application_no": app, "save_path": save_path, "force_refresh": force_refresh},
        timeout_sec=600,
    )
    if data.get("success") and data.get("figure_path"):
        fp = str(data["figure_path"])
        if _copy_figure_if_needed(fp, save_path) or os.path.isfile(save_path):
            return 1, _rel_for_app(app), None, ""
        return 0, None, "FIGURE_SAVE_FAIL", str(data.get("message") or "")
    # runner 已写入 save_path 但未 emit JSON（192908：另存成功无 payload）→ 仍算有图
    if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
        return 1, _rel_for_app(app), None, ""
    if not ok and not data:
        return 0, None, err or "RUNTIME_ERROR", str(err or "")
    code = str(data.get("error") or "")
    msg = str(data.get("message") or err or "")
    if code == "NO_FIGURE":
        return 0, None, None, msg
    return 0, None, code or err or "FIGURE_FAIL", msg


def _execute_type2(cur, task: dict):
    q = task.get("query_json") or {}
    app = qk.dotted_appno(q.get("application_no"))
    force = bool(q.get("force_refresh"))
    tid = int(task["task_id"])
    db.set_progress(cur, tid, f"figure:{app}")
    has_fig, rel, err, _fmsg = _run_figure(app, force)
    db.upsert_figure(cur, tid, app, has_fig, rel)
    result = {"success": True, "application_no": app}
    if has_fig == 1:
        db.finish_ok(cur, tid, 3, result)
        return
    if err in (None, "", "NO_FIGURE"):
        db.finish_ok(cur, tid, 4, result)
        return
    code = str(err or "FIGURE_FAIL")
    db.finish_fail(cur, tid, code[:64], code)


def _execute_type1(cur, task: dict):
    q = task.get("query_json") or {}
    tid = int(task["task_id"])
    force = bool(q.get("force_refresh"))
    skip = _dev_skip(q)
    dev_sk_list = sorted(skip)
    try:
        max_items = min(max(1, int(q.get("max_items") or RESULT_CAP)), RESULT_CAP)
    except (TypeError, ValueError):
        max_items = RESULT_CAP
    library_type = q.get("library_type") or "invalid"
    filters = q.get("filters") or {}

    if config.dev_only_figure():
        # 等同「第2步已跑完」：不跑 search/detail，直接接全流程末尾的附图段
        app = qk.dotted_appno((filters or {}).get("application_no") or q.get("application_no"))
        if not app:
            db.finish_fail(cur, tid, "BAD_REQUEST", "dev.only_figure 需要 filters.application_no")
            return
        from service import chrome_bootstrap
        db.set_progress(cur, tid, "activate_pss_tab")
        chrome_bootstrap.activate_pss_tab(15.0)
        db.set_progress(cur, tid, f"figure:1/1:{app}")
        has_fig, rel, ferr, _ = _run_figure(app, force)
        db.upsert_figure(cur, tid, app, has_fig, rel)
        if has_fig == 1:
            db.finish_ok(cur, tid, 3, {"success": True, "application_no": app, "figure_path": rel})
        elif ferr in (None, "", "NO_FIGURE"):
            db.finish_ok(cur, tid, 4, {"success": True, "application_no": app})
        else:
            db.finish_fail(cur, tid, str(ferr or "FIGURE_FAIL")[:64], str(ferr or ""))
        return

    search_payload = {
        "library_type": library_type,
        "filters": filters,
        "max_items": max_items,
        "page": q.get("page") or 1,
        "force_refresh": force,
        "dev_skip_stages": dev_sk_list,
    }
    ok, data, err = False, {}, ""
    if "search" in skip:
        cached = _search_cache(library_type, filters, max_items)
        if cached:
            ok, data, err = True, cached, ""
            db.set_progress(cur, tid, "search:cache")
            log.info("dev skip search, cache hit")
        else:
            db.finish_fail(cur, tid, "DEV_CACHE_MISS", "dev 跳过 search 但无检索缓存")
            return
    else:
        db.set_progress(cur, tid, "search")
        ok, data, err = runner_invoke.search_decisions(search_payload, timeout_sec=900)
    if ok and data.get("error") == "NOT_LOGGED_IN":
        db.set_progress(cur, tid, "auto_login_retry")
        lok, ldata, _ = runner_invoke.login({}, timeout_sec=300)
        if lok and ldata.get("success"):
            ok, data, err = runner_invoke.search_decisions(search_payload, timeout_sec=900)
        elif (ldata or {}).get("error") == "NEED_CREDENTIALS":
            db.finish_fail(cur, tid, "NEED_CREDENTIALS", str(ldata.get("message") or ""))
            return
    if not ok and not data:
        db.finish_fail(cur, tid, "RUNTIME_ERROR", err or "search failed")
        return
    if not data.get("success"):
        code = str(data.get("error") or "SEARCH_FAIL")
        msg = str(data.get("message") or err or code)
        db.finish_fail(cur, tid, code[:64], msg)
        return

    items = list(data.get("items") or [])
    library_type = data.get("library_type") or library_type
    total = data.get("total")
    truncated = bool(data.get("truncated"))
    if len(items) > RESULT_CAP:
        items = items[:RESULT_CAP]
        truncated = True
    if total is not None:
        try:
            if int(total) > len(items):
                truncated = True
        except (TypeError, ValueError):
            pass

    if not items:
        result = {
            "success": True,
            "library_type": library_type,
            "total": total if total is not None else 0,
            "truncated": truncated,
            "items": [],
            "details": [],
        }
        db.finish_ok(cur, tid, 4, result)
        return

    # 检索 JSON 缓存命中不会动浏览器；若仍缺 PDF/详情，必须重跑检索 UIA 以切库+出结果页
    if data.get("from_cache") and not force and _any_detail_needs_uia(library_type, items):
        log.info(
            "search file cache hit but detail/pdf incomplete; re-run search UIA "
            "task_id=%s items=%s", tid, len(items))
        db.set_progress(cur, tid, "search:uia_for_detail")
        uia_payload = dict(search_payload)
        uia_payload["force_refresh"] = True
        ok, data2, err = runner_invoke.search_decisions(uia_payload, timeout_sec=900)
        if ok and data2.get("success"):
            data = data2
            items = list(data.get("items") or [])
            library_type = data.get("library_type") or library_type
            total = data.get("total")
            truncated = bool(data.get("truncated"))
            if len(items) > RESULT_CAP:
                items = items[:RESULT_CAP]
                truncated = True
        else:
            log.warning(
                "search UIA retry after cache failed task_id=%s err=%s",
                tid, err or data2.get("error"))

    details = []
    for i, item in enumerate(items):
        with db.conn(autocommit=True) as c2:
            if not db.is_running(c2.cursor(), tid):
                return
        db.set_progress(cur, tid, f"detail:{i + 1}/{len(items)}")
        app_d = qk.dotted_appno(item.get("application_no"))
        case_d = str(item.get("case_no") or "").strip()
        if "detail" in skip:
            cached_d = _detail_cache(library_type, app_d, case_d) if app_d and case_d else None
            if cached_d and isinstance(cached_d.get("detail"), dict):
                details.append(cached_d["detail"])
                continue
            details.append(item if isinstance(item, dict) else {})
            continue
        # 详情 JSON + PDF 齐 → 跳过进页（含 force_refresh：盘上已有则不点详情）
        if app_d and case_d:
            cache_mod = _py_import("cnipa_cache")
            cache_mod.ensure_decision_pdf_on_file_root(app_d, case_d)
            if cache_mod.detail_bundle_ready(library_type, app_d, case_d):
                cached_d = _detail_cache(library_type, app_d, case_d)
                if cached_d and isinstance(cached_d.get("detail"), dict):
                    det = dict(cached_d["detail"])
                    if _disk_pdf(app_d, case_d):
                        if not str(det.get("decision_file_url") or "").strip():
                            try:
                                cc = _py_import("cnipa_contract")
                                det["decision_file_url"] = cc.build_decision_file_url(
                                    app_d, case_d)
                            except Exception:
                                pass
                    else:
                        det.pop("decision_file_url", None)
                        log.warning(
                            "detail cache without delivery pdf, re-fetch UIA "
                            "app=%s case=%s", app_d, case_d)
                        cached_d = None
                    if cached_d is not None:
                        details.append(det)
                        db.set_progress(
                            cur, tid, f"detail:{i + 1}/{len(items)}:cache")
                        log.info(
                            "detail bundle hit skip UIA app=%s case=%s force=%s",
                            app_d, case_d, force)
                        continue
            elif _disk_pdf(app_d, case_d) or cache_mod.pdf_available(app_d, case_d):
                log.info(
                    "pdf without detail cache, enter detail UIA app=%s case=%s",
                    app_d, case_d)
        detail_payload = {
            "list_item": item,
            "list_index": i,
            "application_no": item.get("application_no"),
            "case_no": item.get("case_no"),
            "library_type": library_type,
            "detail_url": item.get("detail_url") or "",
            "force_refresh": force,
            "dev_skip_stages": dev_sk_list,
        }
        dok, ddata, derr = runner_invoke.fetch_detail(
            detail_payload, timeout_sec=config.detail_runner_timeout_sec())
        if derr == "CANCELLED" or (isinstance(ddata, dict) and ddata.get("error") == "CANCELLED"):
            return
        if dok and ddata.get("success") and isinstance(ddata.get("detail"), dict):
            details.append(ddata["detail"])
        elif dok and ddata.get("success") is False:
            # 单条详情失败：占位空 detail，不整单失败
            details.append(item if isinstance(item, dict) else {})
        else:
            details.append(item if isinstance(item, dict) else {})

    seen_apps = []
    for row in list(details) + list(items):
        app = qk.dotted_appno(row.get("application_no") if isinstance(row, dict) else "")
        if app and app not in seen_apps:
            seen_apps.append(app)

    fig_by_app: dict[str, Optional[str]] = {}
    # 登录/凭证类失败对后续申请号同样无解 → 不再反复开 PSS 重登
    _FIG_ABORT = frozenset({
        "NEED_CREDENTIALS", "CAPTCHA_FAIL", "AUTH_REJECTED", "NOT_LOGGED_IN",
        "TASK_TIMEOUT", "TIMEOUT", "RUNTIME_FAIL", "FIGURE_FAIL",
    })
    fig_abort = False
    need_fetch: list[str] = []
    if "figure" in skip:
        db.set_progress(cur, tid, "figure:skipped")
        seen_apps = []
    else:
        # 交付盘已有 {app}/{app}.png → 直接入账（含 force_refresh；重抓请先删文件）
        for app in seen_apps:
            if _find_existing_figure(app) is not None:
                rel = _rel_for_app(app)
                db.upsert_figure(cur, tid, app, 1, rel)
                fig_by_app[app] = rel
                log.info(
                    "figure disk_reuse skip pss app=%s path=%s force=%s",
                    app, rel, force)
            else:
                need_fetch.append(app)
        if need_fetch:
            from service import chrome_bootstrap

            db.set_progress(cur, tid, "activate_pss_tab")
            chrome_bootstrap.activate_pss_tab(15.0)
    for j, app in enumerate(need_fetch):
        db.set_progress(cur, tid, f"figure:{j + 1}/{len(need_fetch)}:{app}")
        if fig_abort:
            db.upsert_figure(cur, tid, app, 0, None)
            fig_by_app[app] = None
            continue
        has_fig, rel, ferr, fmsg = _run_figure(app, force)
        db.upsert_figure(cur, tid, app, has_fig, rel)
        fig_by_app[app] = rel if has_fig == 1 and rel else None
        blob = f"{ferr or ''} {fmsg or ''}"
        if ferr in _FIG_ABORT or any(
                k in blob for k in ("登录墙", "登录表单", "自动登录失败",
                                    "account.ini", "滑块验证", "页态不明",
                                    "无法键入账密")):
            fig_abort = True
            log.warning("figure abort remaining apps after %s err=%s", app, blob[:120])

    # decision_date：列表无 → 从详情回填；figure_path：一申请号一图（多决定书共用）
    def _ck(row: dict) -> tuple:
        return (
            qk.dotted_appno(row.get("application_no")) or "",
            str(row.get("case_no") or "").strip().upper(),
        )

    def _fig_path(row: dict) -> str:
        app = qk.dotted_appno(row.get("application_no")) or ""
        return fig_by_app.get(app) or ""

    detail_map = {_ck(d): d for d in details if isinstance(d, dict)}
    for d in details:
        if isinstance(d, dict):
            d["figure_path"] = _fig_path(d)

    clean_items = []
    for item in items:
        if not isinstance(item, dict):
            clean_items.append(item)
            continue
        it = dict(item)
        it["figure_path"] = _fig_path(it)
        det = detail_map.get(_ck(it))
        if det:
            dd = str(det.get("decision_date") or "").strip()
            if dd:
                it["decision_date"] = dd
            dfu = str(det.get("decision_file_url") or "").strip()
            if dfu:
                it["decision_file_url"] = dfu
            if not str(det.get("issue_date") or "").strip() and str(it.get("issue_date") or "").strip():
                det["issue_date"] = it["issue_date"]
        clean_items.append(it)

    result = {
        "success": True,
        "library_type": library_type,
        "total": total if total is not None else len(clean_items),
        "truncated": truncated,
        "items": clean_items,
        "details": details,
    }
    db.finish_ok(cur, tid, 3, result)


def execute_task(task: dict):
    tid = int(task["task_id"])
    ttype = int(task.get("task_type") or 0)
    try:
        # 长任务必须 autocommit：否则 search/detail/figure 全程占同一事务可达数十分钟
        with db.conn(autocommit=True) as c:
            cur = c.cursor()
            if ttype == 2:
                ok, code, msg = ensure_chrome_for_figure(cur, tid)
                if not ok:
                    db.finish_fail(cur, tid, code, msg)
                    return
                _execute_type2(cur, task)
            elif ttype == 1:
                # only_figure 只测附图：前台常是 PSS，勿走 ensure_desktop_ready
                # （会判未就绪→auto_login 开 SSO→掉 tysf 门户→用户名回读失败）
                if config.dev_only_figure():
                    ok, code, msg = ensure_chrome_for_figure(cur, tid)
                else:
                    ok, code, msg = ensure_desktop_ready(cur, tid)
                if not ok:
                    db.finish_fail(cur, tid, code, msg)
                    return
                _execute_type1(cur, task)
            else:
                db.finish_fail(cur, tid, "BAD_TASK_TYPE", f"unknown task_type={ttype}")
    except Exception as e:
        log.exception("task %s failed", tid)
        try:
            with db.conn(autocommit=True) as c:
                db.finish_fail(c.cursor(), tid, "RUNTIME_ERROR", str(e)[:1000])
        except Exception:
            log.error("finish_fail also failed: %s", traceback.format_exc())


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="cnipa-worker")
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        log.info("worker started")
        while not self._stop.is_set():
            try:
                with db.conn() as c:
                    cur = c.cursor()
                    db.expire_stale_running(cur, config.task_timeout_sec())
                    task = db.claim_next(cur)
                if task:
                    log.info("claimed task_id=%s type=%s", task.get("task_id"), task.get("task_type"))
                    execute_task(task)
                else:
                    self._stop.wait(config.poll_interval_sec())
            except Exception:
                log.exception("worker loop error")
                self._stop.wait(config.poll_interval_sec())
        log.info("worker stopped")


_worker: Optional[Worker] = None


def start_worker():
    global _worker
    if _worker and _worker.is_alive():
        return _worker
    _worker = Worker()
    _worker.start()
    return _worker
