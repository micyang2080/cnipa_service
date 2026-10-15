# -*- coding: utf-8 -*-
"""无人值守：必要时启动普通 Chrome 并打开国知局 SSO 入口（不改 UIA runner）。"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from service import config

log = logging.getLogger("cnipa.chrome")

SSO_URL = "https://sso.cponline.cnipa.gov.cn"
CPQUERY_INDEX = "https://cpquery.cponline.cnipa.gov.cn/chinesepatent/index"


def sso_login_url() -> str:
    return f"{SSO_URL}/login?service={quote(CPQUERY_INDEX, safe='')}"


def _chrome_exe() -> Optional[str]:
    configured = (
        os.environ.get("CNIPA_CHROME_EXE")
        or config._get("chrome", "exe", "")
    ).strip()
    if configured and Path(configured).is_file():
        return configured
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def _user_data_dir() -> str:
    return (
        os.environ.get("CNIPA_CHROME_USER_DATA_DIR")
        or config._get("chrome", "user_data_dir", str(config.repo_root() / "chrome_profile"))
    ).strip()


def _uia_mod():
    import sys

    py = Path(config.python_root())
    if str(py) not in sys.path:
        sys.path.insert(0, str(py))
    import cnipa_uia as uia  # noqa: WPS433

    return uia


def chrome_already_running() -> bool:
    try:
        return bool(_uia_mod().find_any_chrome())
    except Exception:
        return False


def open_chrome_url(url: str) -> tuple[bool, str]:
    """同 user-data-dir 已有 Chrome 时只附加标签，禁止 --new-window 再开一扇窗。"""
    ok, how = _uia_mod().open_url_via_chrome(url)
    if ok:
        time.sleep(2.5)
    return ok, how


_PDF_PRINTER_IDS = ("Save as PDF", "另存为 PDF")


def ensure_print_destination_save_as_pdf(profile: Optional[str] = None) -> str:
    """写入专用 profile 的 Preferences：打印默认目标=Save as PDF。

    须在 Chrome 未占用该 profile 时调用（本函数在 launch 前执行）。
    桌面快捷方式用的是系统 Default 用户目录，本服务用 chrome_profile，
    故不会继承桌面里「默认另存为 PDF」的习惯。
    """
    import json

    root = Path(profile or _user_data_dir())
    prefs_path = root / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if prefs_path.is_file():
        try:
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("read Preferences fail: %s", e)
            data = {}

    # 界面中文（否则 PDF 工具条/打印侧栏常是 Automatic Zoom / Save）
    intl = data.setdefault("intl", {})
    intl["app_locale"] = "zh-CN"
    intl["selected_languages"] = "zh-CN,zh,en-US,en"
    intl["accept_languages"] = "zh-CN,zh,en-US,en"

    translate = data.setdefault("translate", {})
    translate["enabled"] = False

    printing = data.setdefault("printing", {})
    sticky = printing.setdefault("print_preview_sticky_settings", {})
    app_raw = sticky.get("appState") or "{}"
    try:
        app = json.loads(app_raw) if isinstance(app_raw, str) else dict(app_raw)
    except Exception:
        app = {}
    if not isinstance(app, dict):
        app = {}

    recent = [
        x for x in (app.get("recentDestinations") or [])
        if isinstance(x, dict) and x.get("id") not in _PDF_PRINTER_IDS
    ]
    # 已有 PDF 条目保留 capabilities 等字段，提到最前
    kept_pdf = []
    for pid in _PDF_PRINTER_IDS:
        for x in (app.get("recentDestinations") or []):
            if isinstance(x, dict) and x.get("id") == pid:
                kept_pdf.append(x)
                break
        else:
            kept_pdf.append({"id": pid, "origin": "local", "account": ""})
    app["version"] = int(app.get("version") or 2)
    app["recentDestinations"] = kept_pdf + recent
    app["selectedDestinationId"] = _PDF_PRINTER_IDS[0]
    sticky["appState"] = json.dumps(app, ensure_ascii=False, separators=(",", ":"))

    # 企业规则：优先匹配 Save as PDF（部分版本认这个）
    printing["default_destination_selection_rules"] = json.dumps(
        {"kind": "local", "idPattern": ".*(Save as PDF|另存为 PDF).*"},
        ensure_ascii=False,
    )

    try:
        prefs_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("write Preferences fail: %s", e)
        return f"fail:{e}"
    log.info("print dest sticky → Save as PDF (%s)", prefs_path)
    return str(prefs_path)


def launch_chrome_for_login(url: Optional[str] = None) -> tuple[bool, str]:
    """启动 Chrome 打开 SSO/查询入口。返回 (ok, detail)。

    桌面已有任意 Chrome（含上次查询留下的 cpquery）→ 只同 profile 开标签，
    禁止 --new-window，避免再弹一扇独立窗（229 实拍：背后 cpquery、前台 SSO）。
    """
    target = url or sso_login_url()
    if chrome_already_running():
        ok, how = open_chrome_url(target)
        if ok:
            log.info("reuse chrome instance, open tab url=%s how=%s", target, how)
            return True, f"tab:{how}"
        return False, f"已有 Chrome 但无法附加标签: {how}"
    exe = _chrome_exe()
    if not exe:
        return False, "未找到 chrome.exe，请在 config.ini [chrome] exe 指定路径"
    profile = _user_data_dir()
    Path(profile).mkdir(parents=True, exist_ok=True)
    ensure_print_destination_save_as_pdf(profile)
    args = [
        exe,
        f"--user-data-dir={profile}",
        "--force-renderer-accessibility",
        "--lang=zh-CN",
        "--disable-translate",
        "--start-maximized",
        target,
    ]
    # 脱离服务进程 Job/控制台，避免 UIA 子进程结束或误杀时连 Chrome 一起退
    flags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"):
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(exe).parent),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as e:
        return False, f"启动 Chrome 失败: {e}"
    log.info("launched chrome url=%s profile=%s", target, profile)
    return True, f"launched:{exe}"


def wait_chrome_window(timeout_sec: float = 45.0) -> bool:
    """轮询直至出现任意 chrome 窗，并尽量最大化。"""
    import sys

    py = Path(config.python_root())
    if str(py) not in sys.path:
        sys.path.insert(0, str(py))
    import cnipa_uia as uia  # noqa: WPS433

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            wins = uia.find_any_chrome()
            if wins:
                for w, info in wins:
                    if info.get("minimized"):
                        continue
                    try:
                        w.set_focus()
                        w.maximize()
                    except Exception:
                        try:
                            w.maximize()
                        except Exception:
                            pass
                    break
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def wait_login_edits(timeout_sec: float = 60.0) -> bool:
    """等登录页账号/密码 Edit 进入 UIA 树（至少 2 个非地址栏 Edit）。"""
    import sys

    py = Path(config.python_root())
    if str(py) not in sys.path:
        sys.path.insert(0, str(py))
    import cnipa_uia as uia  # noqa: WPS433

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            for w, info in uia.find_any_chrome():
                if info.get("minimized"):
                    continue
                try:
                    w.set_focus()
                except Exception:
                    pass
                edits = []
                for el in uia.descendants(w, control_type="Edit"):
                    n = (uia._name(el) or "").strip()
                    r = uia._rect(el)
                    if r is None or (r[2] - r[0]) < 60:
                        continue
                    if "地址" in n:
                        continue
                    edits.append(n)
                if len(edits) >= 2:
                    log.info("login edits ready: %s", edits[:4])
                    return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def activate_cpquery_tab(timeout_sec: float = 20.0) -> tuple[bool, str]:
    """前台若在 pss 等其它标签：点浏览器标签条切回 cpquery，禁止为此新开 Chrome。

    Chrome 窗口标题=当前前台标签；后台 cpquery 标签仍在同一窗口 TabItem 里，
    但 check_status 只认标题 → 会误判「查询页不存在」并 launch 新浏览器。
    """
    import sys

    py = Path(config.python_root())
    if str(py) not in sys.path:
        sys.path.insert(0, str(py))
    import cnipa_uia as uia  # noqa: WPS433

    marks = (uia.WINDOW_TITLE_KEYWORD, "专利审查信息查询", "中国及多国专利")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            for w, info in uia.find_any_chrome():
                if info.get("minimized"):
                    continue
                title = info.get("title") or ""
                if any(m in title for m in marks):
                    try:
                        w.set_focus()
                    except Exception:
                        pass
                    return True, f"already_front:{title[:40]}"
                try:
                    w.set_focus()
                except Exception:
                    pass
                time.sleep(0.3)
                cands = []
                for t in uia.descendants(w, control_type="TabItem"):
                    n = (uia._name(t) or "").strip()
                    r = uia._rect(t)
                    if not n or not r or r[1] >= 80:
                        continue
                    if any(m in n for m in marks):
                        cands.append((n, t))
                if not cands:
                    continue
                n, t = cands[0]
                log.info("activate cpquery tab: %s", n[:60])
                uia.safe_click(w, t, settle=1.5)
                time.sleep(0.3)
                title2 = ""
                try:
                    title2 = w.window_text() or ""
                except Exception:
                    pass
                if any(m in title2 for m in marks):
                    return True, f"switched:{n[:40]}"
                return True, f"clicked:{n[:40]}"
        except Exception as e:
            log.warning("activate_cpquery_tab: %s", e)
        time.sleep(1.0)
    return False, "no_cpquery_tab"


def activate_pss_tab(timeout_sec: float = 20.0) -> tuple[bool, str]:
    """切到已有 PSS「常规检索」标签（昨附图留下的 tab），勿停在最新 cpquery/复审无效。

    优先点「常规检索」；没有再试「检索结果」/标题含专利检索。
    """
    import sys

    py = Path(config.python_root())
    if str(py) not in sys.path:
        sys.path.insert(0, str(py))
    import cnipa_uia as uia  # noqa: WPS433

    front_ok = ("常规检索", "专利检索及分析")
    # 点标签优先级：先填号页，再结果页
    tab_prefer = ("常规检索", "检索结果", "专利检索及分析")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            for w, info in uia.find_any_chrome():
                if info.get("minimized"):
                    continue
                title = info.get("title") or ""
                # 空白 Initpage 不算可用 PSS（勿当成功，交给 figure 强制进常规检索）
                if "initpage" in title.lower() or (
                        "pss-system" in title.lower() and "http" in title.lower()
                        and "conventional" not in title.lower()):
                    log.warning("activate_pss_tab: skip blank Initpage title=%s",
                                title[:60])
                    return False, f"blank_initpage:{title[:40]}"
                if any(m in title for m in front_ok):
                    try:
                        w.set_focus()
                    except Exception:
                        pass
                    return True, f"already_front:{title[:40]}"
                try:
                    w.set_focus()
                except Exception:
                    pass
                time.sleep(0.3)
                cands = []
                for t in uia.descendants(w, control_type="TabItem"):
                    n = (uia._name(t) or "").strip()
                    r = uia._rect(t)
                    if not n or not r or r[1] >= 80:
                        continue
                    if "initpage" in n.lower():
                        continue
                    for pri, mark in enumerate(tab_prefer):
                        if mark in n:
                            cands.append((pri, n, t))
                            break
                if not cands:
                    continue
                cands.sort(key=lambda x: x[0])
                _pri, n, t = cands[0]
                log.info("activate pss tab: %s", n[:60])
                uia.safe_click(w, t, settle=1.5)
                time.sleep(0.3)
                title2 = ""
                try:
                    title2 = w.window_text() or ""
                except Exception:
                    pass
                if "initpage" in title2.lower():
                    return False, f"landed_initpage:{n[:40]}"
                if any(m in title2 for m in front_ok) or "检索结果" in title2:
                    return True, f"switched:{n[:40]}"
                return True, f"clicked:{n[:40]}"
        except Exception as e:
            log.warning("activate_pss_tab: %s", e)
        time.sleep(1.0)
    return False, "no_pss_tab"
