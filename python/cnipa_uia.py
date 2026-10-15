# -*- coding: utf-8 -*-
"""cnipa UIA 动作层：OS 级真实键鼠驱动用户日常 Chrome（免 CDP 架构）。

背景（2026-09-01 定案，见 docs/联调RUNBOOK.md）：
  - 瑞数对一切 CDP 形态（Playwright 自带 Chromium / 接管真实 Chrome）都拒绝
    （HTTP 400 假签），去掉调试口立即恢复 → Playwright 对此站出局；
  - 本模块用 pywinauto(UIA) 定位控件 + SendInput 真实键鼠操作（isTrusted），
    请求由浏览器正常发出，页面 JS / 瑞数无从感知；
  - 前置条件：Chrome 以 --force-renderer-accessibility 启动（普通启动参数、
    非调试口、页面不可见），无障碍树才会暴露网页内容
    （probe 实测：output/uia_probe/cpquery_tree_20260901_162941.txt）。

树结构要点（来自 20260901_162941 probe）：
  - 顶层：Chrome_WidgetWin_1，标题含「中国及多国专利审查信息查询」；
  - Document name='中国及多国专利审查信息查询' 下才是网页内容；
  - 表单标签是 Text 节点（如 '申请号/专利号：'），输入框是无名 Edit，
    靠「同行右侧最近」的几何关系配对；
  - 按钮/菜单项有名字：Button '查询'、Button '重置'、MenuItem '展开更多搜索项'；
  - 总数：Text '共查询到 ' + Edit(数字) + Text ' 条结果'；分页 Text '共N条'；
  - 登录态：MenuItem '欢迎您，…'。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

WINDOW_TITLE_KEYWORD = os.environ.get('CNIPA_UIA_TITLE', '中国及多国专利审查信息查询')


# ---------------------------------------------------------------------------
# 窗口定位（与 C:\git\Mcps\cnipa-mcp\python\cnipa_uia.py 对齐）
# ---------------------------------------------------------------------------

# 顶栏 Tab 名常被截断，除全称外保留短标记
CPQUERY_TAB_MARKS = (
    WINDOW_TITLE_KEYWORD,
    '专利审查信息查询',
    '中国及多国专利',
)


def proc_image_path(pid) -> str:
    """pid → 进程可执行文件全路径（纯 ctypes，免 psutil/pywin32）。

    2026-09-01 21:39 实证：Chrome_WidgetWin_1 类名 Electron 宿主
    （GeniusWork）/Edge 全部命中，键鼠会打进宿主窗口——必须进程名复核。
    """
    import ctypes
    import ctypes.wintypes
    try:
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf,
                                                  ctypes.byref(size)):
                return ''
            return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ''


def _is_chrome_exe(w) -> bool:
    exe = proc_image_path(w.process_id())
    return os.path.normcase(os.path.basename(exe)) == 'chrome.exe'


def _window_minimized(w) -> bool:
    r = _rect(w)
    return r is None or r[0] <= -32000 or r[1] <= -32000


def enum_top_hwnds(*, class_name: str = None,
                   class_exact: str = None) -> list:
    """Win32 EnumWindows → [(hwnd, cls, title), ...]。

    **禁止** pywinauto Desktop.windows()（UIA 全桌面枚举，另存路径曾卡 30～47s/次）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        cls = cbuf.value or ''
        if class_exact is not None and cls != class_exact:
            return True
        if class_name is not None and class_name not in cls:
            return True
        tbuf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, tbuf, 512)
        out.append((int(hwnd), cls, tbuf.value or ''))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def uia_window_from_hwnd(hwnd):
    """hwnd → UIA WindowSpecification；失败 None。"""
    if not hwnd:
        return None
    try:
        from pywinauto import Desktop
        return Desktop(backend='uia').window(handle=int(hwnd))
    except Exception:
        return None


def find_cpquery_strip_tabs(w, keyword: str = None):
    """浏览器顶栏 TabItem（y<80），名含 keyword/短标记。不含页内模块导航 Tab。"""
    marks = CPQUERY_TAB_MARKS
    if keyword and keyword not in marks:
        marks = (keyword,) + CPQUERY_TAB_MARKS
    out = []
    seen = set()
    try:
        tabs = descendants(w, control_type='TabItem')
    except Exception:
        return out
    for t in tabs:
        n, r = _name(t), _rect(t)
        if not n or not r or r[1] >= 80:
            continue
        if not any(m in n for m in marks):
            continue
        key = (n, tuple(r))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def find_chrome_windows(keyword: str = None):
    """收集 chrome.exe 顶层窗口：窗口标题或任一顶栏 Tab 名含 keyword。

    2026-09-01 18:56 实证（run_20260901_185633）：单窗口版只回 z 序第一个，
    可能是最小化的详情窗（chrome 按钮 rect=-31992，树中 0 个 TabItem，
    标签条不枚举）——模块结果页可能在另一个窗口里，必须逐个试。
    """
    keyword = keyword or WINDOW_TITLE_KEYWORD
    from pywinauto import Desktop
    out = []
    for w in Desktop(backend='uia').windows(class_name_re='Chrome_WidgetWin_1'):
        try:
            if not _is_chrome_exe(w):
                continue
            title = w.window_text() or ''
            if keyword in title or any(m in title for m in CPQUERY_TAB_MARKS):
                out.append(w)
                continue
            if _window_minimized(w):
                try:
                    w.set_focus()
                    time.sleep(0.4)
                except Exception:
                    pass
            if find_cpquery_strip_tabs(w, keyword):
                out.append(w)
        except Exception:
            continue
    return out


