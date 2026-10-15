# -*- coding: utf-8 -*-
"""只读 UIA 探测器（安全：零点击、零输入、零网络、不激活窗口）。

用途：2026-09-01 定案 Playwright/CDP 一切形态被瑞数拒绝（run5-9 全 400，
R1 证明去掉调试口即恢复）→ 自动化转 OS 层（pywinauto/UIA + SendInput）。
写选择器之前，需要先知道 cpquery 页面控件在系统 UIA 无障碍树里的真实
命名（控件类型 / Name / AutomationId / 坐标），本脚本就是干这个的。

只做三件事：
  1) 枚举 Chrome 顶层窗口（Chrome_WidgetWin_1）；
  2) 找到标题含关键字的窗口（= cpquery 标签正处于该窗口前台）；
  3) 递归 dump 控件树（深度/节点数封顶），落盘 output/uia_probe/。

绝不调用 click / type_keys / set_focus / set_window_pos —— 只读。
注意：Chrome 只有【前台标签页】的内容会进 UIA 树；若目标标签不在前台，
窗口标题对不上，脚本会提示用户手工切到该标签后重跑。
"""
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TITLE_KEYWORD = os.environ.get('CNIPA_UIA_TITLE', '中国及多国专利审查信息查询')
MAX_DEPTH = int(os.environ.get('CNIPA_UIA_MAX_DEPTH', '16'))
MAX_NODES = int(os.environ.get('CNIPA_UIA_MAX_NODES', '3500'))
NAME_LEN = 70


def _rect_str(info) -> str:
    try:
        r = info.rectangle
        if r is None:
            return ''
        return f' @({r.left},{r.top},{r.right},{r.bottom})'
    except Exception:
        return ''


def walk(elem, depth, lines, stats):
    if depth > MAX_DEPTH or stats['n'] >= MAX_NODES:
        stats['stopped'] = True
        return
    try:
        info = elem.element_info
        name = (info.name or '').replace('\r', ' ').replace('\n', ' ')
        if len(name) > NAME_LEN:
            name = name[:NAME_LEN] + '…'
        aid = info.automation_id or ''
        ctype = getattr(info, 'control_type', '') or ''
        if isinstance(ctype, int):
            ctype = f'type#{ctype}'
        lines.append('  ' * depth + f'{ctype} | name={name!r} | aid={aid!r}{_rect_str(info)}')
        stats['n'] += 1
        for ch in elem.children():
            walk(ch, depth + 1, lines, stats)
            if stats['n'] >= MAX_NODES:
                return
    except Exception as e:
        lines.append('  ' * depth + f'<err: {type(e).__name__}: {e}>')


