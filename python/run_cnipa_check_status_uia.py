# -*- coding: utf-8 -*-
"""CNIPA UIA 轨道环境状态检查（cnipa_login 的新语义，2026-09-02 定案）。

UIA 轨道没有也不需要自动登录（驱动的是操作者桌面已登录的 Chrome）。
本脚本零点击、零站点交互、零键盘鼠标输入——只枚举窗口树读状态，任何人任何时候跑都安全：

  1. chrome.exe 窗口在场吗？（类名 Chrome_WidgetWin_1 且进程名 chrome.exe，
     自动排除 Electron 宿主/Edge 冒充——cnipa_uia.find_any_chrome 同款判据）
  2. 辅助功能(a11y)通道活着吗？（Chrome 未带 --force-renderer-accessibility
     启动时，UIA 树里几乎只有「地址和搜索栏」等零星元素 → 判 a11y 关闭）
  3. cpquery 标签在前台吗？（窗口标题含「中国及多国专利审查信息查询」）
  4. 已登录吗？（cpquery 页内 MenuItem「欢迎您」标记，纯读树）

运行：
    python -X utf8 python/run_cnipa_check_status_uia.py
    python -X utf8 python/run_cnipa_check_status_uia.py --selftest   # 离线自测
产出：python/output/uia_runs/status_<ts>/payload.json
MCP 层（CNIPA_EMIT_JSON=1）经 cnipa_contract 投影为契约字段。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_contract  # noqa: E402
import cnipa_uia as uia  # noqa: E402

REPO = os.path.dirname(ROOT)

# a11y 判定的琐碎元素名（Chrome 未开 a11y 时 UIA 树里仅剩这些）
_TRIVIAL_NAMES = {'', '地址和搜索栏', 'Chrome Legacy Window'}

# 判 a11y 存活所需的最小非琐碎文本数（经验阈值：a11y 关闭时 <3 个）
A11Y_MIN_TEXTS = 5


def a11y_exposed(texts) -> bool:
    """纯函数：由窗口文本集合判 a11y 通道是否暴露（selftest 可离线回放）。"""
    meaningful = {t.strip() for t in texts if t and t.strip() not in _TRIVIAL_NAMES}
    return len(meaningful) >= A11Y_MIN_TEXTS


def build_status(chrome_windows, a11y_ok, cpquery_win, logged_in,
                 cpquery_usable=False):
    """纯函数：组装状态 dict（window 信息已剥离，可离线测）。"""
    titles = [w['title'] for w in chrome_windows if not w['minimized']][:5]
    session_ok = bool(logged_in or cpquery_usable)
    ready = bool(a11y_ok and cpquery_win and session_ok)
    if ready:
        if logged_in:
            msg = '环境就绪：Chrome+a11y+cpquery 前台+已登录，可以执行检索/详情/附图工具。'
        else:
            msg = ('环境就绪：Chrome+a11y+cpquery 可检索（guest 会话，未检测到「欢迎您」）。')
        message = msg
    else:
        miss = []
        if not chrome_windows:
            miss.append('未见可用浏览器窗口')
        elif not cpquery_win:
            miss.append('查询页未就绪（请打开「中国及多国专利审查信息查询」）')
        elif not a11y_ok:
            miss.append('页面内容通道未就绪')
        elif not session_ok:
            miss.append('未登录且 cpquery 页不可操作')
        message = '环境未就绪：' + '；'.join(miss) + '。'
    return {
        'chrome_found': bool(chrome_windows),
        'chrome_window_count': len(chrome_windows),
        'a11y_ok': bool(a11y_ok),
        'cpquery_front': bool(cpquery_win),
        'logged_in': bool(logged_in),
        'cpquery_usable': bool(cpquery_usable),
        'ready': ready,
        'window_titles': titles,
        'message': message,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if '--selftest' in argv:
        return selftest()

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(REPO, 'python', 'output', 'uia_runs', 'status_' + ts)
    os.makedirs(out_dir, exist_ok=True)
    payload = {'mode': 'status', 'run_dir': out_dir,
               'started_at': datetime.now().isoformat(), 'checks': []}

    try:
        chrome_windows = uia.find_any_chrome()   # [(win, info), ...]
        chrome_infos = [info for _w, info in chrome_windows]
    except Exception as e:  # pywinauto 不可用等
        payload.update(build_status([], False, None, False))
        payload['error'] = f'窗口枚举失败：{e}'
        _finish(payload, out_dir)
        return 1

    a11y_ok = False
    cpquery_win = None
    logged_in = False
    cpquery_usable = False
    probe = None
    # 逐窗探测：优先非最小化窗口；cpquery 标题窗优先作探测对象
    for w, info in chrome_windows:
        try:
            texts = uia.all_texts(w)
        except Exception:
            continue
        payload['checks'].append({'title': info['title'][:60],
                                  'minimized': info['minimized'],
                                  'texts': len(texts)})
        if not a11y_ok and a11y_exposed(texts):
            a11y_ok = True
        if uia.WINDOW_TITLE_KEYWORD in (info['title'] or ''):
            cpquery_win = w
            logged_in = uia.looks_logged_in(w)
            cpquery_usable = uia.cpquery_session_ok(w)
            probe = info['title']
            break  # cpquery 窗是权威探测对象，拿到即止

    payload.update(build_status(chrome_infos, a11y_ok, cpquery_win, logged_in,
                                cpquery_usable))
    if probe:
        payload['probe_window'] = probe
    _finish(payload, out_dir)
    print('[status] ' + payload['message'])
    return 0 if payload['ready'] else 1


def _finish(payload: dict, out_dir: str) -> None:
    payload['finished_at'] = datetime.now().isoformat()
    p = os.path.join(out_dir, 'payload.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print('[status] payload => ' + p)
    cnipa_contract.maybe_emit('status', payload)


# ---------------------------------------------------------------------------
# 离线自测：纯函数，不碰窗口
# ---------------------------------------------------------------------------

def selftest() -> int:
    fails = []

    def check(name, cond):
        print(('[PASS] ' if cond else '[FAIL] ') + name)
        if not cond:
            fails.append(name)

    # a11y 判定
    check('A1) a11y 关闭态（仅琐碎元素）→ False',
          a11y_exposed(['地址和搜索栏', '', 'Chrome Legacy Window']) is False)
    check('A2) a11y 存活态（≥5 个真实文本）→ True',
          a11y_exposed(['发明专利', '共查询到 1 条', '案件编号：', '4W107577', '决定号：38430', '地址和搜索栏']) is True)
    check('A3) 半暴露（3 个）→ False（阈值之下）',
          a11y_exposed(['a', 'b', 'c', '地址和搜索栏']) is False)

    # 状态组装
    s1 = build_status([], False, None, False)
    check('B1) 无 Chrome → chrome_found=False, ready=False, message 含「浏览器」',
          s1['chrome_found'] is False and s1['ready'] is False and '浏览器' in s1['message'])
    s2 = build_status([{'title': 'x', 'minimized': False}], False, None, False)
    check('B2) 有 Chrome 无 cpquery → message 含「查询页」',
          '查询页' in s2['message'])
    s3 = build_status([{'title': 'x', 'minimized': False}], True, None, False)
    check('B3) a11y 好但无 cpquery 前台 → message 含「查询页」',
          '查询页' in s3['message'])
    s2b = build_status([{'title': '中国及多国专利审查信息查询 - Google Chrome', 'minimized': False}],
                       False, object(), False)
    check('B2b) 有 cpquery 但 a11y 关 → message 含「页面内容通道」',
          '页面内容通道' in s2b['message'])
    s4 = build_status([{'title': '中国及多国专利审查信息查询 - Google Chrome', 'minimized': False}],
                      True, object(), False)
    check('B4) 到位但未登录且不可操作 → ready=False',
          s4['ready'] is False and '不可操作' in s4['message'])
    s4g = build_status([{'title': '中国及多国专利审查信息查询 - Google Chrome', 'minimized': False}],
                       True, object(), False, cpquery_usable=True)
    check('B4g) guest 可检索 → ready=True 且无欢迎您',
          s4g['ready'] is True and s4g['logged_in'] is False)
    s5 = build_status([{'title': '中国及多国专利审查信息查询 - Google Chrome', 'minimized': False}],
                      True, object(), True)
    check('B5) 全就绪 → ready=True, success 投影 True',
          s5['ready'] is True and cnipa_contract.clean_status(s5)['success'] is True)
    check('B6) cpquery_guest_ready 识别复审无效表单',
          uia.cpquery_guest_ready(['无效决定库', '申请号/专利号', '查询', 'x'] * 2))

    # 契约投影不泄漏 checks/started_at 等遥测
    proj = cnipa_contract.clean_status(s5)
    check('C1) 投影键 ⊆ 契约白名单（无 checks/started_at/run_dir）',
          not (set(proj) & {'checks', 'started_at', 'run_dir', 'probe_window'}))

    print('[selftest] %s' % ('OK' if not fails else 'FAIL (%d)' % len(fails)))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