def find_chrome_window(keyword: str = None):
    """按标题或顶栏 Tab 找 Chrome 顶层窗口（未找到返回 None，不抛异常）。"""
    wins = find_chrome_windows(keyword)
    return wins[0] if wins else None


def find_any_chrome():
    """不带标题关键字收集 Chrome 顶层窗口（05 pss 用：目标标签可能尚未打开，
    无法按 cpquery 标题定位）。返回 (窗口, 说明) 列表；前台/非最小化优先。

    两道坑都防：①最小化窗 rect=-31992 不可交互，排后；②类名
    Chrome_WidgetWin_1 会被 Electron 宿主（GeniusWork 自身）/Edge 冒充，
    须再核进程名 chrome.exe（2026-09-01 21:39 首跑实证：8 个同类名窗，
    排第 1 的是 GeniusWork，Ctrl+T 全打进宿主）。说明 dict 含 'exe'。
    """
    from pywinauto import Desktop
    out = []
    for w in Desktop(backend='uia').windows(class_name_re='Chrome_WidgetWin_1'):
        try:
            r = _rect(w)
            if r is None:
                continue
            exe = proc_image_path(w.process_id())
            if os.path.normcase(os.path.basename(exe)) != 'chrome.exe':
                continue
            minimized = r[0] <= -32000 or r[1] <= -32000
            out.append((w, {'title': w.window_text() or '', 'rect': r,
                            'minimized': minimized, 'exe': exe}))
        except Exception:
            continue
    out.sort(key=lambda it: it[1]['minimized'])       # 可交互的排前面
    return out


def cpquery_title_ok(title: str) -> bool:
    return any(m in (title or '') for m in CPQUERY_TAB_MARKS)


def ensure_cpquery_front_tab(win, step=None, keyword: str = None) -> tuple:
    """前台已是 cpquery 标题 → 成功；顶栏仅 1 个匹配 Tab → 点切；多个 → 不点（交 search 轮询）。"""
    keyword = keyword or WINDOW_TITLE_KEYWORD
    try:
        win.set_focus()
    except Exception:
        pass
    title = ''
    try:
        title = win.window_text() or ''
    except Exception:
        pass
    if cpquery_title_ok(title):
        return True, 'already_front'
    tabs = find_cpquery_strip_tabs(win, keyword)
    if len(tabs) == 1:
        click(win, tabs[0], settle=0.5)
        time.sleep(0.4)
        return True, 'switched_single'
    if len(tabs) > 1:
        if step:
            step('ensure_cpquery_tab', multi=len(tabs), note='不自动点')
        return False, f'multi_tab:{len(tabs)}'
    return False, 'no_strip_tab'


def ensure_cpquery_results_front(win, step=None, max_tabs: int = 3) -> tuple:
    """结果页（共查询到）在前台；多 cpquery 标签时逐个试，不盲点第一个。"""
    if '共查询到' in ' '.join(all_texts(win)):
        return True, 'already_results'
    ok, detail = ensure_cpquery_front_tab(win, step=step)
    if ok and '共查询到' in ' '.join(all_texts(win)):
        return True, detail
    tabs = find_cpquery_strip_tabs(win)
    for i, t in enumerate(tabs[:max_tabs]):
        click(win, t, settle=0.5)
        time.sleep(0.8)
        if '共查询到' in ' '.join(all_texts(win)):
            if step:
                step('ensure_results_tab', strip=i + 1, ok=True)
            return True, f'strip_{i + 1}'
    if step:
        step('ensure_results_tab', ok=False, tried=min(len(tabs), max_tabs))
    return False, 'no_results_tab'


def require_window(keyword: str = None):
    """定位含 cpquery 标签的 Chrome；错 tab 时仅唯一匹配才自动点切（同 MCP）。"""
    keyword = keyword or WINDOW_TITLE_KEYWORD
    win = find_chrome_window(keyword)
    if win is None:
        raise RuntimeError(
            f'未找到含 {keyword!r} 的 Chrome 窗口或标签。'
        )
    ensure_cpquery_front_tab(win, keyword=keyword)
    ensure_page_a11y(win)
    return win


# ---------------------------------------------------------------------------
# 树检索工具
# ---------------------------------------------------------------------------

def _ctype(elem) -> str:
    try:
        t = elem.element_info.control_type
        return str(t) if t is not None else ''
    except Exception:
        return ''


def _name(elem) -> str:
    try:
        return elem.element_info.name or ''
    except Exception:
        return ''


def _rect(elem) -> Optional[Tuple[int, int, int, int]]:
    try:
        r = elem.element_info.rectangle
        if r is None:
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def descendants(win, control_type: str = None, name: str = None,
                name_contains: str = None, depth: int = 32) -> List[Any]:
    """按类型/名称收集后代（每次现查，避免陈旧缓存）。"""
    out: List[Any] = []
    try:
        for el in win.descendants():
            if control_type and _ctype(el) != control_type:
                continue
            n = _name(el)
            if name is not None and n != name:
                continue
            if name_contains and name_contains not in n:
                continue
            out.append(el)
            if len(out) >= 500:
                break
    except Exception:
        pass
    return out


def all_texts(win) -> List[str]:
    """Document 下全部 Text/Edit 文本（读总数/兜底解析用）。"""
    texts: List[str] = []
    try:
        for el in win.descendants():
            if _ctype(el) in ('Text', 'Edit'):
                n = _name(el)
                if n and n not in texts:
                    texts.append(n)
                if len(texts) >= 800:
                    break
    except Exception:
        pass
    return texts


_TRIVIAL_A11Y = frozenset({'', '地址和搜索栏', 'Chrome Legacy Window'})
_MSAA_KEEPALIVE: list = []


def page_a11y_ok(texts) -> bool:
    meaningful = {t.strip() for t in (texts or [])
                  if t and t.strip() not in _TRIVIAL_A11Y}
    return len(meaningful) >= 5