def find_render_host_hwnds(top_hwnd) -> list:
    """枚举顶层窗口下 class=Chrome_RenderWidgetHostHWND 的子 HWND。

    网页内容挂在渲染宿主子窗口上；对它做 UIA 查询是触发 Chrome 生成
    无障碍树的标准入口（无需 --force-renderer-accessibility）。
    """
    import ctypes
    from ctypes import wintypes
    from ctypes import WINFUNCTYPE

    user32 = ctypes.windll.user32
    found: list = []
    cb = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _on_child(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == 'Chrome_RenderWidgetHostHWND':
            found.append(hwnd)
        return True

    user32.EnumChildWindows(top_hwnd, cb(_on_child), 0)
    return found


def uia_element_from_hwnd(hwnd):
    """用指定 HWND 直接构造 UIA wrapper（不激活、不聚焦）。"""
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    return UIAWrapper(UIAElementInfo(hwnd))


def trigger_msaa(hwnd) -> bool:
    """向窗口发 WM_GETOBJECT 查询（AccessibleObjectFromWindow, OBJID_CLIENT）。

    这是屏幕阅读器（NVDA/JAWS）让 Chrome 开启渲染无障碍树的标准方式；
    Chrome 125 已无 Chrome_RenderWidgetHostHWND 子 HWND，此路是免启动参数
    触发的正解。仍然只读：不发送任何输入/网络。
    """
    import ctypes
    from ctypes import wintypes, byref, c_void_p
    from comtypes import GUID

    oleacc = ctypes.oledll.oleacc
    iid_iaccessible = GUID('{618736e0-3c3d-11cf-810c-00aa00389b71}')
    p = c_void_p()
    try:
        hr = oleacc.AccessibleObjectFromWindow(
            wintypes.HWND(hwnd), wintypes.LONG(-4), byref(iid_iaccessible), byref(p))
        if hr != 0:
            print(f'[probe] AccessibleObjectFromWindow hr=0x{hr & 0xFFFFFFFF:08x}')
            return False
        _KEEPALIVE.append(p)  # 持有指针到脚本退出，避免误释放
        return True
    except Exception as e:
        print(f'[probe] msaa trigger err: {e}')
        return False


_KEEPALIVE: list = []


def main() -> None:
    import cnipa_uia as uia_mod

    rows = uia_mod.enum_top_hwnds(class_exact='Chrome_WidgetWin_1')
    print(f'[probe] Chrome 顶层窗口 {len(rows)} 个（Win32 Enum，非 Desktop.windows）')
    target = None
    for hwnd, _cls, title in rows:
        mark = ''
        if TITLE_KEYWORD in (title or ''):
            target = uia_mod.uia_window_from_hwnd(hwnd)
            mark = '  <-- 目标'
        print(f'  - {title!r}{mark}')
    if target is None:
        print(f'[probe] 未找到标题含 {TITLE_KEYWORD!r} 的窗口。')
        print('[probe] => 请在 Chrome 里手工点开 cpquery 那个标签页（让它成为前台标签），再重跑本脚本。')
        return

    # 触发渲染无障碍树：先 MSAA WM_GETOBJECT（读屏方式），再枚举渲染宿主子窗口
    if trigger_msaa(target.handle):
        print('[probe] MSAA WM_GETOBJECT 已发送（Chrome 将异步生成无障碍树）')
    roots = []
    try:
        hosts = find_render_host_hwnds(target.handle)
        if hosts:
            print(f'[probe] Chrome_RenderWidgetHostHWND 子窗口 {len(hosts)} 个')
            for h in hosts[:3]:
                roots.append(('rwh:' + str(h), lambda hh=h: uia_element_from_hwnd(hh)))
    except Exception as e:
        print(f'[probe] 子窗口枚举失败: {e}')
    roots.append(('top', lambda: target))

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'output', 'uia_probe')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'cpquery_tree_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.txt')
    lines = [f'# UIA tree dump (read-only) {datetime.now().isoformat()}',
             f'# window title = {target.window_text()!r}',
             f'# MAX_DEPTH={MAX_DEPTH} MAX_NODES={MAX_NODES}', '']
    t0 = time.time()
    best = None
    for attempt in range(1, 4):
        for tag, maker in roots:
            stats = {'n': 0, 'stopped': False}
            sub: list = [f'--- attempt {attempt} root={tag} ---']
            try:
                walk(maker(), 0, sub, stats)
            except Exception as e:
                sub.append(f'<walk err: {e}>')
                continue
            lines.extend(sub if attempt == 1 or not best else [])
            if best is None or stats['n'] > best[0]:
                best = (stats['n'], tag, sub)
            print(f'[probe] attempt {attempt} root={tag} nodes={stats["n"]}')
        if best and best[0] > 60:
            break  # 网页树已长出来
        print(f'[probe] 节点过少（{best[0] if best else 0}），等 2.5s 触发无障碍树重试 …')
        time.sleep(2.5)
    if best:
        lines.append('')
        lines.extend(best[2])
        lines.append(f'# best_root={best[1]} nodes={best[0]} elapsed={time.time() - t0:.1f}s')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'[probe] 最优树节点 {best[0] if best else 0} 个（root={best[1] if best else "-"}），总耗时 {time.time() - t0:.1f}s')
    print(f'[probe] 已落盘: {out_path}')


if __name__ == '__main__':
    main()