def _msaa_from_hwnd(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes, byref, c_void_p
    try:
        from comtypes import GUID
        oleacc = ctypes.oledll.oleacc
        iid = GUID('{618736e0-3c3d-11cf-810c-00aa00389b71}')
        p = c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(
            wintypes.HWND(int(hwnd)), wintypes.LONG(-4), byref(iid), byref(p))
        if hr != 0:
            return False
        _MSAA_KEEPALIVE.append(p)
        return True
    except Exception:
        return False


def _enum_chrome_content_hwnds(top_hwnd: int) -> list:
    import ctypes
    from ctypes import wintypes, WINFUNCTYPE
    user32 = ctypes.windll.user32
    found: list = []
    interesting = (
        'Chrome_RenderWidgetHostHWND',
        'Intermediate D3D Window',
        'Chrome_WidgetWin_0',
    )

    @WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _on_child(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        if buf.value in interesting:
            found.append(int(hwnd))
        return True

    try:
        user32.EnumChildWindows(wintypes.HWND(int(top_hwnd)), _on_child, 0)
    except Exception:
        pass
    return found


def trigger_page_a11y(win) -> bool:
    ok = False
    try:
        top = int(win.handle)
    except Exception:
        return False
    if _msaa_from_hwnd(top):
        ok = True
    for child in _enum_chrome_content_hwnds(top)[:8]:
        if _msaa_from_hwnd(child):
            ok = True
    return ok


def chrome_exe_version(win) -> str:
    try:
        path = proc_image_path(win.process_id())
        if not path:
            return ''
        import ctypes
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return ''
        u = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
                buf, '\\', ctypes.byref(u), ctypes.byref(ln)):
            return ''

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ('dwSignature', ctypes.c_uint32),
                ('dwStrucVersion', ctypes.c_uint32),
                ('dwFileVersionMS', ctypes.c_uint32),
                ('dwFileVersionLS', ctypes.c_uint32),
                ('dwProductVersionMS', ctypes.c_uint32),
                ('dwProductVersionLS', ctypes.c_uint32),
                ('dwFileFlagsMask', ctypes.c_uint32),
                ('dwFileFlags', ctypes.c_uint32),
                ('dwFileOS', ctypes.c_uint32),
                ('dwFileType', ctypes.c_uint32),
                ('dwFileSubtype', ctypes.c_uint32),
                ('dwFileDateMS', ctypes.c_uint32),
                ('dwFileDateLS', ctypes.c_uint32),
            ]

        info = ctypes.cast(u, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return ('%d.%d.%d.%d' % (
            (ms >> 16) & 0xffff, ms & 0xffff,
            (ls >> 16) & 0xffff, ls & 0xffff))
    except Exception:
        return ''


def document_content_ready(win) -> bool:
    for d in descendants(win, control_type='Document'):
        r = _rect(d)
        if r is None or (r[2] - r[0]) <= 0:
            continue
        try:
            for _ in d.descendants():
                return True
        except Exception:
            continue
    return False


def ensure_page_a11y(win, wait_s: float = 1.8, retries: int = 3) -> bool:
    retries = max(1, int(retries))
    for i in range(retries):
        if page_a11y_ok(all_texts(win)) and document_content_ready(win):
            return True
        if page_a11y_ok(all_texts(win)):
            return True
        trigger_page_a11y(win)
        try:
            win.set_focus()
        except Exception:
            pass
        # 仅点 Document 内容区中心唤醒（禁止点整窗：易误点标题栏关闭）
        try:
            for d in descendants(win, control_type='Document'):
                dr = _rect(d)
                if not dr or (dr[2] - dr[0]) < 120 or (dr[3] - dr[1]) < 120:
                    continue
                human_click(
                    win, (dr[0] + dr[2]) // 2, (dr[1] + dr[3]) // 2, settle=0.15)
                break
        except Exception:
            pass
        time.sleep(wait_s + 0.4 * i)
    return page_a11y_ok(all_texts(win))


def a11y_fail_hint(win) -> str:
    ver = chrome_exe_version(win) or '?'
    empty_doc = not document_content_ready(win)
    parts = ['Chrome=%s' % ver]
    if empty_doc:
        parts.append('Document空壳')
    parts.append('内容通道未就绪，无法点左侧「复审无效审查决定查询」')
    return '；'.join(parts)


# ---------------------------------------------------------------------------
# 表单定位：标签 Text → 同行右侧最近的 Edit
# ---------------------------------------------------------------------------

def label_to_edit(win, label_text: str, max_row_gap: int = 40) -> Optional[Any]:
    """几何配对：找到 name 含 label_text 的 Text，返回其右侧同行最近的 Edit。

    probe 实测：'申请号/专利号：' Text@(529,284..633,302) → Edit@(636,280..1037,308)，
    即 Edit.top 与 Text.top 差 < 40px 且 Edit.left > Text.right。标签可传
    全名或前缀（如 '申请号'），用包含匹配以适配全角冒号差异。
    """
    labels = descendants(win, control_type='Text')
    target = None
    for t in labels:
        n = _name(t)
        if n and label_text in n:
            target = t
            break
    if target is None:
        return None
    tr = _rect(target)
    if tr is None:
        return None
    best = None
    best_dist = None
    for e in descendants(win, control_type='Edit'):
        er = _rect(e)
        if er is None:
            continue
        # 同行：垂直中心接近；右侧：left 在标签右边界之后
        if abs(((er[1] + er[3]) // 2) - ((tr[1] + tr[3]) // 2)) > max_row_gap:
            continue
        if er[0] < tr[2]:
            continue
        # 只取紧邻的（距离标签右边界最近），避免匹配到同行更右的字段
        dist = er[0] - tr[2]
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = e
    return best


def find_button(win, name: str) -> Optional[Any]:
    for b in descendants(win, control_type='Button', name=name):
        return b
    # 允许 MenuItem 承载可点项（如 展开更多搜索项）
    for m in descendants(win, control_type='MenuItem', name=name):
        return m
    return None


USAGE_DIALOG_MARK = '使用说明'
USAGE_DIALOG_BTN = '确定'


def _usage_ok_button(win):
    """「使用说明」模态上的「确定」；多枚时取靠近视口中部者。"""
    btns = list(descendants(win, control_type='Button', name=USAGE_DIALOG_BTN) or [])
    if not btns:
        return None
    if len(btns) == 1:
        return btns[0]
    scored = []
    for b in btns:
        r = _rect(b)
        if not r:
            continue
        cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
        area = max(1, r[2] - r[0]) * max(1, r[3] - r[1])
        scored.append((area - abs(cx - 960) * 0.3 - abs(cy - 700) * 0.5, b))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else btns[0]


def dismiss_usage_dialog(win, step=None, timeout_s: float = 12.0,
                         absent_giveup_s: float = 4.0) -> bool:
    """关掉 cpquery「使用说明」模态（点「确定」）。

    · 弹窗可能首屏就出，也可能晚到（点查询后才出）；各阶段都要可调用。
    · 一直没出现：absent_giveup_s 内放弃，避免登录卡 30s。
    · 已出现：在 timeout_s 内点确定，直到「使用说明」文案消失。
    """
    def _log(name, **kv):
        if callable(step):
            try:
                step(name, **kv)
            except TypeError:
                step(name)

    t0 = time.time()
    seen = False
    btn_miss = False
    while time.time() - t0 < timeout_s:
        joined = ' '.join(all_texts(win))
        if USAGE_DIALOG_MARK in joined:
            seen = True
            btn = _usage_ok_button(win)
            if btn is None:
                if not btn_miss:
                    _log('usage_dialog_btn_not_found')
                    btn_miss = True
                time.sleep(0.8)
                continue
            try:
                click(win, btn, settle=0.8)
            except Exception:
                try:
                    human_click(win, *_center(_rect(btn)), settle=0.8)
                except Exception as e:
                    _log('usage_dialog_click_fail', err=str(e)[:80])
                    time.sleep(0.8)
                    continue
            time.sleep(0.5)
            after = ' '.join(all_texts(win))
            if USAGE_DIALOG_MARK not in after:
                _log('usage_dialog_dismissed')
                return True
            _log('usage_dialog_click_ineffective')
        elif seen:
            _log('usage_dialog_dismissed')
            return True
        else:
            if time.time() - t0 >= absent_giveup_s:
                _log('usage_dialog_not_found')
                return False
        time.sleep(0.8)
    _log('usage_dialog_timeout', seen=seen)
    return False


def usage_dialog_present(win) -> bool:
    return USAGE_DIALOG_MARK in ' '.join(all_texts(win))


# ---------------------------------------------------------------------------
# 动作（真实键鼠）
# ---------------------------------------------------------------------------

def _center(rect: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def click(win, elem, settle: float = 0.35) -> None:
    """真实鼠标点击元素中心（先激活窗口保证前台焦点）。"""
    r = _rect(elem)
    if r is None:
        raise RuntimeError('元素无坐标，无法点击')
    win.set_focus()
    time.sleep(0.15)
    x, y = _center(r)
    from pywinauto.mouse import click as _mouse_click
    _mouse_click(button='left', coords=(x, y))
    time.sleep(settle)


def activate_element(elem) -> bool:
    """无键鼠激活控件（TabItem.select / Button.invoke）。

    229 实跑：RDP/会话输入被拦时 SetCursorPos 失败，但 UIA Pattern 仍可用。
    """
    for name in ('select', 'invoke'):
        fn = getattr(elem, name, None)
        if not callable(fn):
            continue
        try:
            fn()
            return True
        except Exception:
            continue
    return False


def safe_click(win, elem, settle: float = 0.35, tries: int = 3, log=None) -> None:
    """优先 UIA Pattern 激活，失败再鼠标点击（带 SetCursorPos 重试）。"""
    if activate_element(elem):
        time.sleep(settle)
        if log:
            log('safe_click', how='uia_pattern')
        return
    last: Optional[Exception] = None
    for i in range(1, int(tries) + 1):
        try:
            click(win, elem, settle=settle)
            if i > 1 and log:
                log('safe_click', how='mouse', attempt=i)
            return
        except Exception as e:
            last = e
            if log:
                log('safe_click', attempt=i, err=str(e)[:100])
            try:
                win.set_focus()
            except Exception:
                pass
            time.sleep(0.5 * i)
    raise RuntimeError(f'点击失败（UIA+鼠标均不可用）: {last}')


def clear_and_type(win, elem, text: str, pause: float = 0.06) -> str:
    """点击输入框 → 全选清空 → 逐字真实键入 → Tab 失焦；返回框内最终值。

    与手工输入等价：键盘事件 isTrusted=true；申请号原样键入（含小数点），
    站点自己会做规范化（2026-09-01 OCR 对拍证明）。
    SetCursorPos/SendInput 被拦时回落 ValuePattern.set_edit_text。
    """
    r = _rect(elem)
    if r is None:
        raise RuntimeError('输入框无坐标，无法键入')
    try:
        click(win, elem)
        safe_send_keys('^a', win=win, pause=0.02)
        safe_send_keys('{DELETE}', win=win, pause=0.05)
        safe_send_keys(str(text), win=win, pause=pause, with_spaces=True,
                        with_tabs=False)
        safe_send_keys('{TAB}', win=win, pause=0.05)
        time.sleep(0.25)
        return edit_value(elem)
    except Exception:
        try:
            elem.set_edit_text(str(text))
            time.sleep(0.2)
            return edit_value(elem) or str(text)
        except Exception:
            raise


def chrome_exe_path() -> Optional[str]:
    configured = (os.environ.get('CNIPA_CHROME_EXE') or '').strip()
    if configured and os.path.isfile(configured):
        return configured
    for p in (
        os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
    ):
        if p and os.path.isfile(p):
            return p
    return None


def chrome_user_data_dir() -> str:
    env = (os.environ.get('CNIPA_CHROME_USER_DATA_DIR') or '').strip()
    if env:
        return env
    # 与 service/config.ini [chrome] user_data_dir 默认一致
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'chrome_profile')


def open_url_via_chrome(url: str) -> Tuple[bool, str]:
    """用同一 user-data-dir 调 chrome.exe 打开 URL（附加到已有实例新标签）。

    不经 SendInput/鼠标，专治 229 会话键鼠被拦时 _open_url_tab 打不开 pss。
    不加 --new-window，避免再弹一扇窗。
    """
    import subprocess
    exe = chrome_exe_path()
    if not exe:
        return False, 'no_chrome_exe'
    profile = chrome_user_data_dir()
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        pass
    args = [exe, f'--user-data-dir={profile}',
            '--force-renderer-accessibility', url]
    flags = 0
    if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP'):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, 'DETACHED_PROCESS'):
        flags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB'):
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(exe),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as e:
        return False, f'launch_fail:{e}'
    return True, 'chrome_cli'


def foreground_hwnd() -> int:
    """当前前台窗口句柄（取不到/安全桌面时返回 0）。"""
    import ctypes
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def safe_send_keys(keys: str, win=None, pause: float = 0.03,
                   with_spaces: bool = False, with_tabs: bool = False,
                   tries: int = 4, log=None) -> None:
    """SendInput 合成键盘发送，带前台校验 + 失败重试。

    2026-09-08 实证（05 fetch_abstract_figure）：^t 成功后 0.8s 键 URL
    报 RuntimeError: SendInput() inserted only 0 out of 2 keyboard events
    ——SendInput 一个事件都没插进输入流，属系统级阻断：那一刻前台是
    更高完整性窗口（UAC 提权弹层/管理员窗，UIPI 拦低完整性进程输入）、
    锁屏/断开的 RDP/屏保安全桌面，或安全软件钩子临时 BlockInput。
    多为瞬时态：抢回前台 + 退避重试即可过；连续 tries 次仍 0 插入才抛错。
    """
    from pywinauto.keyboard import send_keys
    last: Optional[Exception] = None
    for i in range(1, int(tries) + 1):
        if win is not None:
            try:
                if foreground_hwnd() != int(win.handle):
                    win.set_focus()
                    time.sleep(0.3)
            except Exception:
                pass
        try:
            send_keys(keys, pause=pause, with_spaces=with_spaces,
                      with_tabs=with_tabs)
            if i > 1 and log:
                log('send_keys_retry', ok=True, attempt=i, keys=keys[:40])
            return
        except RuntimeError as e:
            last = e
            if log:
                log('send_keys_retry', attempt=i, err=str(e)[:100],
                    keys=keys[:40])
            time.sleep(0.6 * i)
    raise RuntimeError(
        f'SendInput 连续 {tries} 次被系统阻断（0 事件插入）：{last}；'
        '常见原因：前台出现 UAC/管理员窗口（UIPI 拦截）、锁屏或远程会话'
        '断开、安全软件阻断模拟键盘。请保持会话解锁、关闭提权弹窗后重跑。')


def click_coords(win, x: int, y: int, settle: float = 0.5) -> None:
    """真实鼠标点击屏幕坐标（元素在 a11y 树无可点角色但几何已知时用，如卡片标题链接）。

    注意（2026-09-01 19:02 实跑教训）：pywinauto.mouse.click 内部是
    「光标瞬移到目标 + down/up 几乎零间隔」——页面只见到一次大跳变
    mousemove 和一个无按压时长的 click。对瑞数行为风控保护的站点
    （cpquery 卡片标题 <a target=_blank>），这类瞬移点击会被判非人：
    站点 JS 放弃 window.open 新标签，退化为同页 SPA 跳转（右栏空壳、
    导航收起）。需要开新标签语义的关键点击请用 human_click。
    """
    win.set_focus()
    time.sleep(0.15)
    from pywinauto.mouse import click as _mouse_click
    _mouse_click(button='left', coords=(int(x), int(y)))
    time.sleep(settle)


def human_move_to(x: int, y: int, steps: int = 36, dwell_s: float = 0.4,
                  step_sleep: tuple = (0.018, 0.045)):
    """只慢速移动光标到目标（不点击）。返回 (sx, sy, tx, ty)。"""
    import ctypes
    import ctypes.wintypes  # noqa: F401
    import random

    u32 = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    sx, sy = int(pt.x), int(pt.y)
    tx, ty = int(x), int(y)
    n = max(8, int(steps))
    lo, hi = step_sleep
    for i in range(1, n + 1):
        f = i / n
        # ease-out：后半段更慢，便于肉眼看清落点
        ease = 1.0 - (1.0 - f) * (1.0 - f)
        jx = random.randint(-2, 2) if 1 < i < n else 0
        jy = random.randint(-2, 2) if 1 < i < n else 0
        u32.SetCursorPos(int(sx + (tx - sx) * ease) + jx,
                         int(sy + (ty - sy) * ease) + jy)
        time.sleep(random.uniform(lo, hi))
    u32.SetCursorPos(tx, ty)
    time.sleep(max(0.0, float(dwell_s)))
    return sx, sy, tx, ty


def human_click(win, x: int, y: int, settle: float = 0.6, steps: int = 14,
                press_s: float = 0.09, dwell_s: float = 0.15,
                set_focus: bool = True) -> None:
    """拟真点击：光标多步移入（ease-out+抖动）→ 悬停 → 真实按压时长 → 松开。

    2026-09-01 19:02/19:25 对拍结论：同一坐标（标题文字中部）人工点击开
    新标签且右栏有内容，程序瞬移点击却同页跳转、右栏空壳——差异不在落点
    而在「怎么点」。逐帧对齐人手行为：移入轨迹（每步 SetCursorPos 均产生
    WM_MOUSEMOVE → Chrome 派发 isTrusted mousemove，触发 hover 态）、
    悬停 ~150ms、mousedown→mouseup 间隔 ~90ms（人手 60~120ms）。
    """
    import ctypes
    import ctypes.wintypes  # noqa: F401（POINT 结构需要）

    if set_focus:
        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(0.15)
    human_move_to(x, y, steps=steps, dwell_s=dwell_s)
    u32 = ctypes.windll.user32
    u32.mouse_event(0x0002, 0, 0, 0, 0)       # MOUSEEVENTF_LEFTDOWN
    time.sleep(press_s)                       # 按压时长（人手 60~120ms）
    u32.mouse_event(0x0004, 0, 0, 0, 0)       # MOUSEEVENTF_LEFTUP
    time.sleep(settle)


def human_ctrl_click(win, x: int, y: int, settle: float = 0.8, steps: int = 14,
                     press_s: float = 0.09, dwell_s: float = 0.15) -> None:
    """按住 Ctrl 的拟人左键：强制走「新标签打开」语义，避免同页 SPA 空壳。"""
    import ctypes

    u32 = ctypes.windll.user32
    VK_CONTROL = 0x11
    KEYEVENTF_KEYUP = 0x0002
    try:
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.05)
        human_click(win, x, y, settle=settle, steps=steps,
                    press_s=press_s, dwell_s=dwell_s)
    finally:
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.1)


def human_right_click(win, x: int, y: int, settle: float = 0.5, steps: int = 12,
                      press_s: float = 0.09, dwell_s: float = 0.12) -> None:
    """拟人右键（05 摘要附图：图片右键 →「在新标签页中打开图片」拿原图）。

    轨迹逻辑同 human_click；右键按下/抬起用 RIGHTDOWN 0x0008 / RIGHTUP 0x0010。
    右键菜单弹出后 settle 内稳定，供调用方枚举 MenuItem。
    """
    import ctypes
    import ctypes.wintypes  # noqa: F401（POINT 结构需要）
    import random

    win.set_focus()
    time.sleep(0.15)
    u32 = ctypes.windll.user32
    tx, ty = int(x), int(y)
    cur = ctypes.wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(cur))
    sx, sy = int(cur.x), int(cur.y)
    n = max(4, int(steps))
    for i in range(1, n + 1):
        f = i / n
        u32.SetCursorPos(int(sx + (tx - sx) * f) + (random.randint(-2, 2) if 1 < i < n else 0),
                         int(sy + (ty - sy) * f) + (random.randint(-2, 2) if 1 < i < n else 0))
        time.sleep(random.uniform(0.008, 0.02))
    u32.SetCursorPos(tx, ty)
    time.sleep(dwell_s)
    u32.mouse_event(0x0008, 0, 0, 0, 0)       # MOUSEEVENTF_RIGHTDOWN
    time.sleep(press_s)
    u32.mouse_event(0x0010, 0, 0, 0, 0)       # MOUSEEVENTF_RIGHTUP
    time.sleep(settle)


def human_drag(win, x1: int, y1: int, x2: int, y2: int, steps: int = 30,
               settle: float = 0.8) -> None:
    """按住左键从 (x1,y1) 拖到 (x2,y2) 后松开——PW human_drag 的 UIA 移植。

    轨迹参数逐项照搬 src/utils/core.py::human_drag（2026-08 PW 实跑验证
    可过瑞数滑块）：30 步、ease-out（先快后慢）、步延时 0.005+0.02*t、
    按下前 0.05~0.12s、按住稳定 0.08~0.15s、到位后停止期 0.15~0.30s 再松手
    （停止期过短会被服务端轨迹分析判机器人）。
    新增 ±1px 垂直抖动（人手滑块拖动不完全水平）。
    """
    import ctypes
    import random

    win.set_focus()
    time.sleep(0.15)
    u32 = ctypes.windll.user32
    x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
    u32.SetCursorPos(x1i, y1i)
    time.sleep(random.uniform(0.05, 0.12))
    u32.mouse_event(0x0002, 0, 0, 0, 0)       # MOUSEEVENTF_LEFTDOWN
    time.sleep(random.uniform(0.08, 0.15))
    _drag_held_curve(u32, x1i, y1i, x2i, y2i, steps=steps)
    u32.SetCursorPos(x2i, y2i)
    time.sleep(random.uniform(0.15, 0.30))     # 松手前停止期
    u32.mouse_event(0x0004, 0, 0, 0, 0)       # MOUSEEVENTF_LEFTUP
    time.sleep(settle)


def drag_hold_start(win, x: int, y: int) -> None:
    """开环+闭环拖动的第一段：移到 (x,y) 并按下左键（保持按住，不松手）。

    与 human_drag 分离的原因：PW 版 human_drag_align_piece 的闭环微调
    （欠拖前补/过冲回拉）要求鼠标始终按住；UIA 版需要同样能力。
    """
    import ctypes
    import random

    win.set_focus()
    time.sleep(0.15)
    u32 = ctypes.windll.user32
    u32.SetCursorPos(int(x), int(y))
    time.sleep(random.uniform(0.05, 0.12))
    u32.mouse_event(0x0002, 0, 0, 0, 0)       # LEFTDOWN
    time.sleep(random.uniform(0.08, 0.15))


def drag_held_to(win, x2: int, y2: int, steps: int = 25) -> None:
    """按住状态下沿 ease-out 曲线拖到 (x2,y2)（PW _drag_to 移植，不松手）。"""
    import ctypes
    import random

    u32 = ctypes.windll.user32
    pt = _cursor_pos(u32)
    _drag_held_curve(u32, int(pt[0]), int(pt[1]), int(x2), int(y2), steps=steps)
    u32.SetCursorPos(int(x2), int(y2))
    time.sleep(random.uniform(0.05, 0.10))


def drag_release(win, settle: float = 0.8) -> None:
    """按住状态下的松手（含停止期，防机器人判定）。"""
    import ctypes
    import random

    u32 = ctypes.windll.user32
    time.sleep(random.uniform(0.15, 0.30))     # 停止期
    u32.mouse_event(0x0004, 0, 0, 0, 0)       # LEFTUP
    time.sleep(settle)


def _cursor_pos(u32):
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _drag_held_curve(u32, x1: int, y1: int, x2: int, y2: int, steps: int = 25) -> None:
    """ease-out 曲线移动（鼠标已按住）。PW _drag_to 参数：delay = 0.004+0.016*t。"""
    import random
    n = max(6, int(steps))
    for i in range(n):
        t = (i + 1) / n
        ease = 1 - (1 - t) ** 2
        jy = random.randint(-1, 1) if 1 < i < n else 0
        u32.SetCursorPos(int(x1 + (x2 - x1) * ease),
                         int(y1 + (y2 - y1) * ease) + jy)
        time.sleep(0.004 + 0.016 * t)


def scroll_down(win, x: int, y: int, notches: int = 5, settle: float = 0.8) -> None:
    """在屏幕坐标 (x,y) 处真实滚轮下滚（SendInput wheel，isTrusted）。

    2026-09-01 展开态实测（cpquery_tree_20260901_165804）：「展开更多搜索项」把
    表单撑高后，结果卡片被推出视口，Chrome a11y 对屏外文本回 0 矩形 @(0,0,0,0)。
    解析器已用「枚举顺序门槛」兼容 0 矩形；本函数是兜底——需要真实坐标/截图时
    把内容滚回视口。
    """
    win.set_focus()
    time.sleep(0.1)
    from pywinauto.mouse import scroll as _mouse_scroll
    _mouse_scroll(coords=(int(x), int(y)), wheel_dist=-abs(notches))
    time.sleep(settle)


def scroll_up(win, x: int, y: int, notches: int = 5, settle: float = 0.8) -> None:
    """在屏幕坐标 (x,y) 处真实滚轮上滚（04 收割前把右栏决定书滚回顶部用）。"""
    win.set_focus()
    time.sleep(0.1)
    from pywinauto.mouse import scroll as _mouse_scroll
    _mouse_scroll(coords=(int(x), int(y)), wheel_dist=abs(notches))
    time.sleep(settle)


def edit_value(elem) -> str:
    """读输入框当前值。只走值通道，不用 name。

    2026-09-01 终案（uia_probe_readback 穷举实证，output/uia_probe）：
      - 几何配对命中的常是外层容器 Edit（name=''、无值），真正持值的内层
        input Edit 在第 3 层（Edit>Group>Group>Edit）→ 必须用 descendants
        下钻，children 全是 Group 挖不到；
      - 内层 Edit 的 name 是 antd 图标名（'cancel'），值在
        LegacyIAccessible.Value / ValuePattern.CurrentValue，且 pywinauto
        legacy_properties() 的键是大写 'Value'——旧代码用小写 'value' 取键
        永远拿空（02/03 两轮回读为空的共同根因）；
      - 可用通道（探针实测均返回 '4W107577'）：get_value()、
        iface_value.CurrentValue、legacy_properties()['Value']。
    """
    def _read(el) -> str:
        try:
            v = el.get_value() or ''
        except Exception:
            v = ''
        if not v:
            try:
                v = el.legacy_properties().get('Value') or ''
            except Exception:
                v = ''
        return str(v)

    v = _read(elem)
    if v:
        return v
    try:
        for d in elem.descendants():
            if _ctype(d) == 'Edit':
                v = _read(d)
                if v:
                    return v
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# 结果读取
# ---------------------------------------------------------------------------

TOTAL_PATTERNS = (
    re.compile(r'共查询到\s*([0-9,，]+)\s*条'),
    re.compile(r'共检索到\s*([0-9,，]+)\s*条'),
    re.compile(r'共\s*([0-9,，]+)\s*条'),
)


def read_total(win) -> Optional[int]:
    """读结果总数；0 也返回 0，读不到返回 None。"""
    joined = ' '.join(all_texts(win))
    for pat in TOTAL_PATTERNS:
        m = pat.search(joined)
        if m:
            return int(m.group(1).replace(',', '').replace('，', ''))
    return None


def wait_total_change(win, was: Optional[int], timeout_s: float = 20.0,
                      poll_s: float = 0.8, on_poll=None) -> Optional[int]:
    """轮询总数，等到与 was 不同（或 was 为 None 时出现数字）即返回。

    on_poll：每轮可读到总数前调用（用于关晚到的「使用说明」）。
    was 已是 0 且结果仍为 0：min_settle 后连续稳定读到 0 即返回（避免空转满超时）。
    """
    t0 = time.time()
    stable0 = 0
    min_settle_s = 2.5 if was == 0 else 0.0
    while time.time() - t0 < timeout_s:
        if callable(on_poll):
            try:
                on_poll()
            except Exception:
                pass
        cur = read_total(win)
        if cur is not None and cur != was:
            # 有结果再查时页面会先闪「0/空白」；勿把过渡 0 当最终结果
            if cur == 0 and was is not None and was > 0:
                time.sleep(poll_s)
                continue
            return cur
        if was == 0 and cur == 0 and (time.time() - t0) >= min_settle_s:
            stable0 += 1
            if stable0 >= 2:
                return 0
        else:
            stable0 = 0
        time.sleep(poll_s)
    return read_total(win)


# ---------------------------------------------------------------------------
# 登录态 / 截图
# ---------------------------------------------------------------------------

def looks_logged_in(win) -> bool:
    for t in descendants(win, control_type='MenuItem'):
        if '欢迎您' in _name(t):
            return True
    return False


def cpquery_guest_ready(texts) -> bool:
    """cpquery 未出现「欢迎您」但页面已可检索/看详情（与手工免登录会话一致）。"""
    if not texts:
        return False
    joined = ' '.join(t for t in texts if t)
    if '国内案件信息详情' in joined:
        return True
    if any('共查询到' in (t or '') for t in texts):
        return True
    for mark in ('无效决定库', '复审决定库', '复审无效审查决定', '申请号/专利号'):
        if mark in joined:
            return True
    return False


def cpquery_session_ok(win) -> bool:
    """已 SSO 登录或 guest 会话已能操作 cpquery。"""
    if looks_logged_in(win):
        return True
    try:
        return cpquery_guest_ready(all_texts(win))
    except Exception:
        return False


def dismiss_chrome_translate_offer(win, log=None) -> bool:
    """关掉 Chrome「翻译此页 / Google Translate」条（英文界面时易弹出）。"""
    joined = ' '.join(all_texts(win))
    # 勿用「英语/中文」单字：详情页正文也常出现，会误 ESC
    marks = (
        'Google Translate', '翻译此页', 'Translate this page',
        '翻译此网页', '一律翻译', 'Always translate',
    )
    if not any(m in joined for m in marks):
        return False
    if log:
        log('dismiss_translate', note='关闭翻译提示')
    try:
        win.set_focus()
        safe_send_keys('{ESC}', win=win, pause=0.05)
        time.sleep(0.3)
    except Exception:
        pass
    return True


def dismiss_chrome_restore_bubble(win) -> bool:
    """关掉 Chrome「Restore pages? / 恢复页面」崩溃恢复气泡。

    229 实跑：气泡挡在右上角，抢焦点/挡点击，详情滚轮与检索提交会假成功。
    只关气泡，不点 Restore（点恢复会搅乱标签）。

    严禁点窗口标题栏「关闭」：中文 Chrome 标题栏按钮也叫「关闭」，
    旧逻辑按名匹配 Close/关闭 时会点到整窗关闭 → 浏览器直接退出。
    """
    wr = _rect(win)
    if wr is None:
        return False
    win_right, win_top = wr[2], wr[1]

    def _is_titlebar_chrome_btn(r) -> bool:
        """窗口最小化/最大化/关闭：贴右缘、贴顶。"""
        if not r:
            return True
        return r[0] >= win_right - 90 and r[1] <= win_top + 50

    marks = ('Restore pages', "didn't shut down", '未正确关闭', '恢复页面',
             'Chrome 未正确')
    joined = ' '.join(all_texts(win))
    restore_btn = None
    for b in descendants(win, control_type='Button'):
        n = (_name(b) or '').strip()
        r = _rect(b)
        if not r or r[1] > 160 or _is_titlebar_chrome_btn(r):
            continue
        if n in ('Restore', '恢复', '恢复页面'):
            restore_btn = b
            break
    if not restore_btn and not any(m in joined for m in marks):
        return False
    # 优先点气泡自身关闭（须贴近 Restore，且绝不能是标题栏关闭）
    if restore_btn is not None:
        rr = _rect(restore_btn)
        for b in descendants(win, control_type='Button'):
            n = (_name(b) or '').strip()
            r = _rect(b)
            if not r or not rr or r[1] > 160:
                continue
            if _is_titlebar_chrome_btn(r):
                continue
            if n not in ('Close', '关闭', '×', 'X'):
                continue
            if r[0] < rr[0] - 20:
                continue
            # 气泡 X 紧挨 Restore；窗口关闭钮远在右上角
            if r[0] > rr[2] + 100:
                continue
            if abs(((r[1] + r[3]) // 2) - ((rr[1] + rr[3]) // 2)) > 40:
                continue
            human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2,
                        settle=0.3)
            time.sleep(0.3)
            return True
        # 禁止盲点 Restore 右侧坐标（易落到标题栏关闭上）
    try:
        win.set_focus()
        safe_send_keys('{ESC}', win=win, pause=0.05)
        time.sleep(0.25)
    except Exception:
        pass
    return True


def screenshot_window(win, path: str) -> str:
    """截取目标窗口区域（PIL ImageGrab，纯读屏、无页面交互）。"""
    r = _rect(win)
    if r is None:
        return ''
    # 拒绝无效/离屏矩形（否则 ImageGrab/PNG 编码会抛 SystemError）
    if r[2] <= r[0] or r[3] <= r[1]:
        return ''
    if r[0] < -5000 or r[1] < -5000 or r[2] > 20000 or r[3] > 20000:
        return ''
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(r[0], r[1], r[2], r[3]), all_screens=True)
    if img.width < 2 or img.height < 2:
        return ''
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path)
    return path
