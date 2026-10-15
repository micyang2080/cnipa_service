# -*- coding: utf-8 -*-
"""05 UIA 版：复审/无效决定文书 PDF 下载（免 CDP，真实键鼠驱动用户日常 Chrome）。

业务规则（docs/接口契约.md §3B）：
  · 主键 = application_no + case_no
  · 旧决定：滚到页间横线 → 线上方右键「图片另存为」→ 合并 PDF
  · 新决定：详情已显示则 Ctrl+P 一次另存为完整 PDF（无需翻页）
  · 存储：D:/cnipa_file/{application_no}/{case_no}.pdf

进页（优先复用已开详情，不重造检索链路）：
  ① 前台已是详情页且决定书就绪 → 直接下载
  ② 否则若在结果页 → 点卡片进详情再下（兜底）
  ③ 详情 runner（04）抓字段后也会调用本模块 download_decision_pdf_on_detail

用法：
    python -X utf8 run_cnipa_fetch_decision_file_uia.py <input.json>
    python -X utf8 run_cnipa_fetch_decision_file_uia.py --selftest
input.json：application_no / case_no 必填；library_type / force_refresh / keep_tab 可选
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback

import glob
import hashlib
import tempfile

from PIL import Image
from pywinauto.keyboard import send_keys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_contract  # noqa: E402
import cnipa_cache  # noqa: E402
import cnipa_uia as uia  # noqa: E402
import cnipa_ocr as co  # noqa: E402

# 复用 04 详情 runner 的导航/定位/就绪判定
from run_cnipa_fetch_decision_detail_uia import (  # noqa: E402
    _detail_ready,
    _wait_detail_ready,
    _wait_detail_open,
    _open_detail_from_card_click,
    _wait_doc_ready,
    _nav_drill,
    _classify_state,
    _cleanup_tab,
    _on_results,
    _tab_count,
    _close_gate,
    locate_card_link,
    _link_point,
    translate_inputs,
    DOC_MARKERS,
    doc_marker,
    NAV_X_MAX,
    TYPE_RE,
    PAIR_RE,
)

# 复用 04 附图 runner 的另存为对话框
from run_cnipa_fetch_abstract_figure_uia import (  # noqa: E402
    _find_save_dialog,
    match_save_image_menu,
)

# ---------------------------------------------------------------------------
# 坐标常量（右栏内容区中心点，用于滚动/截图定位）
# ---------------------------------------------------------------------------
# 右栏内容区中心坐标（基于常见 1920×1080 分辨率下 Chrome 最大化布局）。
# 左栏导航区宽度 ~350px，右栏从 ~350px 起始到 ~1920px。
CONTENT_X = 1150  # 右栏内容区 X 中心
# 另存后固定下滚：不 OCR 验「刚存的那条线」；实机约 10 格可把分页线滚出视口
OLD_SCROLL_PAST_NOTCHES = 10
CONTENT_Y = 600   # 右栏内容区 Y 中心（避开地址栏 y<100）

# True=只判新旧并退出（不 Ctrl+P / 不翻页）；确认判断后再改回 False
DETECT_ONLY = False

# ---------------------------------------------------------------------------
# 新旧决定识别
# ---------------------------------------------------------------------------
# 新决定书右栏顶部有翻页控件，OCR 形如 "3 / 11"、"1/5" 等。
# 旧决定是逐页图片无翻页。
_PAGINATION_RE = re.compile(r'(\d+)\s*[//／]\s*(\d+)')
_PAGINATION_OF_RE = re.compile(r'(\d+)\s*of\s*(\d+)', re.I)


_SLASH_TOTAL_RE = re.compile(r'^[//／]\s*(\d+)\s*$')


def _ocr_compact_token(text: str) -> str:
    return re.sub(r'\s+', '', text or '')


def _pag_match_new(text: str):
    """从短文本解析翻页；命中返回 (page, total) 否则 None。

    中文/符号：1/11、1／11；英文 Chrome PDF 工具条：2 of 11。
    """
    raw = (text or '').strip()
    if not raw or len(raw) > 32:
        return None
    m_of = _PAGINATION_OF_RE.search(raw)
    if m_of:
        page_n, total = int(m_of.group(1)), int(m_of.group(2))
        if 1 <= page_n <= total <= 200 and total >= 1:
            return page_n, total
    t = _ocr_compact_token(raw)
    if not t or len(t) > 24:
        return None
    m = _PAGINATION_RE.search(t)
    if m:
        page_n, total = int(m.group(1)), int(m.group(2))
        if 1 <= page_n <= total <= 200 and total >= 1:
            return page_n, total
    m2 = _SLASH_TOTAL_RE.match(t)
    if m2:
        total = int(m2.group(1))
        if 2 <= total <= 200:
            return 1, total
    return None


def _pdf_toolbar_total_from_texts(texts) -> int | None:
    for t in texts or []:
        raw = (t or '').strip()
        m = re.match(r'^[/／]\s*(\d+)\s*$', raw)
        if m:
            return int(m.group(1))
        c = _ocr_compact_token(raw)
        m2 = re.match(r'^[/／](\d+)$', c)
        if m2:
            return int(m2.group(1))
    return None


def _find_pdf_page_edit(win, total_hint: int | None = None):
    """右栏 PDF 工具条页码 Edit（窄、靠上）；返回 (current, edit)。"""
    wr = uia._rect(win)
    y_max = (wr[1] + int((wr[3] - wr[1]) * 0.58)) if wr else 99999
    candidates = []
    try:
        for ed in uia.descendants(win, control_type='Edit'):
            val = (uia.edit_value(ed) or '').strip()
            if not val.isdigit():
                continue
            n = int(val)
            if total_hint is not None:
                if not (1 <= n <= total_hint <= 200):
                    continue
            elif not (1 <= n <= 200):
                continue
            r = uia._rect(ed)
            if r is None or r[0] <= NAV_X_MAX:
                continue
            if r[1] > y_max:
                continue
            w = r[2] - r[0]
            if w > 100:
                continue
            candidates.append((r[1], w, n, ed))
    except Exception:
        return None, None
    if not candidates:
        return None, None
    # 页码框最窄（宽≈30–50px），避免误选其它数字框
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2], candidates[0][3]


def _read_pdf_page_a11y(win) -> tuple[int, int] | None:
    """Chrome 内嵌 PDF 工具条页码（a11y）；滚轮翻页后 Edit 常不刷新，仅作兜底。"""
    texts = uia.all_texts(win) or []
    total_tail = _pdf_toolbar_total_from_texts(texts)
    cur, _ed = _find_pdf_page_edit(win, total_tail)
    if cur is None:
        cur, _ed = _find_pdf_page_edit(win, None)
    if cur is not None and total_tail is not None:
        return cur, total_tail
    for t in texts:
        hit = _pag_match_new(t or '')
        if hit:
            return hit
    joined_compact = _ocr_compact_token(' '.join(texts))
    m = _PAGINATION_RE.search(joined_compact)
    if m:
        page_n, total = int(m.group(1)), int(m.group(2))
        if 1 <= page_n <= total <= 200:
            return page_n, total
    if cur is not None and total_tail is None:
        return cur, cur
    return None


def _read_pdf_page_from_window_png(png_path: str,
                                   crop_save: str | None = None
                                   ) -> tuple[int, int] | None:
    """从整窗截图 OCR PDF 工具条页码（滚轮翻页后 a11y Edit 常仍为 1）。"""
    try:
        with Image.open(png_path) as im:
            ih, iw = im.size
        bits = []
        x_ranges = (
            (max(NAV_X_MAX, int(iw * 0.38)), int(iw * 0.62)),
            (max(NAV_X_MAX, int(iw * 0.35)), min(iw - 20, int(iw * 0.72))),
        )
        y_ranges = ((0.26, 0.42), (0.28, 0.48), (0.30, 0.52))
        for x0, x1 in x_ranges:
            for y0, y1 in y_ranges:
                crop = crop_save or (png_path + '.pag_crop.png')
                with Image.open(png_path) as im:
                    im.crop((x0, int(ih * y0), x1, int(ih * y1))).save(crop)
                for tx, _bx in co.ocr_image(crop):
                    t = (tx or '').strip()
                    if t:
                        bits.append(t)
                    hit = _pag_match_new(t)
                    if hit:
                        return hit
        joined = ' '.join(bits)
        hit = _pag_match_new(joined)
        if hit:
            return hit
        jc = _ocr_compact_token(joined)
        m = _PAGINATION_RE.search(jc)
        if m:
            page_n, total = int(m.group(1)), int(m.group(2))
            if 1 <= page_n <= total <= 200:
                return page_n, total
    except Exception:
        pass
    return None


def _read_pdf_page_ocr(win, out_dir: str) -> tuple[int, int] | None:
    """工具条 OCR 读「1 / 11」（多区域裁剪，覆盖 PDF 控件居中页码）。"""
    os.makedirs(out_dir, exist_ok=True)
    probe = os.path.join(out_dir, '_pag_toolbar.png')
    try:
        win.set_focus()
        uia.screenshot_window(win, probe)
        return _read_pdf_page_from_window_png(
            probe, os.path.join(out_dir, '_pag_toolbar_crop.png'))
    except Exception:
        pass
    return None


def _read_pdf_page_combined(win, out_dir: str | None,
                            step=None,
                            shot_png: str | None = None) -> tuple[int, int] | None:
    """工具条页码：滚屏停页须跟屏上数字 → 优先当前 shot OCR，再整窗 OCR/a11y。"""
    if shot_png and os.path.isfile(shot_png):
        pg = _read_pdf_page_from_window_png(shot_png)
        if pg:
            if step:
                step('pdf_page_read', page=pg, via='shot_ocr')
            return pg
    if out_dir:
        pg_o = _read_pdf_page_ocr(win, out_dir)
        if pg_o:
            if step:
                step('pdf_page_read', page=pg_o, via='ocr')
            return pg_o
    pg_a = _read_pdf_page_a11y(win)
    if pg_a:
        if step:
            step('pdf_page_read', page=pg_a, via='a11y')
        return pg_a
    if step:
        step('pdf_page_read', page=None, via='miss')
    return None


def _ensure_pdf_zoom_percent(win, out_dir: str, step, pct: int = 100) -> bool:
    """内嵌 PDF 缩放改为固定百分比（默认 100%，避免自动缩放大字截不全）。"""
    label = f'{pct}%'
    for t in uia.all_texts(win) or []:
        if (t or '').strip() == label:
            step('pdf_zoom', ok=True, via='already', pct=pct)
            return True
    zoom_ctrl = None
    for ct in ('ComboBox', 'Button', 'Text'):
        for ctrl in uia.descendants(win, control_type=ct):
            n = (uia._name(ctrl) or '').strip()
            if n == label:
                step('pdf_zoom', ok=True, via='on_label', pct=pct)
                return True
            if _looks_like_zoom_label(n):
                zoom_ctrl = ctrl
                break
        if zoom_ctrl is not None:
            break
    if zoom_ctrl is None:
        step('pdf_zoom', ok=False, pct=pct, note='no_zoom_combo')
        return False
    try:
        uia.safe_click(win, zoom_ctrl, settle=0.35, log=step)
        time.sleep(0.55)
    except Exception:
        step('pdf_zoom', ok=False, pct=pct, note='open_menu_fail')
        return False
    for ct in ('ListItem', 'MenuItem', 'Text', 'Button'):
        for c2 in uia.descendants(win, control_type=ct):
            n2 = (uia._name(c2) or '').strip()
            if n2 == label:
                uia.safe_click(win, c2, settle=0.35, log=step)
                time.sleep(0.75)
                step('pdf_zoom', ok=True, pct=pct)
                return True
    try:
        send_keys('{ESC}', pause=0.05)
    except Exception:
        pass
    step('pdf_zoom', ok=False, pct=pct, note='menu_no_pct')
    return False


def _force_pdf_page(win, page: int, out_dir: str, step) -> bool:
    """工具条页码框写入指定页并回车。"""
    texts = uia.all_texts(win) or []
    total = _pdf_toolbar_total_from_texts(texts)
    cur, ed = _find_pdf_page_edit(win, total)
    if ed is None:
        cur, ed = _find_pdf_page_edit(win, None)
    if ed is None:
        step('pdf_page', warn='no_page_edit', want=page)
        return False
    try:
        uia.safe_click(win, ed, settle=0.25, log=step)
        send_keys('^a', pause=0.03)
        send_keys('{DELETE}', pause=0.03)
        send_keys(str(page) + '{ENTER}', pause=0.08)
        time.sleep(0.75)
    except Exception as e:
        step('pdf_page', warn=str(e)[:60], want=page)
        return False
    pg = _read_pdf_page_combined(win, out_dir)
    ok = bool(pg and pg[0] == page)
    step('pdf_page', set=page, ok=ok, read=pg, from_cur=cur)
    return ok


def _pdf_viewer_viewport_hash(win, out_dir: str, tag: str):
    """PDF 正文视口顶区像素哈希（工具栏下方，与滚动位置相关）。"""
    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, f'{tag}.png')
        uia.screenshot_window(win, probe)
        im = Image.open(probe).convert('L')
        w, h = im.size
        left = max(NAV_X_MAX, int(w * 0.32))
        y0 = int(h * 0.36)
        y1 = min(h - 48, y0 + max(120, int(h * 0.20)))
        band = im.crop((left, y0, w - 55, y1))
        return hashlib.md5(band.tobytes()).hexdigest()
    except Exception:
        return None


def _inner_pdf_scrollbar_elem(win):
    """右栏内嵌 PDF 竖条（非浏览器最右条）；取距窗口右缘内缩最大者。"""
    wr = uia._rect(win)
    if wr is None:
        return None
    win_right = wr[2]
    best, best_inset = None, -1
    try:
        for b in uia.descendants(win, control_type='ScrollBar'):
            r = uia._rect(b)
            if r is None:
                continue
            bw, bh = r[2] - r[0], r[3] - r[1]
            if bh < 80 or bw > 48 or bh < bw * 2:
                continue
            if r[0] <= NAV_X_MAX:
                continue
            inset = win_right - r[2]
            if inset < 12:
                continue
            if inset > best_inset:
                best, best_inset = b, inset
    except Exception:
        return None
    return best


def _pdf_scrollbar_thumb_state_uia(win):
    """UIA RangeValue：滑块位置 ratio 0=顶 1=底（Chrome PDF 内嵌条通常可读）。"""
    bar = _inner_pdf_scrollbar_elem(win)
    if bar is None:
        return None, None
    try:
        rv = bar.iface_range_value
        val = float(rv.CurrentValue)
        mn = float(rv.CurrentMinimum)
        mx = float(rv.CurrentMaximum)
        if mx <= mn:
            return None, None
        ratio = (val - mn) / (mx - mn)
        at_top = ratio <= 0.06 or val <= mn + 1.0
        return at_top, round(ratio, 3)
    except Exception:
        pass
    try:
        props = bar.get_properties()
        val = float(props.get('RangeValueValue', props.get('Value', -1)))
        mn = float(props.get('RangeValueMinimum', 0))
        mx = float(props.get('RangeValueMaximum', 100))
        if mx <= mn or val < 0:
            return None, None
        ratio = (val - mn) / (mx - mn)
        return ratio <= 0.06, round(ratio, 3)
    except Exception:
        return None, None


def _pdf_scrollbar_thumb_state_pixels(probe_path: str):
    """像素：在轨道列找较暗滑块块，ratio=滑块顶/轨道高。"""
    try:
        has_bar, inset = _has_inner_doc_scrollbar_pixels(probe_path)
        if not has_bar or inset is None:
            return None, None
        with Image.open(probe_path) as im0:
            im = im0.convert('RGB')
            w, h = im.size
        xc = max(int(w * 0.45), w - inset - 14)
        x0, x1 = max(0, xc - 6), min(w - 1, xc + 8)
        y_start = int(h * 0.33)
        y_end = int(h * 0.90)
        track_h = y_end - y_start
        if track_h < 60:
            return None, None
        row_lum = []
        for y in range(y_start, y_end, 2):
            s, n = 0.0, 0
            for x in range(x0, x1):
                r, g, b = im.getpixel((x, y))
                s += (r + g + b) / 3.0
                n += 1
            row_lum.append((y, s / n if n else 200.0))
        tail = [l for _, l in row_lum[-max(8, len(row_lum) // 5):]]
        baseline = sorted(tail)[len(tail) // 2] if tail else 210.0
        thumb_ys = [y for y, l in row_lum if l < baseline - 10]
        if len(thumb_ys) < 2:
            thumb_ys = [y for y, l in row_lum if 118 <= l <= 188]
        if not thumb_ys:
            return None, None
        thumb_top = min(thumb_ys)
        ratio = (thumb_top - y_start) / float(track_h)
        return ratio <= 0.08, round(ratio, 3)
    except Exception:
        return None, None


def _pdf_scrollbar_thumb_state(win, out_dir: str, step=None):
    """内嵌 PDF 竖条滑块是否在顶。优先 UIA RangeValue，失败再像素。

    返回 (at_top|None, ratio)。
    """
    at, ratio = _pdf_scrollbar_thumb_state_uia(win)
    if at is not None:
        if step:
            step('pdf_thumb', via='uia_range', at_top=at, ratio=ratio)
        return at, ratio
    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, '_pdf_thumb_probe.png')
        uia.screenshot_window(win, probe)
        at, ratio = _pdf_scrollbar_thumb_state_pixels(probe)
        if step:
            step('pdf_thumb', via='pixels', at_top=at, ratio=ratio)
        return at, ratio
    except Exception:
        if step:
            step('pdf_thumb', via='fail', at_top=None, ratio=None)
        return None, None


def _pdf_scrollbar_thumb_at_top(win, out_dir: str, step=None) -> bool | None:
    at_top, _ratio = _pdf_scrollbar_thumb_state(win, out_dir, step=step)
    return at_top


def _pdf_viewer_focus_xy(win):
    wr = uia._rect(win)
    if wr is None:
        return None, None
    cx = _content_click_x(wr)
    cy = max(wr[1] + 220, min(CONTENT_Y, wr[3] - 120))
    return cx, cy


def _pdf_viewer_scroll_at_top(win, out_dir: str, step) -> bool:
    """是否已在顶：滑块到顶 / 页码 1（不再上滚试探，避免已在顶仍空转）。"""
    pg = _read_pdf_page_combined(win, out_dir, step=step)
    thumb, ratio = _pdf_scrollbar_thumb_state(win, out_dir, step=step)
    if pg and pg[0] > 1:
        step('pdf_scroll_top', ok=False, via='page_gt_1', page=pg,
             thumb_ratio=ratio)
        return False
    if thumb is True:
        step('pdf_scroll_top', ok=True, via='scrollbar_thumb', page=pg,
             thumb_ratio=ratio)
        return True
    if pg and pg[0] == 1:
        step('pdf_scroll_top', ok=True, via='page_p1', page=pg,
             thumb_ratio=ratio)
        return True
    if thumb is False:
        step('pdf_scroll_top', ok=False, via='scrollbar_thumb_down', page=pg,
             thumb_ratio=ratio)
        return False
    step('pdf_scroll_top', ok=False, via='unknown', page=pg,
         thumb_ratio=ratio)
    return False


def _scroll_up_until_pdf_top(win, out_dir: str, step,
                             max_rounds: int = 28) -> int:
    """连续上滚直到滑块到顶或页码 1；入口已顶则 0 轮。"""
    if _pdf_viewer_scroll_at_top(win, out_dir, step):
        step('pdf_scroll_up', stopped=True, rounds=0, via='already_at_top')
        return 0
    cx, cy = _pdf_viewer_focus_xy(win)
    if cx is None:
        return 0
    try:
        win.set_focus()
        uia.human_click(win, cx, cy, settle=0.15)
    except Exception:
        pass
    rounds = 0
    for i in range(max_rounds):
        rounds = i + 1
        try:
            uia.scroll_up(win, cx, cy, notches=22, settle=0.35)
        except Exception:
            break
        time.sleep(0.28)
        if _pdf_viewer_scroll_at_top(win, out_dir, step):
            step('pdf_scroll_up', stopped=True, rounds=rounds,
                 via='reached_top')
            return rounds
    step('pdf_scroll_up', stopped=False, rounds=rounds)
    return rounds


def _ensure_pdf_viewer_at_top(win, out_dir: str, step) -> bool:
    """回顶：已在顶则零操作；否则页码→1 / Ctrl+Home / 上滚至顶。"""
    cx, cy = _pdf_viewer_focus_xy(win)
    if cx is None:
        return False
    try:
        win.set_focus()
        uia.human_click(win, cx, cy, settle=0.2)
    except Exception:
        pass
    if _pdf_viewer_scroll_at_top(win, out_dir, step):
        step('pdf_scroll_top', ok=True, action='already_at_top')
        return True
    for phase in range(2):
        pg0 = _read_pdf_page_combined(win, out_dir)
        step('pdf_scroll_top', phase=phase, page_before=pg0)
        if _pdf_viewer_scroll_at_top(win, out_dir, step):
            return True
        if pg0 and pg0[0] > 1:
            _force_pdf_page(win, 1, out_dir, step)
        try:
            send_keys('^{HOME}', pause=0.08)
            time.sleep(0.45)
        except Exception:
            pass
        if _pdf_viewer_scroll_at_top(win, out_dir, step):
            step('pdf_scroll_top', ok=True, action='after_home', phase=phase)
            return True
        n = _scroll_up_until_pdf_top(win, out_dir, step)
        step('pdf_scroll_top', phase=phase, scroll_up_rounds=n)
        if _pdf_viewer_scroll_at_top(win, out_dir, step):
            step('pdf_scroll_top', ok=True, action='recovered', phase=phase)
            return True
    ok = _pdf_viewer_scroll_at_top(win, out_dir, step)
    step('pdf_scroll_top', ok=ok, action='give_up')
    return ok


def _ensure_pdf_viewer_at_page_one(win, out_dir: str, step) -> bool:
    """兼容旧名：以视口回顶为准。"""
    return _ensure_pdf_viewer_at_top(win, out_dir, step)


def _scroll_pdf_viewer_home(win, step, out_dir: str | None = None) -> None:
    """新决定 PDF 阅读器回顶。"""
    od = out_dir or os.path.join(tempfile.gettempdir(), 'cnipa_pdf_home')
    _ensure_pdf_viewer_at_top(win, od, step)


def _looks_like_zoom_label(text: str) -> bool:
    """OCR 常把「自动缩放」读成「+ 动 缩 放」；去空格后认。英文：Automatic Zoom。"""
    raw = (text or '').strip()
    rl = raw.lower()
    if 'automatic zoom' in rl or 'fit to page' in rl or 'actual size' in rl:
        return True
    if 'fit to width' in rl or 'custom' == rl:
        return True
    c = _ocr_compact_token(text)
    if not c:
        return False
    if '自动缩放' in c or '实际大小' in c or '适合页宽' in c:
        return True
    # 缺字/带 +：动缩放、+动缩放、自动缩
    if '动缩放' in c or '动縮放' in c:
        return True
    if '缩放' in c and ('自动' in c or '動' in c or c.startswith('+')):
        return True
    return False


def _has_inner_doc_scrollbar_uia(win) -> bool:
    """UIA：右栏内竖直 ScrollBar，右缘离开窗口右缘（非浏览器条）。"""
    wr = uia._rect(win)
    if wr is None:
        return False
    win_right = wr[2]
    try:
        bars = uia.descendants(win, control_type='ScrollBar')
    except Exception:
        return False
    for b in bars:
        r = uia._rect(b)
        if r is None:
            continue
        bw, bh = r[2] - r[0], r[3] - r[1]
        if bh < 80 or bw > 40 or bh < bw * 3:
            continue
        if r[0] <= NAV_X_MAX:
            continue
        if win_right - r[2] < 8:
            continue
        return True
    return False


def _has_inner_doc_scrollbar_pixels(probe_path: str):
    """截图像素：文档控件内侧灰条（距窗口右缘明显内缩）；浏览器条贴最右。

    返回 (True, inset_px) 或 (False, None)。
    """
    try:
        with Image.open(probe_path) as im0:
            im = im0.convert('RGB')
            w, h = im.size
        ys = range(int(h * 0.32), int(h * 0.88), 2)
        # 右半区找连续竖向灰轨；inset>=40 才算内嵌（非浏览器）
        run_x0 = run_x1 = None
        best = None  # (width, inset, x0)
        for x in range(int(w * 0.45), w - 1):
            gray = 0
            n = 0
            for y in ys:
                r, g, b = im.getpixel((x, y))
                n += 1
                if abs(r - g) < 15 and abs(g - b) < 15 and 165 <= r <= 235:
                    gray += 1
            hit = n > 0 and (gray / n) >= 0.55
            if hit:
                if run_x0 is None:
                    run_x0 = run_x1 = x
                else:
                    run_x1 = x
            else:
                if run_x0 is not None:
                    inset = (w - 1) - run_x1
                    width = run_x1 - run_x0 + 1
                    if 2 <= width <= 28 and inset >= 40:
                        cand = (width, inset, run_x0)
                        if best is None or cand[1] > best[1]:
                            best = cand
                    run_x0 = run_x1 = None
        if run_x0 is not None:
            inset = (w - 1) - run_x1
            width = run_x1 - run_x0 + 1
            if 2 <= width <= 28 and inset >= 40:
                cand = (width, inset, run_x0)
                if best is None or cand[1] > best[1]:
                    best = cand
        if best:
            return True, best[1]
    except Exception:
        pass
    return False, None


def _pdf_zero_slash_zero(text: str) -> bool:
    t = _ocr_compact_token(text or '')
    return bool(t and re.fullmatch(r'0[/／]0', t))


def _pdf_toolbar_is_zero_zero(win) -> bool:
    """工具条页码是否为 0/0（含 Edit=0 + Text「/ 0」拆分）。"""
    texts = uia.all_texts(win) or []
    for t in texts:
        if _pdf_zero_slash_zero(t):
            return True
    joined = ' '.join(texts)
    if re.search(r'0\s*[/／]\s*0', joined):
        return True
    # Chrome：当前页在 Edit，总数常为单独「/ 0」或「/0」
    has_slash_zero = any(
        re.fullmatch(r'[/／]\s*0', (t or '').strip())
        or _ocr_compact_token(t) in ('/0', '／0')
        for t in texts)
    if not has_slash_zero:
        return False
    try:
        for ed in uia.descendants(win, control_type='Edit'):
            val = (uia.edit_value(ed) or '').strip()
            if val == '0':
                r = uia._rect(ed)
                if r is not None and r[0] > NAV_X_MAX:
                    return True
    except Exception:
        pass
    return False


def _pdf_load_failed(win) -> bool:
    """框已出但 PDF 未载入：红条「载入 PDF 时发生错误」或页码 0/0。"""
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    compact = _ocr_compact_token(joined)
    if '载入PDF时发生错误' in compact:
        return True
    if '载入PDF' in compact and '错误' in compact:
        return True
    if '发生错误' in joined and ('PDF' in joined or 'pdf' in joined.lower()):
        return True
    return _pdf_toolbar_is_zero_zero(win)


def _pdf_has_real_pages(win) -> bool:
    """工具条为真实页码（total≥1），排除 0/0。"""
    for t in uia.all_texts(win) or []:
        hit = _pag_match_new(t or '')
        if hit and hit[0] >= 1 and hit[1] >= 1:
            return True
    return False


def _pdf_viewer_visible_light(win) -> bool:
    """右栏 PDF **正文**已渲染：真实页码/锚词/大页图。

    仅有「自动缩放」不够——载入失败时工具条仍在、页码 0/0。
    """
    if _pdf_load_failed(win):
        return False
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    if _pdf_has_real_pages(win):
        return True
    for m in DOC_MARKERS:
        if m in joined:
            return True
    if any(k in joined for k in ('一、案由', '、案由', '二、决定')):
        return True
    for c in _list_right_page_images(win):
        if c.get('h', 0) >= 320:
            return True
    return False


def _detail_shell_empty(win) -> bool:
    """详情壳在但右栏 PDF 未出（仅顶栏/案号 chip、大空白）。"""
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    if '共查询到' in joined:
        return False
    on_detail = (
        '国内案件信息详情' in joined
        or _detail_ready(texts)
        or '申请号/专利号' in joined
    )
    if not on_detail:
        return False
    return not _pdf_viewer_visible_light(win)


def _pdf_viewer_visible(win, out_dir: str) -> bool:
    if _pdf_load_failed(win):
        return False
    if _pdf_viewer_visible_light(win):
        return True
    probe = os.path.join(out_dir, '_viewer_probe.png')
    try:
        uia.screenshot_window(win, probe)
        if doc_marker(co.ocr_image(probe)):
            return True
        with Image.open(probe) as im:
            ih = im.size[1]
        y_max = max(200, int(ih * 0.55))
        found = _ocr_find_new_markers(co.ocr_image(probe), y_max)
        # 仅认真实页码 OCR，不认单独的「自动缩放」
        if found and str(found[0]).startswith('pag'):
            return True
    except Exception:
        pass
    return False


def _refresh_pdf_viewer(win, step, why: str) -> None:
    try:
        win.set_focus()
    except Exception:
        pass
    step('wait_pdf_viewer', action='browser_refresh', why=why)
    uia.safe_send_keys('^r', win=win, pause=0.05, log=step)
    time.sleep(5.0)


def _wait_pdf_viewer_ready(win, out_dir: str, step,
                           timeout_s: float = 45.0) -> bool:
    """等右栏 PDF 正文就绪；框出但载入失败/空壳 → 刷新再等（勿滚轮）。"""
    refreshed = 0
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if _pdf_load_failed(win):
            if refreshed < 2:
                _refresh_pdf_viewer(win, step, 'pdf_load_error')
                refreshed += 1
                # 刷新后慢转圈常见：多等一会再判失败
                time.sleep(3.0)
                continue
            step('wait_pdf_viewer', ok=False, why='pdf_load_error',
                 refreshed=refreshed, after=round(time.time() - t0, 1))
            return False
        if _pdf_viewer_visible(win, out_dir):
            step('wait_pdf_viewer', ok=True,
                 after=round(time.time() - t0, 1), refreshed=refreshed)
            return True
        elapsed = time.time() - t0
        # 空壳/慢空白：优先刷新，禁止靠滚轮「找」PDF
        if refreshed < 2 and elapsed >= 5.0 and (
                _detail_shell_empty(win) or refreshed == 0 and elapsed >= 10.0):
            why = 'shell_empty' if _detail_shell_empty(win) else 'slow_blank'
            _refresh_pdf_viewer(win, step, why)
            refreshed += 1
            time.sleep(2.0)
            continue
        time.sleep(1.2)
    step('wait_pdf_viewer', ok=False, refreshed=refreshed,
         after=round(time.time() - t0, 1))
    return False


def _texts_has_load_failed(texts) -> bool:
    return any('加载失败' in (t or '') for t in (texts or []))


def _wait_decision_pages_loaded(win, step, budget_s: float = 20.0) -> bool:
    """旧决定分页图懒加载；页间「加载失败」会改变空白高度，干扰找分页线。"""
    refreshed = False
    t0 = time.time()
    while time.time() - t0 < budget_s:
        texts = uia.all_texts(win) or []
        if not _texts_has_load_failed(texts):
            step('wait_page_images', ok=True,
                 after=round(time.time() - t0, 1), refreshed=refreshed)
            return True
        elapsed = time.time() - t0
        step('wait_page_images', note='加载失败', after=round(elapsed, 1),
             refreshed=refreshed)
        if not refreshed and elapsed >= 5.0:
            try:
                win.set_focus()
            except Exception:
                pass
            uia.safe_send_keys('^r', win=win, pause=0.05, log=step)
            time.sleep(4.0)
            refreshed = True
            continue
        time.sleep(1.0)
    step('wait_page_images', ok=False, after=round(time.time() - t0, 1),
         refreshed=refreshed)
    return False


def _ocr_find_new_markers(items, y_max: int):
    """从 OCR 结果找页码/缩放；命中返回 (via, detail) 否则 None。"""
    bits = []
    for tx, bx in items:
        if bx[0] <= NAV_X_MAX or bx[1] >= y_max:
            continue
        t = (tx or '').strip()
        if not t or len(t) > 24:
            continue
        bits.append(t)
        if _looks_like_zoom_label(t):
            return 'zoom_ocr', t
        hit = _pag_match_new(t)
        if hit:
            return 'pag_ocr', f'{hit[0]}/{hit[1]}:{t}'
    joined = ' '.join(bits)
    if _looks_like_zoom_label(joined):
        return 'zoom_ocr_join', _ocr_compact_token(joined)[:24]
    hit = _pag_match_new(joined)
    if hit:
        return 'pag_ocr_join', f'{hit[0]}/{hit[1]}'
    return None


def _detect_decision_type(win, out_dir: str, step) -> str:
    """页内识别新决定证据；无证据返回 unknown（不默认 old）。

    新：页码（1/11）、「自动缩放」、文档内侧滚动条（a11y→截屏 OCR→滚动条）。
    无上述证据 → unknown，由调用方用列表发文日补充或失败。
    """
    probe = os.path.join(out_dir, 'detect_type.png')
    try:
        win.set_focus()
        time.sleep(0.3)

        # —— ① 先认页码 /「自动缩放」（a11y）——
        texts = uia.all_texts(win) or []
        for t in texts:
            raw = t or ''
            if _looks_like_zoom_label(raw):
                step('detect_type', kind='new', via='zoom_a11y',
                     text=raw.strip()[:24])
                return 'new'
            hit = _pag_match_new(raw)
            compact = _ocr_compact_token(raw)
            if hit and re.fullmatch(r'\d+[//／]\d+', compact):
                step('detect_type', kind='new', pagination=raw.strip(),
                     page=hit[0], total=hit[1], via='a11y')
                return 'new'
            if _SLASH_TOTAL_RE.match(compact):
                hit = _pag_match_new(raw)
                if hit:
                    step('detect_type', kind='new', pagination=raw.strip(),
                         total=hit[1], via='slash_a11y')
                    return 'new'

        # —— ① OCR 截图认页码/缩放 ——
        uia.screenshot_window(win, probe)
        with Image.open(probe) as im:
            ih, iw = im.size[1], im.size[0]
        y_max = max(200, int(ih * 0.55))
        found = _ocr_find_new_markers(co.ocr_image(probe), y_max)
        if found:
            via, detail = found
            step('detect_type', kind='new', via=via, detail=detail)
            return 'new'

        # 工具条窄带二次 OCR（整窗常漏「1 / 11」）
        try:
            y0, y1 = int(ih * 0.28), int(ih * 0.48)
            x0 = min(iw - 40, max(NAV_X_MAX, int(iw * 0.35)))
            crop = os.path.join(out_dir, 'detect_type_toolbar.png')
            with Image.open(probe) as im:
                im.crop((x0, y0, iw, y1)).save(crop)
            crop_items = [(tx, (bx[0] + x0, bx[1] + y0, bx[2] + x0, bx[3] + y0))
                          for tx, bx in co.ocr_image(crop)]
            # 窄带内不再用 y_max 滤掉
            bits = []
            for tx, _bx in crop_items:
                t = (tx or '').strip()
                if t:
                    bits.append(t)
                if _looks_like_zoom_label(t):
                    step('detect_type', kind='new', via='zoom_crop', text=t)
                    return 'new'
                hit = _pag_match_new(t)
                if hit:
                    step('detect_type', kind='new', pagination=t,
                         page=hit[0], total=hit[1], via='pag_crop')
                    return 'new'
            cj = ' '.join(bits)
            if _looks_like_zoom_label(cj) or _pag_match_new(cj):
                step('detect_type', kind='new', via='toolbar_crop',
                     sample=_ocr_compact_token(cj)[:40])
                return 'new'
        except Exception as e:
            step('detect_type', warn=f'toolbar_crop: {e}')

        # —— ② 页码认不出 → 用滚动条位置 ——
        if _has_inner_doc_scrollbar_uia(win):
            step('detect_type', kind='new', via='inner_scrollbar_uia')
            return 'new'
        ok_pix, inset = _has_inner_doc_scrollbar_pixels(probe)
        if ok_pix:
            step('detect_type', kind='new', via='inner_scrollbar_px',
                 inset=inset)
            return 'new'
        if _detail_shell_empty(win):
            step('detect_type', warn='右栏空白 → unknown')
            return 'unknown'
        # 无新决定证据时不默认 old：OCR/a11y 漏检会把新决定错判成旧；
        # 交由 resolve_decision_type_kind（列表发文日）再补，仍空则失败。
        step('detect_type', kind='unknown',
             note='页码/缩放/内嵌滚动条均未识别')
        return 'unknown'
    except Exception as e:
        step('detect_type', warn=f'检测异常 → unknown: {e}')
        return 'unknown'


# ---------------------------------------------------------------------------
# 旧决定路径：右键「图片另存为」逐页原图 → 合并 PDF
# （视口截图会截半页；另存为可得完整页图，对齐 image/1.png≈2480×3508）
# ---------------------------------------------------------------------------

def _list_right_page_images(win) -> list:
    """右栏决定书整页 Image（过滤 icon/左导航）。按 y 排序。"""
    out = []
    for el in uia.descendants(win, control_type='Image'):
        r = uia._rect(el)
        if r is None:
            continue
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 280 or h < 280:
            continue
        if r[0] < NAV_X_MAX - 20:
            continue
        out.append({'rect': r, 'w': w, 'h': h, 'y': r[1],
                    'cx': (r[0] + r[2]) // 2, 'cy': (r[1] + r[3]) // 2})
    out.sort(key=lambda c: (c['y'], c['rect'][0]))
    return out


# 新页页眉（1→2 页常见）；章节标题（2→3 常见「一、案由」）
_NEW_PAGE_PRIMARY = ('无效宣告请求审查决定', '复审请求审查决定')
_NEW_PAGE_SECONDARY = ('国家知识产权局专利复审委员会',)
_NEW_PAGE_SECTION = ('一、案由', '二、决定', '三、决定')
# 写在分页空白里的小字注释（可缩小空白观感，但仍算页间区）
_PAGE_GAP_NOTES = ('回函请寄', '纸件申请', '电子申请', '蓟门桥', '西土城路')
# 上一页结尾（真分页线上沿常见）。不含「专利复审委员会」：
# 第2页页眉「国家知识产权局专利复审委员会」会误命中。
_PAGE_END_ABOVE = (
    '合议组组长', '主审员', '参审员', '合议组',
) + _PAGE_GAP_NOTES
# 第1页封面字段：只认线上方（第2页页眉也有「案件编号」，线下出现不算封面）
_COVER_NEAR_LABELS = (
    '发文日', '发文', '申请号或专利号', '申请号', '发文序号',
    '发明创造名称', '专利权人', '无效宣告请求人',
)
# 仅当横线下方出现「短标签行」时排除（表头）；不含「案件编号」（第2页页眉常见）
_DIVIDER_FALSE_LABELS = (
    '发文日', '发文', '申请号或专利号', '发文序号', '发明创造名称',
    '国际分类号', '法律依据', '决定要点', '无效宣告请求日', '授权公告日',
    '优先权日', '专利权人', '无效宣告请求人',
)


def _ocr_compact(text: str) -> str:
    return re.sub(r'\s+', '', text or '')


def _band_empty_ratio(px, bw: int, bh: int, y0: int, y1: int) -> float:
    step = max(1, bw // 80)
    white = tot = 0
    for y in range(max(0, y0), min(bh, y1)):
        for x in range(0, bw, step):
            tot += 1
            if px[x, y] >= 230:
                white += 1
    return white / tot if tot else 0.0


def _band_ink_ratio(px, bw: int, bh: int, y0: int, y1: int) -> float:
    step = max(1, bw // 80)
    ink = tot = 0
    for y in range(max(0, y0), min(bh, y1)):
        for x in range(0, bw, step):
            tot += 1
            if px[x, y] < 200:
                ink += 1
    return ink / tot if tot else 0.0


def _new_page_header_below(norms: list, line_y: int) -> bool:
    """横线下方是否出现新页页眉/章节标题。

    页间线常夹在两段空白中间，「一、案由」可能在线下 200px+。
    """
    below = [(t, ty) for t, ty in norms if line_y + 8 < ty < line_y + 320]
    if not below:
        return False
    blob = ''.join(t for t, _ in below)
    if any(m in blob for m in _NEW_PAGE_PRIMARY):
        return True
    if any(m in blob for m in _NEW_PAGE_SECTION):
        return True
    for t, _ in below:
        if t in ('、案由', '案由', '、决定') or (
                t.endswith('、案由') and len(t) <= 12):
            return True
    if any(k in blob for k in _PAGE_GAP_NOTES):
        return False
    if any(m in blob for m in _NEW_PAGE_SECONDARY):
        near = ''.join(t for t, ty in norms if abs(ty - line_y) < 90)
        if '发文日' in near or '申请号或专利号' in near:
            return False
        return True
    return False


def _false_table_label_below(norms: list, line_y: int) -> bool:
    """横线下方短标签（表头字段）→ 表线，非页间线。长段落里的词忽略。"""
    for t, ty in norms:
        if not (line_y + 8 < ty < line_y + 70):
            continue
        if len(t) > 28:
            continue
        if any(f in t for f in _DIVIDER_FALSE_LABELS):
            return True
    return False


def _short_labels_above(norms: list, line_y: int, labels: tuple,
                        y_off: int = 200, maxlen: int = 40) -> bool:
    for t, ty in norms:
        if not (line_y - y_off < ty < line_y - 6):
            continue
        if len(t) > maxlen:
            continue
        if any(f in t for f in labels):
            return True
    return False


def _gap_above_ok(px, bw, bh, band_y: int, norms: list, line_y: int) -> bool:
    """线上方大块空白；空白里可有小字注释。"""
    empty = _band_empty_ratio(px, bw, bh, band_y - 110, band_y - 6)
    if empty >= 0.84:
        return True
    above = ''.join(t for t, ty in norms if line_y - 120 < ty < line_y - 6)
    has_notes = any(k in above for k in _PAGE_GAP_NOTES)
    if has_notes and empty >= 0.45:
        return True
    return False


_BELOW_UI_NOISE = (
    '全部显示', 'savetmp', 'fetch-file', '另存为', '保存', '取消',
    '.png', '.jpg', 'dec_page', 'divider_probe',
)


def _immediate_white_above(px, bw: int, bh: int, band_y: int) -> bool:
    """线上方紧邻约 50px 几乎全白（真分页）；表框底边上方贴正文则否。"""
    return _band_empty_ratio(px, bw, bh, band_y - 55, band_y - 3) >= 0.90


def _content_below_ok(px, bw: int, bh: int, band_y: int,
                      norms: list, line_y: int) -> bool:
    """线下有下一页正文；允许中间再隔一段空白（线夹在页间空白中）。"""
    if _band_ink_ratio(px, bw, bh, band_y + 6, band_y + 70) >= 0.035:
        return True
    # 线下先白后文：在更远处找第一段墨迹
    for off in range(50, 300, 15):
        if _band_ink_ratio(px, bw, bh, band_y + off,
                           band_y + off + 40) >= 0.04:
            return True
    for t, ty in norms:
        if not (line_y + 8 < ty < line_y + 320):
            continue
        tl = t.lower()
        if any(n.lower() in tl or n in t for n in _BELOW_UI_NOISE):
            continue
        if any(k in t for k in _NEW_PAGE_SECTION + ('、案由', '反证', '证据')):
            return True
        if len(t) >= 6:
            return True
    return False


def _row_is_hline(px, bw: int, y: int) -> bool:
    """页间横线：加粗黑线、细黑线或空白中的淡灰线。"""
    step = max(1, bw // 100)
    vals = [px[x, y] for x in range(0, bw, step)]
    if not vals:
        return False
    n = len(vals)
    avg = sum(vals) / n
    var = sum((v - avg) ** 2 for v in vals) / n
    if sum(1 for v in vals if v < 70) / n > 0.45:
        return True
    # 细黑/深灰（抗锯齿时 var 偏高）
    span = sum(1 for v in vals if v < 170) / n
    if span >= 0.55 and var < 1600 and 70 <= avg <= 175:
        return True
    if span >= 0.82 and avg <= 145:
        return True
    return False


def _cover_fields_near(norms: list, line_y: int, dist: int = 160) -> bool:
    """线上方短标签为发文日等 → 首页抬头。长句里的「专利权人」不算。"""
    for t, ty in norms:
        if not (line_y - dist < ty <= line_y + 20):
            continue
        if len(t) > 22:
            continue
        if any(f in t for f in _COVER_NEAR_LABELS):
            return True
        if '发文' in t and len(t) <= 12:
            return True
    return False


def _logo_header_above(norms: list, line_y: int) -> bool:
    """线上方短距内是首页「国家知识产权局」抬头（非页末落款）。"""
    above = ''.join(t for t, ty in norms if line_y - 140 < ty < line_y - 4)
    if '国家知识产权局' not in above:
        return False
    # 页末「专利复审委员会」旁偶发局名残片时，有合议组则不算抬头
    if any(k in above for k in ('合议组', '主审员', '参审员', '回函请寄', '纸件申请')):
        return False
    return True


def _notes_immediately_below(norms: list, line_y: int) -> bool:
    """线下近处先是页末小字（细线），不是分页粗线。"""
    near = ''.join(t for t, ty in norms if line_y + 8 < ty < line_y + 90)
    if not any(k in near for k in _PAGE_GAP_NOTES):
        return False
    if any(m in near for m in _NEW_PAGE_PRIMARY + _NEW_PAGE_SECONDARY):
        return False
    return True


def _classic_page1_to_2(norms: list, line_y: int) -> bool:
    """线→注释→空白→真分页线：注释/合议组可在线上较远处；线下是新页眉。"""
    if _notes_immediately_below(norms, line_y):
        return False
    # 空白较大时注释距线可超过 200px
    end_above = _short_labels_above(
        norms, line_y, _PAGE_END_ABOVE, y_off=360, maxlen=80)
    if not end_above:
        return False
    below = ''.join(t for t, ty in norms if line_y + 8 < ty < line_y + 320)
    return any(m in below for m in _NEW_PAGE_PRIMARY + _NEW_PAGE_SECONDARY)


def _page_start_below(norms: list, line_y: int) -> bool:
    below = ''.join(t for t, ty in norms if line_y + 8 < ty < line_y + 320)
    return any(m in below for m in (
        _NEW_PAGE_PRIMARY + _NEW_PAGE_SECONDARY + _NEW_PAGE_SECTION))


def _section_start_below(norms: list, line_y: int) -> bool:
    """线下是「一、案由」等章节标题（第2→3页常见，OCR 常丢「一」）。"""
    for t, ty in norms:
        if not (line_y + 8 < ty < line_y + 120):
            continue
        if t in ('、案由', '案由', '、决定') or any(
                m in t for m in _NEW_PAGE_SECTION):
            return True
        if t.endswith('、案由') and len(t) <= 12:
            return True
    return False


def _is_page_divider(y: int, uniq: list, px, bw: int, bh: int,
                     norms: list) -> bool:
    """真分页线判定。

    认：第1→2（注释/合议组+新页眉）、第2→3（空白+一、案由）、空白上沿+正文。
    拒：发文日抬头、注释正下方细线、表框底边。
    """
    if _classic_page1_to_2(norms, y):
        return True
    if _notes_immediately_below(norms, y):
        return False
    if _cover_fields_near(norms, y) or _logo_header_above(norms, y):
        return False
    if _false_table_label_below(norms, y):
        return False
    band_y = y - 100
    touch = _band_ink_ratio(px, bw, bh, band_y - 30, band_y - 3) >= 0.04
    imm = _immediate_white_above(px, bw, bh, band_y)
    gap = imm or _gap_above_ok(px, bw, bh, band_y, norms, y)
    if touch and not imm:
        return False
    if not gap:
        return False
    if not _content_below_ok(px, bw, bh, band_y, norms, y):
        return False
    # 章节分页：空白上沿 +「一、案由」即可，不要求合议组
    if _section_start_below(norms, y):
        return True
    # 复审委/决定书标题在下：须有页末痕迹，防首页内标题上沿
    if _new_page_header_below(norms, y):
        end_above = _short_labels_above(
            norms, y, _PAGE_END_ABOVE, y_off=360, maxlen=80)
        if not end_above:
            return False
    if sum(1 for z in uniq if 0 < abs(z - y) < 55) >= 2:
        return False
    return True


def _divider_ok_as_first_page(y: int, norms: list, px, bw: int, bh: int) -> bool:
    """尚未存页：只认第1→2页边界，不把首页抬头/合议组区当末页。"""
    if _classic_page1_to_2(norms, y):
        return True
    if _notes_immediately_below(norms, y):
        return False
    if _cover_fields_near(norms, y) or _logo_header_above(norms, y):
        return False
    # 首页视口上沿假线直接丢（真分页在滚过抬头之后）
    if y < 100 + int(bh * 0.28):
        return False
    band_y = y - 100
    if not (_immediate_white_above(px, bw, bh, band_y)
            or _gap_above_ok(px, bw, bh, band_y, norms, y)):
        return False
    # 必须像下页开头；禁止「仅有合议组在上」就放行
    if not _page_start_below(norms, y):
        return False
    end_above = _short_labels_above(
        norms, y, _PAGE_END_ABOVE, y_off=360, maxlen=80)
    return bool(end_above)


def _divider_ocr_norms(png_path: str) -> tuple:
    """找线用 OCR：右栏裁切后走全局 OCR（config 默认 MinerU）。

    返回 (norms, meta)；norms 为 [(text, ty_img)]，ty 与 _find_divider 内 y 同坐标系。
    """
    meta = {'ocr': 'skip', 'ocr_n': 0, 'ocr_err': '', 'crop': False}
    try:
        im = Image.open(png_path).convert('RGB')
    except Exception as e:
        meta['ocr'] = 'fail'
        meta['ocr_err'] = str(e)[:120]
        return [], meta
    w, h = im.size
    left = max(0, min(NAV_X_MAX, w // 3, w - 200))
    x0 = left + (w - left) // 10
    x1 = w - (w - left) // 10
    y0 = 100
    y1 = max(y0 + 80, h - 40)
    crop = im.crop((x0, y0, x1, y1))
    crop_path = png_path + '._divider_ocr.png'
    try:
        crop.save(crop_path)
        meta['crop'] = True
        norms = []
        for tx, bx in co.ocr_image(crop_path):
            t = _ocr_compact(tx)
            if t:
                ty = (bx[1] + bx[3]) // 2 + y0
                norms.append((t, ty))
        meta['ocr'] = 'ok' if norms else 'empty'
        meta['ocr_n'] = len(norms)
        return norms, meta
    except Exception as e:
        meta['ocr'] = 'fail'
        meta['ocr_err'] = str(e)[:160]
        return [], meta


def _find_divider_screen_ys(png_path: str, win_rect,
                            first_page: bool = False,
                            *, ocr_validate: bool = True) -> list:
    """截图右栏找页间分隔横线，返回屏幕 y（升序）。

    ocr_validate=False：仅像素判线；默认 True 时对右栏内容带裁切后 OCR。
    """
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return []
    w, h = im.size
    left = max(0, min(NAV_X_MAX, w // 3, w - 200))
    x0 = left + (w - left) // 10
    x1 = w - (w - left) // 10
    band = im.crop((x0, 100, x1, h - 40))
    bw, bh = band.size
    px = band.load()
    dark_rows = []
    for y in range(bh):
        if _row_is_hline(px, bw, y):
            dark_rows.append(y + 100)
    lines = []
    if dark_rows:
        run = [dark_rows[0]]
        for y in dark_rows[1:]:
            if y - run[-1] <= 5:
                run.append(y)
            else:
                lines.append(sum(run) // len(run))
                run = [y]
        lines.append(sum(run) // len(run))
    uniq = []
    for y in lines:
        if not uniq or y - uniq[-1] >= 80:
            uniq.append(y)
    norms = []
    ocr_meta = {'ocr': 'skip', 'ocr_n': 0, 'ocr_err': ''}
    if ocr_validate:
        norms, ocr_meta = _divider_ocr_norms(png_path)
    # 供调用方 step 打点（勿在 OCR 空时擅自改成纯像素验线）
    _find_divider_screen_ys.last_ocr_meta = ocr_meta  # type: ignore[attr-defined]
    validated = []
    for y in uniq:
        if ocr_validate:
            if not _is_page_divider(y, uniq, px, bw, bh, norms):
                continue
            if first_page and not _divider_ok_as_first_page(y, norms, px, bw, bh):
                continue
        else:
            band_y = y - 100
            if not (_immediate_white_above(px, bw, bh, band_y)
                    or _gap_above_ok(px, bw, bh, band_y, norms, y)):
                continue
            if not _content_below_ok(px, bw, bh, band_y, norms, y):
                continue
        validated.append(y)
    return [win_rect[1] + y for y in validated]


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _clipboard_set_text(text: str) -> bool:
    """写入剪贴板（另存为用粘贴，避免 send_keys 吞 \\u 等转义）。"""
    try:
        import ctypes
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return False
        user32.EmptyClipboard()
        data = str(text).encode('utf-16-le') + b'\x00\x00'
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            user32.CloseClipboard()
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        user32.CloseClipboard()
        return True
    except Exception:
        return False


def _foreground_uia_window():
    """前台窗 UIA 包装；失败 None。禁止 Desktop.windows 全扫。"""
    try:
        import ctypes
        h = ctypes.windll.user32.GetForegroundWindow()
        return uia.uia_window_from_hwnd(h) if h else None
    except Exception:
        return None


def _window_looks_overwrite(w) -> bool:
    try:
        title = (w.window_text() or '').strip()
    except Exception:
        title = ''
    texts = ''
    try:
        texts = ' '.join(
            uia._name(t) or ''
            for t in uia.descendants(w, control_type='Text')[:40])
    except Exception:
        pass
    blob = title + ' ' + texts
    bl = blob.lower()
    return (
        '确认另存为' in title
        or 'confirm save as' in title.lower()
        or '已存在' in blob
        or '要替换' in blob
        or 'already exists' in bl
        or 'replace it' in bl
    )


def _click_overwrite_yes_on(w) -> bool:
    try:
        for b in uia.descendants(w, control_type='Button'):
            bn = (uia._name(b) or '').strip()
            bnl = bn.lower()
            if (bn.startswith('是') or bnl in ('yes', 'y')
                    or '是(' in bn or bn == 'Yes'):
                if _mouse_click_elem(b):
                    time.sleep(0.2)
                    return True
        send_keys('y', pause=0.05)
        time.sleep(0.2)
        return True
    except Exception:
        return False


def _dismiss_overwrite_confirm() -> bool:
    """「要替换它吗？」→ 点「是」。只查前台窗，禁止 Desktop 全扫（曾 30～47s/次）。"""
    w = _foreground_uia_window()
    if w is None:
        return False
    try:
        if not _window_looks_overwrite(w):
            return False
        return _click_overwrite_yes_on(w)
    except Exception:
        return False


def _dismiss_filename_invalid() -> None:
    """关掉另存为附属提示；只查前台，禁止 Desktop 全扫。"""
    if _dismiss_overwrite_confirm():
        return
    w = _foreground_uia_window()
    if w is None:
        return
    try:
        title = w.window_text() or ''
    except Exception:
        return
    if '另存为' not in title and 'Save' not in title and '保存' not in title:
        return
    try:
        texts = ' '.join(
            uia._name(t) or ''
            for t in uia.descendants(w, control_type='Text')[:40])
        tl = texts.lower()
        ok_alert = (
            '文件名无效' in texts
            or '路径不存在' in texts
            or 'invalid' in tl
            or 'does not exist' in tl
        )
        if not ok_alert:
            return
        for b in uia.descendants(w, control_type='Button'):
            bn = uia._name(b) or ''
            if '确定' in bn or bn.lower() in ('ok', 'o'):
                _mouse_click_elem(b)
                time.sleep(0.2)
                return
        send_keys('{ENTER}', pause=0.05)
    except Exception:
        pass


def _mouse_click_elem(elem) -> bool:
    """按屏幕坐标点控件（不 set_focus，避免另存为丢焦点）。"""
    r = uia._rect(elem)
    if not r:
        return False
    from pywinauto.mouse import click as _mouse_click
    _mouse_click(button='left',
                 coords=((r[0] + r[2]) // 2, (r[1] + r[3]) // 2))
    time.sleep(0.25)
    return True


def _fname_edit(dlg):
    """另存为「文件名」编辑框：取最靠下的 Edit。"""
    edits = uia.descendants(dlg, control_type='Edit')
    if not edits:
        return None
    return max(edits, key=lambda e: (uia._rect(e) or (0, 0, 0, 0))[1])


def _set_edit_text(elem, text: str) -> bool:
    """UIA SetValue 优先；失败再剪贴板粘贴。"""
    text = str(text)
    try:
        elem.iface_value.SetValue(text)
        time.sleep(0.15)
        if text.lower() in (uia.edit_value(elem) or '').lower():
            return True
    except Exception:
        pass
    try:
        r = uia._rect(elem)
        if r:
            from pywinauto.mouse import click as _mouse_click
            _mouse_click(button='left',
                         coords=((r[0] + r[2]) // 2, (r[1] + r[3]) // 2))
            time.sleep(0.15)
        send_keys('^a', pause=0.02)
        send_keys('{DELETE}', pause=0.03)
        if _clipboard_set_text(text):
            send_keys('^v', pause=0.05)
            time.sleep(0.2)
            cur = (uia.edit_value(elem) or '')
            if os.path.basename(text).lower() in cur.lower() or text.lower() in cur.lower():
                return True
    except Exception:
        pass
    return False


def _click_save_button(dlg) -> bool:
    """点「保存」；排除「取消」。"""
    buttons = []
    for b in uia.descendants(dlg, control_type='Button'):
        n = (uia._name(b) or '').strip()
        nl = n.lower()
        if '取消' in n or 'cancel' in nl:
            continue
        if '保存' in n or nl == 'save' or nl.startswith('save'):
            buttons.append((0 if n.startswith('保存') else 1, b))
    buttons.sort(key=lambda x: x[0])
    for _, b in buttons:
        if _mouse_click_elem(b):
            return True
    send_keys('{ENTER}', pause=0.05)
    return True


def _close_save_dialog() -> None:
    """确保另存为/无效名提示都关掉。"""
    _dismiss_filename_invalid()
    try:
        send_keys('{ESC}', pause=0.05)
    except Exception:
        pass
    time.sleep(0.2)
    dlg = _find_save_dialog(timeout_s=0.6)
    if dlg is None:
        return
    for b in uia.descendants(dlg, control_type='Button'):
        n = (uia._name(b) or '').lower()
        if '取消' in (uia._name(b) or '') or 'cancel' in n:
            _mouse_click_elem(b)
            time.sleep(0.2)
            break
    try:
        send_keys('{ESC}', pause=0.05)
    except Exception:
        pass


def _fill_and_confirm_save(dlg, target: str, step) -> bool:
    """写入完整路径并点保存；已存在则先删或点「是」覆盖。

    点保存后**先轮询落盘**；覆盖确认只查前台窗。勿 Desktop.windows（205052
    实跑每页 fill 30～47s，文件其实早已写完）。
    """
    target = os.path.normpath(os.path.abspath(target))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
        step('saveas', note='dir_ready', dir=parent)
    if os.path.isfile(target):
        try:
            os.remove(target)
            step('saveas', note='removed_existing', path=target)
        except OSError as e:
            step('saveas', warn=f'删旧文件失败，稍后点是覆盖: {e}')
    base = os.path.basename(target)
    t_set = time.time()
    fname = _fname_edit(dlg)
    if fname is None:
        step('saveas', warn='无文件名框')
        return False
    if not _set_edit_text(fname, target):
        if not _set_edit_text(fname, base):
            step('saveas', warn='文件名写入失败', want=base)
            return False
    cur = uia.edit_value(fname) or ''
    if base.lower() not in cur.lower() and 'fetch-file' in cur.lower():
        step('saveas', warn='文件名仍是默认 fetch-file', cur=cur[:80])
        return False
    step('saveas', note='filename_set', cur=cur[:100],
         set_s=round(time.time() - t_set, 3))
    t_click = time.time()
    if not _click_save_button(dlg):
        step('saveas', warn='未点到保存按钮')
        return False
    step('saveas', note='save_clicked', click_s=round(time.time() - t_click, 3))
    # 落盘优先：文件在则立刻成功，勿先扫覆盖框
    t_poll = time.time()
    for i in range(40):
        if os.path.isfile(target) and os.path.getsize(target) > 1000:
            step('saveas', note='file_ready_after_click',
                 poll_s=round(time.time() - t_poll, 3), loops=i + 1)
            return True
        if i in (0, 2, 5, 10) and _dismiss_overwrite_confirm():
            step('saveas', note='overwrite_yes')
            time.sleep(0.15)
            continue
        if i == 8:
            _dismiss_filename_invalid()
        time.sleep(0.1)
    if _dismiss_overwrite_confirm():
        time.sleep(0.2)
    ok = os.path.isfile(target) and os.path.getsize(target) > 1000
    step('saveas', note='poll_done', ok=ok,
         poll_s=round(time.time() - t_poll, 3))
    return ok


def _timing_wall() -> str:
    """墙钟时间戳 HH:MM:SS.mmm（对人眼盯屏对齐）。"""
    t = time.time()
    return time.strftime('%H:%M:%S', time.localtime(t)) + f'.{int(t * 1000) % 1000:03d}'


def _timing_log_line(clock: dict, line: str, step=None, **extra) -> None:
    msg = f'[decfile][timing] {line}'
    print(msg, flush=True)
    logp = clock.get('log') if clock else None
    if logp:
        try:
            with open(logp, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass
    if step:
        step('timing', line=line[:200], **{k: v for k, v in extra.items()
                                           if v is not None})


def _timing_clock_new(out_dir: str, *, page: int = 0, label: str = '') -> dict:
    logp = os.path.join(out_dir, 'timing_after_saveas.txt')
    clock = {
        't0': time.time(),
        'log': logp,
        'open': {},
        'marks': [],
        'page': page,
    }
    hdr = (f'======== page={page} {label} wall={_timing_wall()} '
           f't0={clock["t0"]:.3f} ========')
    _timing_log_line(clock, hdr)
    return clock


def _timing_start(clock: dict, op: str, step=None) -> None:
    """START op + 墙钟；duration 在 _timing_end 里算。"""
    if not clock:
        return
    now = time.time()
    clock.setdefault('open', {})[op] = now
    since = now - clock['t0']
    _timing_log_line(
        clock,
        f'START {op}  ts={_timing_wall()}  since_t0={since:.3f}s',
        step, phase=f'start:{op}', since=round(since, 3))


def _timing_end(clock: dict, op: str, step=None, **extra) -> float:
    """END op + 墙钟 + duration；返回 duration 秒。"""
    if not clock:
        return 0.0
    now = time.time()
    t0 = clock.setdefault('open', {}).pop(op, now)
    dur = now - t0
    since = now - clock['t0']
    extra_s = ''
    if extra:
        extra_s = '  ' + ' '.join(f'{k}={v}' for k, v in extra.items())
    _timing_log_line(
        clock,
        f'END   {op}  ts={_timing_wall()}  duration={dur:.3f}s  '
        f'since_t0={since:.3f}s{extra_s}',
        step, phase=f'end:{op}', duration=round(dur, 3), since=round(since, 3),
        **extra)
    clock.setdefault('marks', []).append(
        (op, round(dur, 3), round(since, 3)))
    return dur


def _timing_mark(clock: dict, name: str, step=None) -> None:
    """兼容旧调用：等价于瞬时点（start+end duration≈0 的里程碑）。"""
    if not clock:
        return
    _timing_start(clock, name, step)
    _timing_end(clock, name, step)


def _chrome_window_title() -> str:
    """前台窗口标题（ctypes，避免大图 Tab 时 UIA 枚举卡数十秒）。"""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ''
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value or ''
    except Exception:
        return ''


def _close_chrome_save_preview(step=None) -> bool:
    """另存后 Chrome 常打开 fetch-file/savetmp PNG 预览 Tab，UIA 被大图拖死。

    仅当标题像预览图时 Ctrl+W；cpquery 详情标题绝不关。
    """
    title = _chrome_window_title()
    tl = title.lower()
    if any(m in title for m in (
            '专利审查信息查询', '国内案件信息详情', '中国及多国专利')):
        return False
    if not (('fetch-file' in tl) or ('savetmp_' in tl)
            or ('.png' in tl and 'chrome' in tl)):
        return False
    try:
        send_keys('^w', pause=0.05)
        time.sleep(0.35)
        if step:
            step('saveas', note='closed_png_preview', title=title[:80])
        return True
    except Exception as e:
        if step:
            step('saveas', warn=f'关预览 Tab 失败: {e}'[:80])
        return False


def _save_image_as_at(win, cx: int, cy: int, out_path: str, out_dir: str, step) -> bool:
    """在 (cx,cy) 右键 → 图片另存为 → 存到 out_path。成功 True。"""
    page_n = 0
    try:
        m = re.search(r'dec_page_(\d+)', os.path.basename(out_path))
        page_n = int(m.group(1)) if m else 0
    except Exception:
        page_n = 0
    # 从右键起记全链路；另存「完成」以文件落盘为界（after_saveas_complete）
    clock = _timing_clock_new(out_dir, page=page_n, label='saveas_full')
    win._cnipa_after_save_clock = clock

    for old in glob.glob(os.path.join(out_dir, 'savetmp_*.png')):
        try:
            os.remove(old)
        except OSError:
            pass
    tmp_name = 'savetmp_' + time.strftime('%H%M%S') + '.png'
    target = os.path.normpath(os.path.join(os.path.abspath(out_dir), tmp_name))

    _timing_start(clock, 'right_click_menu', step)
    uia.human_right_click(win, cx, cy)
    time.sleep(0.6)
    menu_r = None
    for m in uia.descendants(win, control_type='MenuItem'):
        if match_save_image_menu(uia._name(m)):
            mr = uia._rect(m)
            if mr:
                menu_r = mr
                break
    _timing_end(clock, 'right_click_menu', step, menu_ok=bool(menu_r))
    if menu_r is None:
        try:
            send_keys('{ESC}', pause=0.05)
        except Exception:
            pass
        step('saveas', warn='右键无「图片另存为」', at=(cx, cy))
        try:
            del win._cnipa_after_save_clock
        except AttributeError:
            pass
        return False

    _timing_start(clock, 'click_menu_图片另存为', step)
    uia.human_click(win, (menu_r[0] + menu_r[2]) // 2,
                    (menu_r[1] + menu_r[3]) // 2)
    _timing_end(clock, 'click_menu_图片另存为', step)

    _timing_start(clock, 'find_save_dialog', step)
    dlg = _find_save_dialog(timeout_s=0.5)
    if dlg is None:
        dlg = _find_save_dialog(
            timeout_s=4.0,
            probe_path=os.path.join(out_dir, 'saveas_dec_probe.json'))
    _timing_end(clock, 'find_save_dialog', step, found=bool(dlg))
    if dlg is None:
        try:
            send_keys('{ESC}', pause=0.05)
        except Exception:
            pass
        step('saveas', warn='另存为对话框未出现', at=(cx, cy))
        try:
            del win._cnipa_after_save_clock
        except AttributeError:
            pass
        return False

    _timing_start(clock, 'fill_and_confirm_save', step)
    try:
        ok_fill = _fill_and_confirm_save(dlg, target, step)
    except Exception as e:
        step('saveas', warn=f'对话框失败: {e}')
        ok_fill = False
    _timing_end(clock, 'fill_and_confirm_save', step, ok=ok_fill)
    if not ok_fill:
        _close_save_dialog()
        try:
            del win._cnipa_after_save_clock
        except AttributeError:
            pass
        return False

    saved = ''
    _timing_start(clock, 'wait_file_on_disk', step)
    for i in range(40):
        if os.path.isfile(target) and os.path.getsize(target) > 1000:
            saved = target
            break
        hits = [p for p in glob.glob(os.path.join(out_dir, 'savetmp_*.png'))
                if os.path.isfile(p) and os.path.getsize(p) > 1000]
        if hits:
            saved = hits[0]
            break
        time.sleep(0.12)
    _timing_end(clock, 'wait_file_on_disk', step, saved=bool(saved))
    if not saved:
        try:
            send_keys('{ESC}', pause=0.05)
            send_keys('{ESC}', pause=0.05)
        except Exception:
            pass
        step('saveas', warn='文件未落盘', at=(cx, cy))
        try:
            del win._cnipa_after_save_clock
        except AttributeError:
            pass
        return False

    _timing_start(clock, 'esc_close_dialog', step)
    try:
        send_keys('{ESC}', pause=0.05)
        send_keys('{ESC}', pause=0.05)
    except Exception:
        pass
    time.sleep(0.1)
    _timing_end(clock, 'esc_close_dialog', step)

    _timing_start(clock, 'close_png_preview_tab', step)
    closed = _close_chrome_save_preview(step)
    _timing_end(clock, 'close_png_preview_tab', step, closed=closed)

    _timing_start(clock, 'rename_to_dec_page', step)
    try:
        if os.path.abspath(saved) != os.path.abspath(out_path):
            if os.path.isfile(out_path):
                os.remove(out_path)
            os.replace(saved, out_path)
    except OSError:
        shutil.copyfile(saved, out_path)
    _timing_end(clock, 'rename_to_dec_page', step,
                bytes=os.path.getsize(out_path) if os.path.isfile(out_path) else 0)

    # ★ 另存完成分界：落盘+关框+改名后（对齐「对话框已关、页图已就绪」）
    clock['t_save_done'] = time.time()
    _timing_log_line(
        clock,
        f'NOTE  after_saveas_complete  ts={_timing_wall()}  '
        f'since_t0={clock["t_save_done"] - clock["t0"]:.3f}s',
        step, phase='after_saveas_complete')

    step('saveas', ok=True, out=out_path, size=os.path.getsize(out_path),
         timing=clock.get('marks'))
    return True


def _merge_pngs_to_pdf(pages: list, target_pdf: str, step) -> dict:
    if not pages:
        return {'ok': False, 'error': '旧决定页图为空'}
    try:
        images = []
        for p in pages:
            img = Image.open(p)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            images.append(img)
        os.makedirs(os.path.dirname(target_pdf), exist_ok=True)
        if len(images) == 1:
            images[0].save(target_pdf, 'PDF', resolution=150)
        else:
            images[0].save(target_pdf, 'PDF', save_all=True,
                           append_images=images[1:], resolution=150)
        for img in images:
            img.close()
        step('old_merge', pages=len(pages), pdf=target_pdf)
        return {'ok': True, 'method': 'saveas_images', 'pages': len(pages)}
    except Exception as e:
        step('old_merge', warn=f'PDF 合并失败: {e}')
        return {'ok': False, 'error': f'PDF 合并失败: {e}'}


def _content_click_x(wr) -> int:
    """右栏决定书内容区 X（屏幕坐标）。"""
    return wr[0] + max(NAV_X_MAX + 120, (wr[2] - wr[0]) * 2 // 3)


# 末页无横线：OCR 认合议组角色名（三者不必齐全，命中任一即可）。
# 机构落款文案不固定，不作为判定条件。
_LAST_PAGE_PANEL = ('合议组组长', '主审员', '参审员')


def _last_page_anchor(png_path: str, win_rect) -> dict:
    """OCR 找末页合议组落款。仅供「已存页且找不到分页线」时使用。"""
    try:
        items = co.ocr_image(png_path)
    except Exception:
        return {}
    hits = []
    for tx, bx in items:
        t = _ocr_compact(tx)
        for m in _LAST_PAGE_PANEL:
            if m in t:
                hits.append((m, bx))
                break
    if not hits:
        return {}
    hit, bx = min(hits, key=lambda it: it[1][1])
    cx = win_rect[0] + (bx[0] + bx[2]) // 2
    cy = win_rect[1] + max(bx[1] - 80, (bx[1] + bx[3]) // 2 - 120)
    if cy < win_rect[1] + 160:
        cy = win_rect[1] + 160
    if cx < win_rect[0] + NAV_X_MAX:
        cx = _content_click_x(win_rect)
    return {'hit': hit, 'cx': cx, 'cy': cy}


def _ink_click_candidates(png_path: str, win_rect, *, max_n: int = 6) -> list:
    """从截图自下而上扫墨迹点（屏幕坐标），供末页右键另存；避开顶部导航区。

    返回 [{'cx','cy','ink','via'}, ...]，ink 为该行像素暗色占比。
    """
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return []
    w, h = im.size
    if w < 200 or h < 200:
        return []
    left = max(0, min(NAV_X_MAX, w // 3, w - 200))
    x0 = left + (w - left) // 8
    x1 = w - max(40, (w - left) // 10)
    y_lo = max(140, int(h * 0.12))
    y_hi = min(h - 40, int(h * 0.90))
    if y_hi <= y_lo + 40 or x1 <= x0 + 40:
        return []
    px = im.load()
    step_x = max(1, (x1 - x0) // 80)
    cands = []
    y = y_hi
    while y > y_lo and len(cands) < max_n * 3:
        band0 = max(y_lo, y - 10)
        band1 = min(y_hi, y + 2)
        ink = tot = 0
        dark_cols = []
        for yy in range(band0, band1, 2):
            row_dark = []
            for x in range(x0, x1, step_x):
                tot += 1
                v = px[x, yy]
                if v < 200:
                    ink += 1
                    row_dark.append(x)
            if row_dark:
                dark_cols.extend(row_dark)
        ratio = ink / tot if tot else 0.0
        if ratio >= 0.05 and dark_cols:
            dark_cols.sort()
            ix = dark_cols[len(dark_cols) // 2]
            cx = win_rect[0] + ix
            cy = win_rect[1] + y
            if not any(abs(cy - c['cy']) < 48 for c in cands):
                cands.append({
                    'cx': cx, 'cy': cy, 'ink': round(ratio, 4),
                    'via': 'ink_scan',
                })
        y -= 36
    cands.sort(key=lambda d: (-d['ink'], -d['cy']))
    return cands[:max_n]


def _ink_above_divider(png_path: str, line_y_img: int) -> float:
    """横线上方约 180px 墨迹占比；过低说明本页正文被滚出视口。"""
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return 0.0
    w, h = im.size
    left = max(0, min(NAV_X_MAX, w - 120))
    x0 = left + (w - left) // 8
    x1 = w - (w - left) // 8
    y0 = max(100, line_y_img - 200)
    y1 = max(y0 + 10, line_y_img - 12)
    band = im.crop((x0, y0, x1, y1))
    bw, bh = band.size
    return _band_ink_ratio(band.load(), bw, bh, 0, bh)


def _click_y_on_page_above(png_path: str, line_y_img: int, wr) -> int:
    """在分页线上方点本页墨迹，禁止点页间白边。

    只用像素扫墨迹，勿再 OCR（找线已 OCR；此处二次 MinerU 可空等数十秒）。
    """
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return wr[1] + max(140, line_y_img - 180)
    w, h = im.size
    left = max(0, min(NAV_X_MAX, w - 120))
    x0 = left + (w - left) // 8
    x1 = w - (w - left) // 8
    px = im.load()
    best = None
    for y in range(line_y_img - 30, max(110, line_y_img - 420), -1):
        if y >= h or y < 0:
            continue
        step_x = max(1, (x1 - x0) // 60)
        vals = [px[x, y] for x in range(x0, x1, step_x)]
        if not vals:
            continue
        if sum(1 for v in vals if v < 200) / len(vals) >= 0.06:
            best = y
            break
    if best is None:
        best = max(120, line_y_img - 200)
    cy = wr[1] + best
    if cy < wr[1] + 140:
        cy = wr[1] + 140
    if cy >= wr[1] + line_y_img - 12:
        cy = wr[1] + line_y_img - 50
    return cy


def _usable_divider_ys(lines, wr, *, has_pages: bool) -> list:
    bottom = (wr[3] - 60) if has_pages else (wr[3] - 140)
    return [y for y in lines if (wr[1] + 180) < y < bottom]


def _usable_dividers(lines, wr, *, has_pages: bool = True) -> list:
    return _usable_divider_ys(lines, wr, has_pages=has_pages)


def _nudge_up_reveal_ink(win, out_dir: str, step, wr, probe: str,
                         line_y: int, first_page: bool):
    """线上方空白（本页尾已滚出）→ 小步上滚露出本页墨迹再另存。

    界限：一露出墨迹即停；若再看到「上一页分界线+其上空白」则禁止继续上滚。
    """
    line_y_img = line_y - wr[1]
    ink = _ink_above_divider(probe, line_y_img)
    if ink >= 0.05:
        return probe, wr, line_y, ink, 'ink_ok'

    for i in range(8):
        step('old_nudge', why='线上方空白，小步上滚露本页墨迹',
             notches=1, round=i + 1, ink=round(ink, 4))
        uia.scroll_up(win, CONTENT_X, CONTENT_Y, notches=1)
        time.sleep(0.45)
        wr = uia._rect(win) or wr
        probe = os.path.join(out_dir, f'nudge_up_{i}.png')
        uia.screenshot_window(win, probe)
        # nudge 只跟线：纯像素，勿 OCR
        lines = _find_divider_screen_ys(
            probe, wr, first_page=False, ocr_validate=False)
        usable = _usable_dividers(lines, wr)
        if not usable:
            # 目标线滚出下沿：略下滚找回，不再上滚
            for j in range(4):
                uia.scroll_down(win, CONTENT_X, CONTENT_Y, notches=1)
                time.sleep(0.35)
                wr = uia._rect(win) or wr
                uia.screenshot_window(win, probe)
                lines = _find_divider_screen_ys(
                    probe, wr, first_page=False, ocr_validate=False)
                usable = _usable_dividers(lines, wr)
                if usable:
                    break
            if not usable:
                return probe, wr, line_y, ink, 'line_lost'
            line_y = usable[0]
            ink = _ink_above_divider(probe, line_y - wr[1])
            return probe, wr, line_y, ink, 'line_recovered'

        # 出现更上方的另一条分页线 = 上一页分界，禁止再上滚
        above = [y for y in usable if y < line_y - 60]
        if above:
            # 若上滚后原线还在，用原线；并停
            same = [y for y in usable if abs(y - line_y) < 120]
            line_y = same[0] if same else usable[-1]
            ink = _ink_above_divider(probe, line_y - wr[1])
            step('old_nudge', why='已见上一页分界线，停止上滚',
                 prev_divider=above[0], line_y=line_y)
            return probe, wr, line_y, ink, 'hit_prev_divider'

        # 跟踪目标线（上滚后屏幕 y 变大）
        line_y = max(usable)
        ink = _ink_above_divider(probe, line_y - wr[1])
        if ink >= 0.05:
            step('old_nudge', why='已露出本页墨迹', rounds=i + 1,
                 ink=round(ink, 4))
            return probe, wr, line_y, ink, 'ink_ok'

    return probe, wr, line_y, ink, 'nudge_cap'


def _try_save_one_page(win, cx, cy, out_dir, pages, seen_hash, step, note='') -> bool:
    """另存一页；新页则 append 并返回 True。"""
    n = len(pages) + 1
    out_png = os.path.join(out_dir, f'dec_page_{n}.png')
    if not _save_image_as_at(win, cx, cy, out_png, out_dir, step):
        return False
    clock = getattr(win, '_cnipa_after_save_clock', None)
    try:
        _timing_start(clock, 'md5_page_png', step)
        md = _file_md5(out_png)
        _timing_end(clock, 'md5_page_png', step)
        _timing_start(clock, 'read_image_size', step)
        with Image.open(out_png) as im:
            iw, ih = im.size
        _timing_end(clock, 'read_image_size', step, size=f'{iw}x{ih}')
    except OSError:
        md, iw, ih = '', 0, 0
    if md and md not in seen_hash and iw >= 600 and ih >= 600:
        seen_hash.add(md)
        pages.append(out_png)
        _timing_start(clock, 'register_old_page', step)
        _timing_end(clock, 'register_old_page', step, n=len(pages))
        step('old_page', n=len(pages), size=f'{iw}x{ih}',
             pages_collected=len(pages), note=note or None)
        if clock:
            clock['t_before_scroll'] = time.time()
            tsd = clock.get('t_save_done')
            gap = (clock['t_before_scroll'] - tsd) if tsd else None
            gap_s = f'post_save_md5_gap={gap:.3f}s' if gap is not None else ''
            _timing_log_line(
                clock,
                f'NOTE  before_first_scroll  ts={_timing_wall()}  {gap_s}  '
                f'(MD5/收录结束，即将下滚)',
                step, phase='before_first_scroll')
        return True
    try:
        os.remove(out_png)
    except OSError:
        pass
    step('old_page', n=n, note='重复或过小', size=f'{iw}x{ih}')
    try:
        del win._cnipa_after_save_clock
    except AttributeError:
        pass
    return False


_CASE_NO_RE = re.compile(r'\d{1,3}W\d{5,7}', re.I)


def _collect_case_nos_from_text(text: str) -> set:
    return {m.group(0).upper() for m in _CASE_NO_RE.finditer(text or '')}


def _left_nav_case_nos(win, out_dir: str, tag: str) -> set:
    """左栏可见的案件号（如高亮的 4W107577）。a11y + 左栏 OCR。"""
    found = set()
    wr = uia._rect(win)
    x_max = (wr[0] + NAV_X_MAX + 120) if wr else NAV_X_MAX + 120
    for ct in ('TreeItem', 'ListItem', 'Hyperlink', 'Text', 'Button'):
        try:
            for el in uia.descendants(win, control_type=ct):
                name = uia._name(el) or ''
                nos = _collect_case_nos_from_text(name)
                if not nos:
                    continue
                r = uia._rect(el)
                # Chrome 树节点常给 0 矩形：名称已是案号则仍收录
                if r is None or r[2] <= r[0] or r[3] <= r[1]:
                    found |= nos
                    continue
                if r[0] <= x_max:
                    found |= nos
        except Exception:
            continue
    probe = os.path.join(out_dir, f'{tag}_nav.png')
    try:
        uia.screenshot_window(win, probe)
        im = Image.open(probe)
        w, h = im.size
        # 左栏约 1/3 宽，含「无效审查决定」下高亮案号
        crop_w = min(max(NAV_X_MAX + 120, w // 3 + 80), max(200, w // 2))
        left = im.crop((0, 50, crop_w, h - 20))
        tmp = os.path.join(out_dir, f'{tag}_nav_crop.png')
        left.save(tmp)
        blob = ''.join(t for t, _ in co.ocr_image(tmp))
        found |= _collect_case_nos_from_text(_ocr_compact(blob))
    except Exception:
        pass
    return found


def _window_has_case_no(win, case_no: str, out_dir: str, tag: str) -> bool:
    """整窗截图是否已含目标 case_no（前台已是详情时勿再点 Tab）。"""
    key = _ocr_compact(case_no).upper()
    if not key:
        return False
    try:
        probe = os.path.join(out_dir, f'{tag}_full.png')
        uia.screenshot_window(win, probe)
        blob = _ocr_compact(''.join(t for t, _ in co.ocr_image(probe)))
        return key in blob.upper()
    except Exception:
        return False


def _classify_tab_case(win, case_no: str, out_dir: str, step,
                       tag: str) -> str:
    """当前 Tab 相对目标 case：hit / other / unknown。

    - hit：左栏已见目标案件号
    - other：左栏见其它案件号（如 4W120325）→ 禁止回顶滚动
    - unknown：左栏看不见任何案件号（栏收起或正文滚在中间）→ 才允许回顶核
    """
    key = _ocr_compact(case_no).upper()
    if not key:
        return 'unknown'
    found = _left_nav_case_nos(win, out_dir, tag)
    step(tag, nav_cases=sorted(found), want=key)
    if key in found:
        return 'hit'
    if found:
        return 'other'
    return 'unknown'


def _content_band_hash(win, out_dir: str, tag: str,
                       y0: int = 100, y1_ratio: float = 0.45):
    """右栏内容区哈希；失败返回 None。"""
    try:
        probe = os.path.join(out_dir, f'{tag}.png')
        uia.screenshot_window(win, probe)
        im = Image.open(probe).convert('L')
        w, h = im.size
        left = max(0, min(NAV_X_MAX, w - 120))
        y1 = min(h - 40, max(y0 + 80, int(h * y1_ratio)))
        band = im.crop((left, y0, w - 30, y1))
        return hashlib.md5(band.tobytes()).hexdigest()
    except Exception:
        return None


def _content_top_hash(win, out_dir: str, tag: str):
    """右栏顶区内容哈希；失败返回 None。"""
    return _content_band_hash(win, out_dir, tag, y0=100, y1_ratio=0.42)


def _scroll_at_bottom(win, out_dir: str, step) -> bool:
    """再下滚一小步画面不变 → 滚动条已到底 = 末页（cnipa-mcp 同款）。"""
    wr = uia._rect(win)
    if wr is None:
        return False
    cx = _content_click_x(wr)
    cy = max(wr[1] + 220, min(CONTENT_Y, wr[3] - 120))
    h0 = _content_band_hash(win, out_dir, 'at_bottom_0', y0=120, y1_ratio=0.92)
    try:
        win.set_focus()
        uia.scroll_down(win, cx, cy, notches=4, settle=0.4)
    except Exception:
        pass
    h1 = _content_band_hash(win, out_dir, 'at_bottom_1', y0=120, y1_ratio=0.92)
    ok = bool(h0 and h1 and h0 == h1)
    step('at_bottom', ok=ok, via='scroll_probe')
    return ok


def _old_finish_merge(pages: list, target_pdf: str, step, **extra) -> dict:
    """有页图就合并落盘（与 cnipa-mcp 一致：7 页也出 PDF）。"""
    if not pages:
        out = {'ok': False, 'pages': 0}
        out.update(extra)
        if 'error' not in out:
            out['error'] = '旧决定未收到任何页图'
        return out
    m = _merge_pngs_to_pdf(pages, target_pdf, step)
    m.setdefault('pages', len(pages))
    m.setdefault('decision_type', 'old')
    for k, v in extra.items():
        if v is not None and k not in m:
            m[k] = v
    return m


def _detail_already_at_top(win, out_dir: str, step) -> bool:
    """是否已滚到决定书顶部：再上滚一小步画面不变，或顶区已见局名/发文日。"""
    wr = uia._rect(win)
    if wr is None:
        return False
    # 顶区 OCR：首页抬头
    try:
        probe = os.path.join(out_dir, 'at_top_ocr.png')
        uia.screenshot_window(win, probe)
        for tx, bx in co.ocr_image(probe):
            if bx[0] <= NAV_X_MAX or bx[1] >= 300:
                continue
            t = _ocr_compact(tx)
            if '国家知识产权局' in t or '发文' in t:
                step('at_top', ok=True, via='header_ocr')
                return True
    except Exception:
        pass
    # 上滚试探：不变=已在顶
    cx = _content_click_x(wr)
    cy = max(wr[1] + 220, min(CONTENT_Y, wr[3] - 120))
    h0 = _content_top_hash(win, out_dir, 'at_top_0')
    try:
        win.set_focus()
        uia.scroll_up(win, cx, cy, notches=4, settle=0.35)
    except Exception:
        pass
    h1 = _content_top_hash(win, out_dir, 'at_top_1')
    ok = bool(h0 and h1 and h0 == h1)
    step('at_top', ok=ok, via='scroll_probe')
    return ok


def _screenshot_ocr_blob(win, out_dir: str, tag: str) -> str:
    try:
        probe = os.path.join(out_dir, f'{tag}.png')
        uia.screenshot_window(win, probe)
        return _ocr_compact(''.join(t for t, _ in co.ocr_image(probe)))
    except Exception:
        return ''


def _blob_is_results(blob: str) -> bool:
    """仅认明确结果列表锚词；不用「重置」（详情页也常见，会误判）。"""
    b = blob or ''
    return '共查询到' in b or '展开更多搜索项' in b


def _blob_is_detail(blob: str) -> bool:
    """详情页特征（相对结果列表）。"""
    b = blob or ''
    if _blob_is_results(b):
        return False
    if '国内案件信息详情' in b:
        return True
    if any(m in b for m in DOC_MARKERS):
        return True
    if '国家知识产权局' in b and ('发文' in b or '无效宣告' in b or '复审' in b):
        return True
    if '案件编号' in b and ('发文' in b or '申请号' in b):
        return True
    if '无效审查决定' in b or '复审无效审查决定' in b:
        return True
    if any(m in b for m in ('一、案由', '二、决定', '三、决定', '、案由')):
        return True
    return False


def _page_is_results(win, out_dir: str, tag: str) -> bool:
    """查询结果列表页（必须见「共查询到」等，禁止单靠「重置」）。"""
    try:
        joined = '\n'.join(uia.all_texts(win) or [])
        if _blob_is_results(_ocr_compact(joined)):
            return True
    except Exception:
        pass
    return _blob_is_results(_screenshot_ocr_blob(win, out_dir, tag + '_state'))


def _page_has_case_no(blob: str, case_no: str) -> bool:
    key = _ocr_compact(case_no).upper()
    if not key:
        return False
    b = (blob or '').upper()
    if key in b:
        return True
    # OCR 偶发漏字母：4W107577 → 4107577 / 第4W107577号
    digits = re.sub(r'[^0-9]', '', key)
    if len(digits) >= 6 and digits in re.sub(r'[^0-9]', '', b):
        if 'W' in key.upper() and ('W' + digits[-6:]) in b.replace(' ', ''):
            return True
    return key in b


def _try_accept_detail_tab(win, case_no: str, out_dir: str, step,
                           tag: str) -> bool:
    """当前 Tab 是否目标详情。

    1) 结果列表 → 否
    2) 先回顶（已在顶则跳过）
    3) 左栏或 OCR 见目标 case_no → 是
    禁止：未回顶/未 OCR 就因左栏 other 去点其它 Tab
    """
    key = _ocr_compact(case_no).upper()
    if _page_is_results(win, out_dir, tag):
        step(tag, note='查询结果页，跳过', case_no=case_no)
        return False

    # 已在顶部：先整窗 OCR（你截图这种抬头+案号场景应直接命中）
    at_top = _detail_already_at_top(win, out_dir, step)
    if at_top:
        blob0 = _screenshot_ocr_blob(win, out_dir, tag + '_pre')
        if _page_has_case_no(blob0, case_no) and (
                _blob_is_detail(blob0) or '发文' in blob0
                or '案件编号' in blob0 or '国家知识产权局' in blob0):
            step(tag, match=True, via='ocr_already_top', case_no=case_no)
            return True

    kind = _classify_tab_case(win, case_no, out_dir, step, tag=tag)
    if kind == 'hit':
        step(tag, match=True, via='nav', case_no=case_no)
        return True

    # 未在顶 → 必须回顶后再判（滚在中间看不到抬头）
    if not at_top:
        step(tag, note='未在顶部，先回顶再核是否目标详情')
        _scroll_decision_to_top(win, out_dir, step)

    if _page_is_results(win, out_dir, tag + '_after_top'):
        step(tag, note='回顶后是结果页，跳过', case_no=case_no)
        return False

    kind2 = _classify_tab_case(win, case_no, out_dir, step, tag=tag + '_top')
    if kind2 == 'hit':
        step(tag, match=True, via='nav_after_top', case_no=case_no)
        return True

    blob = _screenshot_ocr_blob(win, out_dir, tag + '_top_ocr')
    if _blob_is_results(blob):
        step(tag, match=False, via='results_ocr')
        return False
    if _page_has_case_no(blob, case_no):
        step(tag, match=True, via='ocr_after_top', case_no=case_no,
             is_detail=_blob_is_detail(blob))
        return True

    # 左栏 other 也不能在未见正文 OCR 前就否决；到这里正文也无案号才否
    step(tag, match=False, via='not_target_after_top',
         nav_kind=kind2, sample=(blob or '')[:80])
    return False


def _activate_case_detail_tab(win, case_no: str, out_dir: str, step) -> bool:
    """认目标详情 Tab：当前页先回顶核对；不是目标才点其它 Tab。"""
    if _try_accept_detail_tab(win, case_no, out_dir, step, 'tab_cur'):
        step('tab_pick', ok=True, via='current', case_no=case_no)
        return True
    tabs = uia.find_cpquery_strip_tabs(win)
    if not tabs:
        step('tab_pick', warn='无 cpquery 顶栏 Tab')
        return False
    for i, t in enumerate(tabs):
        try:
            tab_title = uia._name(t) or ''
            uia.click(win, t, settle=0.5)
            time.sleep(0.4)
        except Exception as e:
            step('tab_pick', warn=f'tab{i}: {e}')
            continue
        if _try_accept_detail_tab(win, case_no, out_dir, step, f'tab_{i}'):
            step('tab_pick', ok=True, index=i, tab=tab_title[:60],
                 case_no=case_no)
            return True
    step('tab_pick', ok=False, tabs=len(tabs), case_no=case_no,
         warn='未找到目标决定详情 Tab')
    return False


def _scroll_decision_to_top(win, out_dir: str, step) -> None:
    """决定书右栏滚回最顶（用户中途拖过滚动条时必须先回顶再另存）。"""
    wr = uia._rect(win)
    if wr is None:
        return
    cx = _content_click_x(wr)
    cy = max(wr[1] + 220, min(CONTENT_Y, wr[3] - 120))
    try:
        win.set_focus()
        time.sleep(0.15)
        from pywinauto.mouse import click as _mouse_click
        _mouse_click(button='left', coords=(int(cx), int(cy)))
        time.sleep(0.2)
        send_keys('^{HOME}', pause=0.08)
        time.sleep(0.35)
    except Exception:
        pass
    prev = None
    stable = 0
    for i in range(14):
        uia.scroll_up(win, cx, cy, notches=22, settle=0.35)
        hsh = _content_top_hash(win, out_dir, f'scroll_top_{i}')
        if hsh and hsh == prev:
            stable += 1
            if stable >= 2:
                step('scroll_top', ok=True, rounds=i + 1, stable=True)
                return
        else:
            stable = 0
        prev = hsh
    step('scroll_top', ok=True, rounds=14, stable=False)


def _scroll_past_divider(win, out_dir: str, step, _saved_line_y: int) -> None:
    """另存后固定下滚；用右栏哈希验证页面是否真动（日志 START/END+duration）。"""
    clock = getattr(win, '_cnipa_after_save_clock', None)
    if clock is None:
        clock = _timing_clock_new(out_dir, label='scroll_past_only')
        win._cnipa_after_save_clock = clock
    t_done = clock.get('t_save_done')
    t_before = clock.get('t_before_scroll')
    now = time.time()
    if t_before or t_done:
        parts = [f'NOTE  enter_scroll_past  ts={_timing_wall()}']
        if t_before:
            parts.append(f'since_before_scroll={now - t_before:.3f}s')
        if t_done:
            parts.append(f'since_save_done={now - t_done:.3f}s')
        _timing_log_line(clock, '  '.join(parts), step)

    _timing_start(clock, 'scroll_past.close_preview', step)
    _close_chrome_save_preview(step)
    _timing_end(clock, 'scroll_past.close_preview', step)

    # 截图哈希本身可能很慢——单独计时，便于抓「函数返回了但页没动」或「截图卡死」
    _timing_start(clock, 'scroll_past.hash_before', step)
    h0 = _content_band_hash(win, out_dir, 'scroll_past_before',
                            y0=120, y1_ratio=0.92)
    _timing_end(clock, 'scroll_past.hash_before', step,
                hash=(h0 or '')[:12] or 'none')

    _timing_start(clock, 'scroll_past.mouse_click_focus', step)
    try:
        wr = uia._rect(win)
        cx = _content_click_x(wr) if wr else CONTENT_X
        cy = max((wr[1] + 220) if wr else 280,
                 min(CONTENT_Y, (wr[3] - 120) if wr else CONTENT_Y))
        from pywinauto.mouse import click as _mouse_click
        from pywinauto.mouse import scroll as _mouse_scroll
        _mouse_click(button='left', coords=(int(cx), int(cy)))
        time.sleep(0.05)
    except Exception:
        cx, cy = CONTENT_X, CONTENT_Y
        from pywinauto.mouse import scroll as _mouse_scroll
    _timing_end(clock, 'scroll_past.mouse_click_focus', step, at=(cx, cy))

    n = OLD_SCROLL_PAST_NOTCHES
    _timing_start(clock, 'scroll_past.wheel', step)
    try:
        _mouse_scroll(coords=(int(cx), int(cy)), wheel_dist=-abs(n))
        via = 'mouse_scroll'
    except Exception:
        uia.scroll_down(win, cx, cy, notches=n, settle=0.15)
        via = 'uia_scroll_down'
    time.sleep(0.25)
    _timing_end(clock, 'scroll_past.wheel', step, notches=n, via=via)

    _timing_start(clock, 'scroll_past.hash_after', step)
    h1 = _content_band_hash(win, out_dir, 'scroll_past_after',
                            y0=120, y1_ratio=0.92)
    hash_ok = h0 is not None and h1 is not None
    moved = hash_ok and h0 != h1
    _timing_end(clock, 'scroll_past.hash_after', step,
                hash=(h1 or '')[:12] or 'none', page_moved=moved,
                hash_ok=hash_ok)

    if not moved:
        _timing_start(clock, 'scroll_past.retry_wheel', step)
        try:
            _mouse_scroll(coords=(int(cx), int(cy)), wheel_dist=-abs(n))
            time.sleep(0.25)
            if hash_ok:
                h2 = _content_band_hash(win, out_dir, 'scroll_past_retry',
                                        y0=120, y1_ratio=0.92)
                moved = bool(h2 and h0 != h2)
            else:
                _timing_log_line(
                    clock,
                    'NOTE  scroll_hash_unavailable  retry_wheel_only',
                    step)
        except Exception as e:
            step('old_scroll', warn=f'retry_wheel:{e}'[:80])
        _timing_end(clock, 'scroll_past.retry_wheel', step, page_moved=moved)

    now = time.time()
    parts = [f'NOTE  after_saveas_to_scroll_done  ts={_timing_wall()}',
             f'page_moved={moved}']
    if t_before:
        parts.append(f'since_before_scroll={now - t_before:.3f}s')
    if t_done:
        parts.append(f'since_save_done={now - t_done:.3f}s')
    _timing_log_line(clock, '  '.join(parts), step)
    try:
        del win._cnipa_after_save_clock
    except AttributeError:
        pass
    step('old_scroll', why='另存后下滚过线（不OCR验线）',
         notches=n, page_moved=moved)


def _fetch_old_decision(win, out_dir: str, target_pdf: str, step) -> dict:
    """旧决定：对齐人工操作。

    0. 滚回顶部后先下滚一次（离开首页抬头，不 OCR）
    1. 再找「本页与下页」分隔横线
    2. 鼠标移到横线上方 → 右键「图片另存为」
    3. 另存后固定下滚 OLD_SCROLL_PAST_NOTCHES（不 OCR 验刚存的线）→ OCR 找「下页↔下下页」横线，重复
    4. 末页：无横线且下滚不动（滚动条到底）→ 另存；合议组文字仅可选点位
    """
    pages = []
    seen_hash = set()
    max_rounds = 48
    no_line_rounds = 0
    last_page_done = False
    save_fail = 0

    run_clock = _timing_clock_new(out_dir, page=0, label='old_decision_run')
    win._cnipa_old_run_clock = run_clock
    _timing_start(run_clock, 'scroll_decision_to_top', step)
    _scroll_decision_to_top(win, out_dir, step)
    _timing_end(run_clock, 'scroll_decision_to_top', step)
    wr = uia._rect(win)
    if wr is None:
        return {'ok': False, 'error': '无法获取窗口矩形'}

    _timing_start(run_clock, 'wait_decision_pages_loaded', step)
    _wait_decision_pages_loaded(win, step)
    _timing_end(run_clock, 'wait_decision_pages_loaded', step)

    # 已在顶部=首页抬头区：禁止先 OCR/找线，先下滚离开「国家知识产权局」下划线
    _timing_start(run_clock, 'first_scroll_leave_header', step)
    step('old_scroll', why='首页顶部先下滚，再找第1→2页分页线', notches=8)
    uia.scroll_down(win, CONTENT_X, CONTENT_Y, notches=8)
    time.sleep(0.7)
    _timing_end(run_clock, 'first_scroll_leave_header', step)
    _timing_log_line(
        run_clock,
        f'NOTE  old_decision_ready_for_page1  ts={_timing_wall()}  '
        f'since_run_t0={time.time() - run_clock["t0"]:.3f}s',
        step)

    def _save_last_page(note: str) -> bool:
        nonlocal last_page_done, probe, wr
        last_page_done = True
        try:
            step('last_page_nudge', why='贴底后上滚露墨迹', notches=2)
            win.set_focus()
            uia.scroll_up(win, CONTENT_X, CONTENT_Y, notches=2, settle=0.4)
            time.sleep(0.45)
        except Exception as e:
            step('last_page_nudge', warn=str(e)[:80])
        try:
            wr = uia._rect(win) or wr
            probe = os.path.join(out_dir, 'last_page_probe.png')
            uia.screenshot_window(win, probe)
        except Exception:
            pass
        tries = []
        a = _last_page_anchor(probe, wr)
        if a:
            tries.append({
                'cx': a['cx'], 'cy': a['cy'],
                'via': f"ocr:{a.get('hit')}",
            })
        for c in _ink_click_candidates(probe, wr, max_n=6):
            tries.append(c)
        cx0 = _content_click_x(wr)
        tries.append({
            'cx': cx0,
            'cy': max(wr[1] + 220, (wr[1] * 2 + wr[3]) // 3),
            'via': 'mid_upper',
        })
        tries.append({
            'cx': cx0,
            'cy': max(wr[1] + 200, (wr[1] + wr[3]) // 2),
            'via': 'mid',
        })
        seen_pt = set()
        for i, pt in enumerate(tries):
            cx, cy = int(pt['cx']), int(pt['cy'])
            key = (cx // 8, cy // 8)
            if key in seen_pt:
                continue
            seen_pt.add(key)
            step('last_page_click', n=i + 1, via=pt.get('via'),
                 at=(cx, cy), ink=pt.get('ink'))
            if _try_save_one_page(
                    win, cx, cy, out_dir, pages, seen_hash, step, note=note):
                return True
            _close_save_dialog()
            time.sleep(0.25)
        return False

    pending_search_scroll = 0
    just_scrolled_past = False
    for rnd in range(max_rounds):
        if just_scrolled_past:
            just_scrolled_past = False
            pending_search_scroll = 0
            step('old_seek', note='另存已下滚，OCR找下一条分页线')
        if pending_search_scroll:
            step('old_scroll', why='找线前先下滚（再 OCR）',
                 notches=pending_search_scroll)
            win.set_focus()
            uia.scroll_down(
                win, CONTENT_X, CONTENT_Y,
                notches=pending_search_scroll, settle=0.35)
            time.sleep(0.25)
            pending_search_scroll = 0
        win.set_focus()
        time.sleep(0.2)
        wr = uia._rect(win) or wr
        probe = os.path.join(out_dir, f'divider_probe_{rnd}.png')
        uia.screenshot_window(win, probe)
        first_page = not pages
        t_probe = time.time()
        lines = _find_divider_screen_ys(
            probe, wr, first_page=first_page, ocr_validate=True)
        probe_s = round(time.time() - t_probe, 2)
        usable = _usable_divider_ys(lines, wr, has_pages=bool(pages))
        ocr_meta = getattr(_find_divider_screen_ys, 'last_ocr_meta', {}) or {}
        try:
            _psz = os.path.getsize(probe) if os.path.isfile(probe) else 0
        except OSError:
            _psz = 0
        step('old_round', rnd=rnd + 1, dividers=len(lines),
             usable=len(usable), first_page=first_page, pages=len(pages),
             probe_s=probe_s, probe_bytes=_psz,
             ocr=ocr_meta.get('ocr'), ocr_n=ocr_meta.get('ocr_n'),
             ocr_err=(ocr_meta.get('ocr_err') or '')[:120] or None)
        if ocr_meta.get('ocr') == 'fail':
            step('old_ocr', level='error',
                 msg='找页间线 OCR 失败（右栏裁切/MinerU）',
                 err=ocr_meta.get('ocr_err', '')[:200], probe=probe)
        elif ocr_meta.get('ocr') == 'empty':
            step('old_ocr', level='warn',
                 msg='找页间线时 OCR 返回 0 行（引擎可能正常但本帧无字）',
                 probe=probe, probe_bytes=_psz)

        if not usable:
            no_line_rounds += 1
            if no_line_rounds >= 24:
                try:
                    send_keys('{ESC}', pause=0.05)
                except Exception:
                    pass
                _close_save_dialog()
                step('old_abort', warn='连滚无分页线，停止',
                     scrolls=no_line_rounds, pages=len(pages))
                if not pages:
                    return _old_finish_merge(
                        pages, target_pdf, step,
                        error=(f'旧决定连滚{no_line_rounds}次无分页线'
                               '（可能把新决定误判为旧）'))
                break
            if no_line_rounds >= 6 and pages:
                if not last_page_done and _scroll_at_bottom(win, out_dir, step):
                    ok = _save_last_page('末页已到底')
                    step('old_round', note='末页已到底', saved=ok)
                    break
                pending_search_scroll = 8
                no_line_rounds = 0
                continue
            pending_search_scroll = 3
            continue

        no_line_rounds = 0
        line_y = usable[0]
        # 164104：用屏 y 比「同屏」会误杀第2页（底部分页线 y 仍≈900）；同页靠 MD5 拒收
        probe, wr, line_y, ink, nudge_via = _nudge_up_reveal_ink(
            win, out_dir, step, wr, probe, line_y, first_page=not pages)
        line_y_img = line_y - wr[1]
        cx = _content_click_x(wr)
        cy = _click_y_on_page_above(probe, line_y_img, wr)
        step('old_click', divider_y=line_y, above_y=cy,
             ink_above=round(ink, 4), nudge=nudge_via)
        ok = _try_save_one_page(win, cx, cy, out_dir, pages, seen_hash, step)
        if not ok:
            save_fail += 1
            _close_save_dialog()
            step('old_round', warn='另存未收下，下滚跳过', fail=save_fail)
            _scroll_past_divider(win, out_dir, step, line_y)
            just_scrolled_past = True
            if save_fail >= 6:
                return _old_finish_merge(
                    pages, target_pdf, step,
                    error='另存为连续未收下', warn='partial_after_save_fail')
            continue
        save_fail = 0

        if len(pages) >= 30:
            break
        _scroll_past_divider(win, out_dir, step, line_y)
        just_scrolled_past = True

    return _old_finish_merge(pages, target_pdf, step)


def _locate_target_card(items: list, case_no: str, step):
    """结果页定位可点卡片（本服务 search 无 locate_card_for_list_item，用详情侧 locate_card_link）。"""
    loc = locate_card_link(items)
    if loc is None:
        step('locate_card', error=f'结果页未找到卡片 case_no={case_no!r}')
    elif case_no:
        step('locate_card', matched=loc[0], case_no=case_no)
    return loc


# ---------------------------------------------------------------------------
# 新决定路径：Ctrl+P → 另存为完整 PDF（一次即可，无需翻页）
# ---------------------------------------------------------------------------

_PRINT_BUDGET_S = 60.0  # Ctrl+P+另存为（与 MCP 同路径，略放宽等系统对话框）


def _abort_print_ui(step, why: str) -> None:
    """关掉打印/另存为，释放键鼠，避免死循环抢控制权。"""
    step('print_abort', warn=why)
    _close_save_dialog()
    for _ in range(3):
        try:
            send_keys('{ESC}', pause=0.08)
        except Exception:
            pass
        time.sleep(0.15)
    _close_save_dialog()


def _fetch_new_decision(win, out_dir: str, target_pdf: str, step) -> dict:
    """新决定：详情已显示则 Ctrl+P 一次另存为完整多页 PDF。

    任一步失败或超过硬时限 → ESC 退出打印/另存为并返回错误（不空转抢鼠标）。
    """
    t0 = time.time()

    def _over() -> bool:
        return (time.time() - t0) >= _PRINT_BUDGET_S

    try:
        prep = _prepare_target_pdf(target_pdf, step)
        if prep == 'reuse':
            return {
                'ok': True, 'method': 'disk_reuse', 'decision_type': 'new',
                'path': target_pdf, 'size': os.path.getsize(target_pdf),
            }
        _scroll_pdf_viewer_home(win, step, out_dir=out_dir)
        win.set_focus()
        time.sleep(0.2)
        send_keys('^p', pause=0.5)
        step('print_open', note='Ctrl+P 已发送')
        time.sleep(2.0)
        _focus_print_window(win, step)
        if _over():
            _abort_print_ui(step, 'Ctrl+P 后已超时')
            return {'ok': False, 'error': '打印流程超时', 'decision_type': 'new'}

        probe = os.path.join(out_dir, 'print_dialog.png')
        try:
            uia.screenshot_window(win, probe)
        except Exception:
            pass

        ok_dlg = _operate_print_dialog(win, out_dir, target_pdf, step, t0)
        if not ok_dlg:
            _abort_print_ui(step, '打印对话框未成功')
            return {'ok': False, 'error': '打印对话框操作失败', 'decision_type': 'new'}

        while not _over():
            if os.path.isfile(target_pdf) and os.path.getsize(target_pdf) > 0:
                step('print_save', pdf=target_pdf,
                     size=os.path.getsize(target_pdf))
                return {'ok': True, 'method': 'print_pdf', 'decision_type': 'new'}
            time.sleep(0.3)

        _abort_print_ui(step, 'PDF 未在时限内落盘')
        return {'ok': False, 'error': '打印 PDF 未在限时内落盘', 'decision_type': 'new'}
    except Exception as e:
        _abort_print_ui(step, f'异常: {e}')
        return {'ok': False, 'error': f'打印失败: {e}', 'decision_type': 'new'}


def _find_print_titled_windows():
    """仅标题为「打印」的 Chrome 打印预览窗（不扫其它应用的保存按钮）。"""
    from pywinauto import Desktop
    out = []
    seen = set()
    try:
        for w in Desktop(backend='uia').windows(
                class_name_re='Chrome_WidgetWin_1'):
            try:
                t = (w.window_text() or '').strip()
            except Exception:
                continue
            tl = t.lower()
            if (t != '打印' and not (t.endswith('打印') and len(t) <= 12)
                    and tl not in ('print', 'print preview')):
                continue
            h = getattr(w, 'handle', None)
            if h in seen:
                continue
            seen.add(h)
            out.append(w)
    except Exception:
        pass
    return out


def _print_panel_on_main(win) -> bool:
    """打印叠在 cpquery 主窗上时，a11y 应同时见「打印/另存为 PDF/页」。"""
    joined = '\n'.join(uia.all_texts(win) or [])
    if '另存为 PDF' not in joined and '另存为PDF' not in joined.replace(' ', ''):
        return False
    return '打印' in joined or '页' in joined


def _print_ui_roots(win):
    """只在打印预览范围内找「保存」，避免误点其它窗口。"""
    titled = _find_print_titled_windows()
    if titled:
        return titled
    if _print_panel_on_main(win):
        return [win]
    return []


def _focus_print_window(win, step) -> None:
    for root in _print_ui_roots(win):
        try:
            t = (root.window_text() or '').strip()
            if t == '打印' or t.endswith('打印'):
                root.set_focus()
                time.sleep(0.35)
                step('print_dialog', note='打印窗已前置', title=t[:20])
                return
        except Exception:
            continue


def _name_is_save(n: str) -> bool:
    n = (n or '').strip()
    if not n:
        return False
    nl = n.lower()
    return n == '保存' or n.startswith('保存') or nl == 'save'


def _find_print_save_button(win):
    """打印面板「保存」；优先最靠下的匹配控件（右下角蓝钮）。"""
    best = None
    best_y = -1
    for root in _print_ui_roots(win):
        try:
            for el in uia.descendants(root):
                n = uia._name(el) or ''
                if not _name_is_save(n):
                    continue
                r = uia._rect(el)
                if not r or r[3] <= r[1]:
                    continue
                cy = (r[1] + r[3]) // 2
                if cy > best_y:
                    best_y = cy
                    best = (el, root)
        except Exception:
            continue
    if best:
        return best[0], best[1]
    return None, None


def _screenshot_print_surface(win, out_dir: str) -> str:
    """打印预览截图：优先主 cpquery 窗（overlay 常叠在上面）。"""
    probe = os.path.join(out_dir, 'print_save_probe.png')
    uia.screenshot_window(win, probe)
    try:
        items = co.ocr_image(probe)
        if any('另存为' in (t or '') and 'PDF' in (t or '').upper()
               for t, _ in items):
            return probe
    except Exception:
        pass
    for pw in _find_print_titled_windows():
        try:
            pw.set_focus()
            time.sleep(0.25)
            pw.capture_as_image().save(probe)
            return probe
        except Exception:
            continue
    return probe


# 「保存」中心相对打印模态右下角（屏幕像素；保存钮在「取消」左侧约 68px）
_PRINT_SAVE_DX = 134
_PRINT_SAVE_DY = 14
# 「保存」中心在「取消」文字中心左侧（Chrome 打印侧栏，与主题色无关）
_PRINT_SAVE_LEFT_OF_CANCEL = 68


def _infer_print_modal_rect_from_ocr(probe_path: str, main_wr):
    """从截图推断居中「打印」模态框（非整浏览器、非下载条）。"""
    with Image.open(probe_path) as im:
        iw, ih = im.size
    items = co.ocr_image(probe_path)
    ox, oy = (main_wr[0], main_wr[1]) if main_wr else (0, 0)

    shelf_y = ih
    for tx, bx in items:
        t = (tx or '').strip()
        tl = t.lower()
        if '全部显示' in t or '已全部' in t or 'savetmp' in tl:
            shelf_y = min(shelf_y, bx[1])

    keys = ('另存为', 'PDF', '打印', '页面', '边距', '目标', '背景', '选项',
            '取消', '保存', '每个', '工作表')
    side = []
    for tx, bx in items:
        if bx[0] < iw * 0.38 or bx[1] >= shelf_y - 8:
            continue
        t = tx or ''
        if any(k in t for k in keys):
            side.append(bx)
            continue
        if re.search(r'\d+\s*页', t) and bx[0] > iw * 0.45:
            side.append(bx)
    if not side:
        return None

    top_i = max(0, min(b[1] for b in side) - 72)
    # OCR 常只读到侧栏顶部「另存为 PDF」；模态底在下载条之上
    prop_bottom = top_i + int((shelf_y - top_i) * 0.90)
    bottom_i = min(shelf_y - 6, max(max(b[3] for b in side) + 98, prop_bottom))
    # 模态宽约 40%～84% 屏宽（用户标红框）；右缘在设置列右侧
    right_i = min(iw - 8, max(max(b[2] for b in side) + 48, int(iw * 0.72)))
    left_i = max(0, min(int(iw * 0.40), min(b[0] for b in side) - 220))

    if bottom_i - top_i < 180 or right_i - left_i < 200:
        return None
    return (left_i + ox, top_i + oy, right_i + ox, bottom_i + oy)


def _print_dialog_screen_rect(win, probe_path: str, step=None):
    """打印模态外接矩形（屏幕坐标）。勿用与浏览器等大的「打印」hwnd 或主窗底栏。"""
    wr = uia._rect(win)
    main = (wr[0], wr[1], wr[2], wr[3]) if wr else None

    for pw in _find_print_titled_windows():
        try:
            r = pw.rectangle()
            pr = (r.left, r.top, r.right, r.bottom)
            if main:
                pw_w, pw_h = pr[2] - pr[0], pr[3] - pr[1]
                mw, mh = main[2] - main[0], main[3] - main[1]
                if pw_w >= mw * 0.92 and pw_h >= mh * 0.85:
                    if step:
                        step('print_dialog', note='打印 hwnd 与浏览器同大，改 OCR 模态框')
                    continue
            if step:
                step('print_dialog', note='使用打印 hwnd 矩形', rect=pr)
            return pr, 'print_hwnd'
        except Exception:
            pass

    modal = _infer_print_modal_rect_from_ocr(probe_path, main)
    if modal:
        if step:
            step('print_dialog', note='OCR 打印模态框', rect=modal)
        return modal, 'ocr_modal'
    if step:
        step('print_dialog', warn='未能推断打印模态框')
    return None, ''


def _print_focus_target(win):
    titled = _find_print_titled_windows()
    return titled[0] if titled else win


def _human_click_screen(focus_win, cx: int, cy: int, step, tag: str) -> None:
    """先可见移入再点击；焦点保持在打印窗，勿抢回 cpquery 主窗。"""
    sx, sy, tx, ty = uia.human_move_to(cx, cy, steps=24, dwell_s=0.9)
    step('print_dialog', note='光标已移到保存位', tag=tag,
         at=(tx, ty), from_pos=(sx, sy))
    uia.human_click(focus_win, tx, ty, set_focus=False, steps=3,
                    dwell_s=0.12, settle=0.2)


def _find_print_save_from_modal_ocr(probe_path: str, modal_rect, main_wr):
    """模态底栏 OCR「保存」/「取消」→ 保存中心（不依赖按钮颜色）。"""
    import tempfile
    try:
        with Image.open(probe_path) as im:
            im = im.convert('RGB')
            iw, ih = im.size
        ox, oy = (main_wr[0], main_wr[1]) if main_wr else (0, 0)
        _l, _t, r, b = modal_rect
        x0 = max(0, int(r - ox) - 200)
        x1 = min(iw, int(r - ox) + 6)
        y1 = min(ih, int(b - oy) + 10)
        y0 = max(0, y1 - 96)
        if x1 - x0 < 40 or y1 - y0 < 20:
            return None
        crop = im.crop((x0, y0, x1, y1))
        probe = os.path.join(tempfile.gettempdir(), 'cnipa_print_save_modal_ocr.png')
        crop.save(probe)
        saves, cancels = [], []
        for tx, bx in co.ocr_image(probe):
            c = _ocr_compact_token(tx)
            cx_i = x0 + (bx[0] + bx[2]) // 2
            cy_i = y0 + (bx[1] + bx[3]) // 2
            if '保存' in c or c == '保存':
                saves.append((cy_i, cx_i))
            if '取消' in c or c == '取消':
                cancels.append((cy_i, cx_i))
        if saves:
            saves.sort(key=lambda t: t[0], reverse=True)
            _cy, cx_i = saves[0]
            cy_i = _cy
            return (ox + cx_i, oy + cy_i)
        if cancels:
            cancels.sort(key=lambda t: t[0], reverse=True)
            cy_i, cx_i = cancels[0]
            return (ox + cx_i - _PRINT_SAVE_LEFT_OF_CANCEL, oy + cy_i)
        return None
    except Exception:
        return None


def _find_print_save_blue_center(probe_path: str, modal_rect, main_wr):
    """侧栏底部 Chrome 默认实心蓝「保存」钮（仅作 OCR/偏移失败时的加速）。"""
    try:
        with Image.open(probe_path) as im:
            im = im.convert('RGB')
            iw, ih = im.size
        ox, oy = (main_wr[0], main_wr[1]) if main_wr else (0, 0)
        _l, _t, r, b = modal_rect
        x0 = max(0, int(r - ox) - 180)
        x1 = min(iw, int(r - ox) + 4)
        y1 = min(ih, int(b - oy) + 2)
        y0 = max(0, y1 - 88)
        if x1 - x0 < 50 or y1 - y0 < 24:
            return None
        best = None
        for y in range(y1 - 8, y0, -1):
            runs = []
            x = x0
            while x < x1:
                red, green, blue = im.getpixel((x, y))
                if blue > 190 and red < 88 and green > 108:
                    rs = x
                    while x < x1:
                        red, green, blue = im.getpixel((x, y))
                        if not (blue > 190 and red < 88 and green > 108):
                            break
                        x += 1
                    if x - rs >= 36:
                        runs.append((rs, x - 1))
                else:
                    x += 1
            if not runs:
                continue
            left = min(runs, key=lambda t: t[0])
            cx_i = (left[0] + left[1]) // 2
            cy_i = y
            best = (ox + cx_i, oy + cy_i)
            break
        return best
    except Exception:
        return None


def _click_print_save_landmark(win, probe_path: str, step) -> bool:
    """保存钮：模态底栏 OCR → 蓝钮像素 → 模态右下角固定偏移。"""
    try:
        rect, via = _print_dialog_screen_rect(win, probe_path, step=step)
        if not rect:
            return False
        _left, _top, right, bottom = rect
        wr = uia._rect(win)
        main = (wr[0], wr[1], wr[2], wr[3]) if wr else None
        pt = _find_print_save_from_modal_ocr(probe_path, rect, main)
        if pt:
            cx, cy = int(pt[0]), int(pt[1])
            tag = f'modal_ocr_{via}'
        elif (blue := _find_print_save_blue_center(probe_path, rect, main)):
            cx, cy = int(blue[0]), int(blue[1])
            tag = f'blue_pixel_{via}'
        else:
            cx = int(right - _PRINT_SAVE_DX)
            cy = int(bottom - _PRINT_SAVE_DY)
            tag = f'br_offset_{via}'
        focus = _print_focus_target(win)
        try:
            focus.set_focus()
            time.sleep(0.2)
        except Exception:
            pass
        _human_click_screen(focus, cx, cy, step, tag)
        step('print_dialog', note='已点保存', via=tag,
             at=(cx, cy), dx=_PRINT_SAVE_DX, dy=_PRINT_SAVE_DY,
             dialog_br=(right, bottom))
        return True
    except Exception as e:
        step('print_dialog', warn=f'landmark 保存: {e}')
        return False


def _ocr_click_save_in_image(img, ox: int, oy: int, step, via: str) -> bool:
    import tempfile
    probe = os.path.join(tempfile.gettempdir(), 'cnipa_print_save_ocr.png')
    img.save(probe)
    hits = []
    for tx, bx in co.ocr_image(probe):
        c = _ocr_compact_token(tx)
        if '保存' not in c and c not in ('保存',):
            continue
        cx = ox + (bx[0] + bx[2]) // 2
        cy = oy + (bx[1] + bx[3]) // 2
        hits.append((cy, cx, tx))
    if not hits:
        return False
    hits.sort(key=lambda t: t[0], reverse=True)
    cy, cx, raw = hits[0]
    from pywinauto.mouse import click as _mouse_click
    _mouse_click(button='left', coords=(int(cx), int(cy)))
    step('print_dialog', note='已点保存', via=via, text=str(raw)[:12],
         at=(cx, cy))
    return True


def _click_print_save(win, out_dir: str, step) -> bool:
    if not _print_ui_roots(win):
        step('print_dialog', warn='未见打印预览，不点保存')
        return False
    _focus_print_window(win, step)
    time.sleep(0.35)
    focus = _print_focus_target(win)
    btn, _root = _find_print_save_button(win)
    if btn is not None:
        r = uia._rect(btn)
        if r:
            _human_click_screen(focus, (r[0] + r[2]) // 2,
                                (r[1] + r[3]) // 2, step, 'uia_button')
            step('print_dialog', note='已点保存', via='uia_button')
            return True
    probe = _screenshot_print_surface(win, out_dir)
    if _click_print_save_landmark(win, probe, step):
        return True
    try:
        img = Image.open(probe)
        wr = uia._rect(win) or (0, 0, img.size[0], img.size[1])
        ox, oy = wr[0], wr[1]
        if _ocr_click_save_in_image(img, ox, oy, step, 'ocr_save_text'):
            return True
    except Exception:
        pass
    step('print_dialog', warn='打印窗内未点到保存')
    return False


def _operate_print_dialog(win, out_dir: str, target_pdf: str, step,
                          t0: float) -> bool:
    """Chrome 打印：目标已是「另存为 PDF」时只点「保存」→ 系统另存为写路径。

    返回 True=已确认保存；False=任一步失败（调用方负责 abort）。
    """
    if (time.time() - t0) >= _PRINT_BUDGET_S:
        return False
    time.sleep(1.0)
    if not _click_print_save(win, out_dir, step):
        step('print_dialog', warn='未点到打印面板「保存」')
        return False
    time.sleep(1.2)

    remain = max(8.0, _PRINT_BUDGET_S - (time.time() - t0))
    dlg = _find_save_dialog(
        timeout_s=min(18.0, remain),
        probe_path=os.path.join(out_dir, 'saveas_print_probe.json'))
    if dlg is None:
        step('print_dialog', warn='未检测到另存为对话框')
        return False

    try:
        if not _fill_and_confirm_save(dlg, target_pdf, step):
            time.sleep(0.6)
            dlg2 = _find_save_dialog(timeout_s=3.0)
            if dlg2 is not None and _fill_and_confirm_save(dlg2, target_pdf, step):
                step('print_dialog', pdf=target_pdf, note='另存为重试成功')
                return True
            step('print_dialog', warn='另存为填写/确认失败')
            return False
    except Exception as e:
        step('print_dialog', warn=f'对话框操作失败: {e}')
        return False

    step('print_dialog', pdf=target_pdf, note='已键入路径并保存')
    return True


def _prepare_target_pdf(disk_path: str, step) -> str | None:
    """目标 PDF：非空已存在→reuse；0 字节→删后需另存；无文件→需另存。

    返回 'reuse' | 'need_save' | None(路径非法)。
    """
    if not disk_path:
        return None
    if not os.path.isfile(disk_path):
        return 'need_save'
    try:
        sz = os.path.getsize(disk_path)
    except OSError:
        return 'need_save'
    if sz > 0:
        step('decision_pdf', note='disk_reuse', path=disk_path, size=sz)
        return 'reuse'
    try:
        os.remove(disk_path)
        step('decision_pdf', note='removed_zero_byte', path=disk_path)
    except OSError as e:
        step('decision_pdf', warn=f'删 0 字节失败: {e}', path=disk_path)
    return 'need_save'


def download_decision_pdf_on_detail(win, out_dir: str, disk_path: str, step,
                                    card_fields: dict | None = None) -> dict:
    """详情页右栏已就绪时：识别新旧决定并落盘 PDF。

    返回 {'ok': True, 'method': ..., ...} 或 {'ok': False, 'error': ...}。
    供本 runner 与 04 详情 runner 共用，避免再走结果页点卡片。
    目标文件已存在且非 0 字节：跳过 Ctrl+P/翻页另存，直接 reuse（后续截屏 OCR）。

    card_fields：列表卡片（含 issue_date/发文日）。页内判型 unknown 时用
    发文日≥2020 补 new；仍无法判定则失败，不默认 old/new。
    """
    if not disk_path:
        return {'ok': False, 'error': '无法构造磁盘路径'}
    prep = _prepare_target_pdf(disk_path, step)
    # 懒导入：避免与 detail runner 循环依赖
    from run_cnipa_fetch_decision_detail_uia import (  # noqa: E402
        resolve_decision_type_kind,
    )
    def _disk_reuse_by_issue_date(note: str):
        dec_type, dtype_via = resolve_decision_type_kind(
            '', card_fields if isinstance(card_fields, dict) else {})
        step('decision_type', kind=dec_type or 'unknown',
             page='unknown', via=dtype_via or 'none',
             issue_date=str((card_fields or {}).get('issue_date') or '')[:32],
             note=note)
        if dec_type not in ('old', 'new'):
            return None
        print(f'[decfile] ★disk_reuse 跳过 viewer → {dec_type!r} '
              f'via={dtype_via!r}', flush=True)
        return {
            'ok': True,
            'method': 'disk_reuse',
            'decision_type': dec_type,
            'decision_type_page': 'unknown',
            'decision_type_via': dtype_via,
            'path': disk_path,
        }

    # 盘上已有 PDF：红条/空壳只刷 1 次短等；仍失败则发文日判型后 reuse
    if prep == 'reuse' and (_pdf_load_failed(win) or _detail_shell_empty(win)):
        _refresh_pdf_viewer(win, step, 'pdf_load_error_before_reuse')
        time.sleep(4.0)
        if not (_pdf_load_failed(win) or _detail_shell_empty(win)) \
                and _pdf_viewer_visible(win, out_dir):
            viewer_ok = True
        else:
            early = _disk_reuse_by_issue_date('disk_reuse_after_one_refresh')
            if early:
                return early
            viewer_ok = False
    else:
        wait_s = 18.0 if prep == 'reuse' else 45.0
        viewer_ok = _wait_pdf_viewer_ready(win, out_dir, step, timeout_s=wait_s)
    if not viewer_ok:
        if prep == 'reuse':
            early = _disk_reuse_by_issue_date('disk_reuse_skip_viewer')
            if early:
                return early
        return {
            'ok': False,
            'error': '右栏决定书 PDF 控件未渲染（已尝试浏览器刷新）',
            'decision_type': 'unknown',
        }
    _wait_decision_pages_loaded(win, step)
    try:
        sx, sy = CONTENT_X, max(280, int((uia._rect(win) or (0, 0, 1920, 900))[3] * 0.42))
        uia.human_click(win, sx, sy, settle=0.25)
    except Exception:
        pass
    page_kind = _detect_decision_type(win, out_dir, step)
    dec_type, dtype_via = resolve_decision_type_kind(
        page_kind, card_fields if isinstance(card_fields, dict) else {})
    step('decision_type', kind=dec_type or 'unknown',
         page=page_kind or 'unknown', via=dtype_via or 'none',
         issue_date=str((card_fields or {}).get('issue_date') or '')[:32])
    print(f'[decfile] ★判断结果 page={page_kind!r} → {dec_type!r} '
          f'via={dtype_via or "none"!r} (DETECT_ONLY={DETECT_ONLY})',
          flush=True)
    if DETECT_ONLY:
        step('detect_only_exit', kind=dec_type or page_kind,
             note='仅验证判断，未下载')
        return {
            'ok': True,
            'method': 'detect_only',
            'decision_type': dec_type or page_kind or 'unknown',
            'decision_type_page': page_kind or 'unknown',
            'decision_type_via': dtype_via or '',
            'detect_only': True,
            'note': f'仅判断为{dec_type or page_kind}，已退出未下载',
        }
    if not dec_type:
        return {
            'ok': False,
            'error': '无法判定新旧决定（页内 unknown，且列表发文日无法补充）',
            'decision_type': 'unknown',
            'decision_type_page': page_kind or 'unknown',
            'decision_type_via': '',
        }
    if prep == 'reuse':
        return {
            'ok': True,
            'method': 'disk_reuse',
            'decision_type': dec_type,
            'decision_type_page': page_kind or 'unknown',
            'decision_type_via': dtype_via or 'page',
            'path': disk_path,
            'size': os.path.getsize(disk_path),
        }
    if dec_type == 'new':
        # 已判定新决定：只走 Ctrl+P；失败也不回落旧决定翻页
        res = _fetch_new_decision(win, out_dir, disk_path, step)
        res.setdefault('decision_type', 'new')
        res['decision_type_page'] = page_kind or 'unknown'
        res['decision_type_via'] = dtype_via or 'page'
        return res
    # 旧决定：与 cnipa-mcp 一致，不因「非 PDF 控件」拦截（整页图 + 滚轮）
    try:
        res = _fetch_old_decision(win, out_dir, disk_path, step)
    except Exception as e:
        step('old_fetch', warn=f'异常仍尝试合并已存页: {e}')
        res = {'ok': False, 'error': str(e), 'decision_type': 'old'}
    if isinstance(res, dict):
        res.setdefault('decision_type', 'old')
        res['decision_type_page'] = page_kind or 'unknown'
        res['decision_type_via'] = dtype_via or 'page'
    pngs = sorted(glob.glob(os.path.join(out_dir, 'dec_page_*.png')))
    if pngs and not (disk_path and os.path.isfile(disk_path)
                     and os.path.getsize(disk_path) > 0):
        m = _old_finish_merge(pngs, disk_path, step, warn='exit_fallback_merge')
        m.setdefault('decision_type', 'old')
        m['decision_type_page'] = page_kind or 'unknown'
        m['decision_type_via'] = dtype_via or 'page'
        return m
    return res


def _doc_ready_from_a11y(win, out_dir: str) -> dict | None:
    """右栏已就绪 → 跳过 wait_doc 三位置滚屏（新决定 Ctrl+P 前勿乱滚）。"""
    joined = '\n'.join(uia.all_texts(win) or [])
    for m in DOC_MARKERS:
        if m in joined:
            return {'ready': True, 'marker': m, 'pos': 0,
                    'waited_s': 0, 'via': 'a11y_no_scroll'}
    try:
        probe = os.path.join(out_dir, 'probe_doc_quick.png')
        uia.screenshot_window(win, probe)
        m = doc_marker(co.ocr_image(probe))
        if m:
            return {'ready': True, 'marker': m, 'pos': 0,
                    'waited_s': 0, 'via': 'ocr_no_scroll'}
    except Exception:
        pass
    return None


def _ensure_doc_on_detail(win, out_dir, case_no, lib, step) -> dict:
    """详情页内确保决定书就绪；未就绪则左导航下钻。返回 wait_doc 结果。"""
    if lib == 'reexam':
        prefer = '复审审查决定'
    elif lib == 'invalid':
        prefer = '无效审查决定'
    else:
        prefer = '无效审查决定'
    quick = _doc_ready_from_a11y(win, out_dir)
    if quick:
        step('wait_doc', **quick)
        return quick
    doc = _wait_doc_ready(win, out_dir, step)
    step('wait_doc', **doc)
    if not doc.get('ready') and case_no:
        if _nav_drill(win, case_no, prefer, step, out_dir=out_dir):
            _wait_pdf_viewer_ready(win, out_dir, step, timeout_s=35.0)
            doc = _wait_doc_ready(win, out_dir, step, budget_s=12.0)
            step('wait_doc', **doc, after_drill=True)
    return doc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _finish(payload: dict, out_dir: str) -> None:
    """落盘 payload.json + debug.json + 契约发射。"""
    # 分离遥测
    telemetry_keys = ('mode', 'run_dir', 'input_file', 'started_at',
                      'elapsed_s', 'steps', 'detect_detail', 'nav_drilled',
                      'cleanup_ok', 'shots')
    debug = {k: payload.pop(k) for k in list(payload)
             if k in telemetry_keys}
    debug['note'] = '遥测（运行元数据/每步动作）；结果看 payload.json'

    with open(os.path.join(out_dir, 'payload.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, 'debug.json'), 'w', encoding='utf-8') as f:
        json.dump(debug, f, ensure_ascii=False, indent=2)
    cnipa_contract.maybe_emit('decision_file', payload)


def main(argv: list) -> int:
    t0 = time.time()
    if len(argv) >= 2 and argv[1] == '--selftest':
        return _selftest()
    if len(argv) < 2:
        print(__doc__)
        return 2
    with open(argv[1], 'r', encoding='utf-8-sig') as f:
        args = json.load(f)

    ts = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(ROOT, 'output', 'uia_runs', f'decfile_{ts}')
    os.makedirs(out_dir, exist_ok=True)
    payload: dict = {'mode': 'uia_decision_file', 'run_dir': out_dir,
                     'input_file': argv[1],
                     'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'steps': []}

    def step(name: str, **kw) -> None:
        kw['step'] = name
        payload['steps'].append(kw)
        print(f"[decfile] {name}: {kw}")

    # —— 入参解析 ——
    app_raw = str(args.get('application_no') or '').strip()
    case_no = str(args.get('case_no') or '').strip()
    force_refresh = bool(args.get('force_refresh'))
    lib = str(args.get('library_type') or '').strip().lower()
    app = cnipa_contract.dotted_appno(app_raw)

    payload['application_no'] = app
    payload['case_no'] = case_no

    if not app or not case_no:
        step('input', warn=f'缺少必填参数 application_no={app_raw!r} case_no={case_no!r}')
        payload['ok'] = False
        payload['error'] = 'DECISION_FILE_FAIL'
        payload['message'] = '缺少必填参数：application_no 和 case_no'
        _finish(payload, out_dir)
        return 1

    # —— 缓存优先（磁盘直查）——
    disk_path = cnipa_contract.decision_file_disk_path(app, case_no)
    if not force_refresh and disk_path and os.path.isfile(disk_path) \
            and os.path.getsize(disk_path) > 0:
        step('cache', hit='disk', path=disk_path)
        raw_out = {
            'ok': True,
            'application_no': app,
            'case_no': case_no,
            'decision_file_url': cnipa_contract.build_decision_file_url(app, case_no),
            'method': 'cached',
            'from_cache': True,
        }
        out = cnipa_contract.clean_decision_file(raw_out)
        cnipa_contract.emit_dict(out)
        try:
            os.rmdir(out_dir)
        except OSError:
            pass
        return 0

    # —— 缓存模块查 ——
    if not force_refresh:
        cached = cnipa_cache.load_decision_file(app, case_no)
        if cached is not None and cached.get('kind') == 'pdf':
            # 复制到磁盘目标路径
            src = cached['path']
            if disk_path and not os.path.isfile(disk_path):
                os.makedirs(os.path.dirname(disk_path), exist_ok=True)
                shutil.copyfile(src, disk_path)
            step('cache', hit='cache_module', path=src)
            raw_out = {
                'ok': True,
                'application_no': app,
                'case_no': case_no,
                'decision_file_url': cnipa_contract.build_decision_file_url(app, case_no),
                'method': 'cached',
                'from_cache': True,
                'cache_file': cnipa_cache.decision_file_path(app, case_no),
            }
            out = cnipa_contract.clean_decision_file(raw_out)
            cnipa_contract.emit_dict(out)
            try:
                os.rmdir(out_dir)
            except OSError:
                pass
            return 0
        if cached is not None and cached.get('kind') == 'no_file':
            step('cache', hit='no_file', note='此前确认无 PDF')
            payload['ok'] = False
            payload['error'] = 'DECISION_FILE_FAIL'
            payload['message'] = '此前已确认该决定无 PDF 文书（负缓存命中）'
            _finish(payload, out_dir)
            return 1

    # —— 窗口获取 ——
    # 本工具主路径：前台已是详情页 → 直接截图/打印 PDF。
    # a11y 经常只有「地址和搜索栏」，不能靠它拦下载；仅当明确看到
    # 「共查询到」时才走结果页点卡片。
    # 多 Tab 时不能点最后一个；必须用 case_no 校验当前详情。
    win = uia.require_window()
    step('window', title=win.window_text())
    # 只调用一次：按左栏 case_no 认 Tab，禁止后面再扫第二轮
    tab_ok = _activate_case_detail_tab(win, case_no, out_dir, step)
    if not tab_ok:
        step('tab_pick', note='未命中详情 Tab，若是结果页则继续点卡片')
    tabs_before = _tab_count(win)
    texts0 = uia.all_texts(win)
    if not uia.page_a11y_ok(texts0):
        uia.ensure_page_a11y(win)
        texts0 = uia.all_texts(win)
    joined0 = '\n'.join(texts0)
    on_results = ('共查询到' in joined0) or ('展开更多搜索项' in joined0)
    ocr_items = []
    if not on_results and not uia.page_a11y_ok(texts0):
        try:
            op = os.path.join(out_dir, 'page_state.png')
            uia.screenshot_window(win, op)
            ocr_items = co.ocr_image(op)
            oj = '\n'.join(t for t, _ in ocr_items)
            on_results = '共查询到' in oj
            step('page_state', via='ocr', on_results=on_results,
                 ocr_hits=len(ocr_items), sample=oj[:200])
        except Exception as e:
            step('page_state', ocr_warn=str(e))
    else:
        step('page_state', on_results=on_results, a11y_ok=uia.page_a11y_ok(texts0),
             sample=texts0[:20])

    if on_results:
        items = _dedup_texts(win)
        loc = _locate_target_card(items, case_no, step)
        if loc is None and ocr_items:
            wr = uia._rect(win) or (0, 0, 0, 0)
            ox, oy = wr[0], wr[1]
            screen_items = [
                (t, (b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy))
                for t, b in ocr_items]
            loc = _locate_target_card(screen_items, case_no, step)
        if loc is None:
            payload['ok'] = False
            payload['error'] = 'DECISION_FILE_FAIL'
            payload['message'] = f'结果页未找到 case_no={case_no!r} 的卡片'
            _finish(payload, out_dir)
            return 1
        kind, text, r, real = loc
        scrolls = 0
        while not real and scrolls < 2:
            uia.scroll_down(win, 960, 950, notches=6)
            scrolls += 1
            loc = _locate_target_card(_dedup_texts(win), case_no, step)
            if loc is None:
                break
            kind, text, r, real = loc
        if loc is None or not real:
            payload['ok'] = False
            payload['error'] = 'DECISION_FILE_FAIL'
            payload['message'] = '滚屏后仍拿不到卡片链接实矩形'
            _finish(payload, out_dir)
            return 1
        x, y = _link_point(r)
        uia.human_click(win, x, y)
        time.sleep(1.2)
        step('click_link', at=(x, y),
             new_tab=max(_tab_count(win), tabs_before) > tabs_before)

        def _reopen_file_card() -> bool:
            if not _on_results(win):
                uia.ensure_cpquery_results_front(win, step=step)
                time.sleep(0.35)
            loc2 = _locate_target_card(_dedup_texts(win), case_no, step)
            if loc2 is None or not loc2[3]:
                return False
            return _open_detail_from_card_click(win, step, loc2[2], case_no)

        rdy_open = _wait_detail_open(
            win, step, case_no=case_no, reopen=_reopen_file_card)
        step('wait_detail', **rdy_open)
        if not rdy_open.get('ready'):
            payload['ok'] = False
            payload['error'] = 'DECISION_FILE_FAIL'
            payload['message'] = '详情页转圈超时（已 F5 与关 Tab 重进）'
            _finish(payload, out_dir)
            return 1
        _ensure_doc_on_detail(win, out_dir, case_no, lib, step)
    else:
        # 默认详情：沿用上面唯一一次 tab_ok，不再点遍 Tab / 不再误用未回顶正文
        if not tab_ok:
            payload['ok'] = False
            payload['error'] = 'DECISION_FILE_FAIL'
            payload['message'] = (
                f'当前详情不是 case_no={case_no!r}（请打开目标决定详情或结果页）')
            _finish(payload, out_dir)
            return 1
        step('wait_doc', ready=True, via='detail_tab_ok',
             case_no=case_no, note='已在目标详情 Tab，跳过三位置滚屏')
        doc = {'ready': True}
        payload['nav_drilled'] = False

    # —— 下载 PDF（带入列表发文日供判型补充；有 PDF 则 reuse）——
    _cf = {}
    _li = args.get('list_item') if isinstance(args.get('list_item'), dict) else {}
    for _k in ('issue_date', 'decision_date'):
        _v = str(args.get(_k) or _li.get(_k) or '').strip()
        if _v:
            _cf[_k] = _v
            break
    result = download_decision_pdf_on_detail(
        win, out_dir, disk_path, step, card_fields=_cf or None)
    payload['decision_type'] = result.get('method') or ''
    payload.update(result)

    # —— 还原现场 ——
    if not args.get('keep_tab'):
        try:
            payload['cleanup_ok'] = _cleanup_tab(win, step)
        except Exception as e:
            step('cleanup', warn=str(e))

    payload['elapsed_s'] = round(time.time() - t0, 1)

    if result.get('ok'):
        payload['ok'] = True
        if result.get('detect_only'):
            payload['decision_type'] = result.get('decision_type') or ''
            payload['decision_file_url'] = ''
        else:
            payload['decision_file_url'] = cnipa_contract.build_decision_file_url(
                app, case_no)
            cnipa_cache.write_decision_file(app, case_no, disk_path)
            _dt = (result.get('decision_type') or '').strip().lower()
            if _dt in ('new', 'old'):
                cnipa_cache.write_decision_meta(
                    app, case_no, _dt, method=str(result.get('method') or ''))
    else:
        payload['ok'] = False
        payload['error'] = 'DECISION_FILE_FAIL'
        payload['message'] = result.get('error', '决定文书 PDF 下载失败')
        # 仅对确认无文书的错误写负缓存（与契约 §3B 一致：
        # 超时/打印卡死等瞬态失败不挡重试，仅 force_refresh 可绕过）
        _fail_txt = ((result.get('message') or '') + ' '
                     + (result.get('error') or '')).lower()
        if ('无 pdf' in _fail_txt or '无检索结果' in _fail_txt
                or ('未找到' in _fail_txt and '卡片' in _fail_txt)):
            cnipa_cache.write_no_decision_file(app, case_no)

    elapsed = payload['elapsed_s']
    _finish(payload, out_dir)
    print(f"[decfile] 完成：ok={payload.get('ok')}，type={payload.get('decision_type')}，"
          f"耗时 {elapsed}s，产物目录：{out_dir}")
    return 0 if payload.get('ok') else 1


def _dedup_texts(win) -> list:
    """从 win 获取去重的 (text, rect) 列表，供 locate_card_link 使用。"""
    from run_cnipa_search_decisions_uia import ordered_texts
    return ordered_texts(win)


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    # 1) 翻页正则
    check('A0) 自动缩放 OCR 变体',
          _looks_like_zoom_label('自动缩放') and _looks_like_zoom_label('+ 动 缩 放'))

    check('A) 翻页正则 "3 / 11"',
          _PAGINATION_RE.search('3 / 11') is not None)
    check('A2) 翻页正则 "1/5"',
          _PAGINATION_RE.search('1/5') is not None)
    check('A3) 翻页正则 "第2页/共10页" 不含纯数字分页',
          _PAGINATION_RE.search('第2页/共10页') is None)
    check('A4) 翻页正则 非翻页文本',
          _PAGINATION_RE.search('决定要点：维持专利权有效') is None)
    check('A5) _pag_match_new "1 / 11"',
          _pag_match_new('1 / 11') == (1, 11))
    check('A6) _pag_match_new "3/11"',
          _pag_match_new('3/11') == (3, 11))
    check('A7) 总数尾 "/ 11"',
          _pdf_toolbar_total_from_texts(['自动缩放', '/ 11']) == 11)

    # 2) 分页横线：线上方空白 + 线下有墨迹正文
    import tempfile
    td = tempfile.mkdtemp()
    probe = os.path.join(td, 'div.png')
    im = Image.new('L', (1200, 900), 255)
    for y in range(400, 408):
        for x in range(400, 1100):
            im.putpixel((x, y), 20)
    for y in range(430, 520):
        for x in range(420, 1000):
            if (x + y) % 3 == 0:
                im.putpixel((x, y), 40)
    im.save(probe)
    ys = _find_divider_screen_ys(probe, (0, 0, 1200, 900))
    check('B) 横线检测命中', len(ys) >= 1 and 395 <= ys[0] <= 410)
    try:
        os.remove(probe)
        os.rmdir(td)
    except OSError:
        pass

    # 3) URL 构造
    url = cnipa_contract.build_decision_file_url('CN2006100041736', '4W107577')
    check('C) URL 构造',
          url == '/cnipa_file/200610004173.6/4W107577.pdf')

    # 4) 磁盘路径
    dp = cnipa_contract.decision_file_disk_path('200610004173.6', '4W107577')
    check('D) 磁盘路径',
          dp.endswith(os.path.join('cnipa_file', '200610004173.6', '4W107577.pdf')))

    # 5) 缓存路径
    cp = cnipa_cache.decision_file_path('200610004173.6', '4W107577')
    check('E) 缓存路径',
          cp.endswith(os.path.join('decision_files', '200610004173.6__4W107577.pdf')))

    # 6) 投影
    proj = cnipa_contract.clean_decision_file({
        'ok': True,
        'application_no': 'CN2006100041736',
        'case_no': '4W107577',
        'decision_file_url': '/cnipa_file/200610004173.6/4W107577.pdf',
        'method': 'saveas_images',
    })
    check('F) 投影正确',
          proj.get('success') is True
          and proj.get('application_no') == '200610004173.6'
          and proj.get('case_no') == '4W107577'
          and proj.get('method') == 'saveas_images')

    # 7) 错误投影
    err = cnipa_contract.clean_decision_file({
        'ok': False,
        'error': 'DECISION_FILE_FAIL',
        'message': '详情页未就绪',
    })
    check('G) 错误投影',
          err.get('success') is False
          and err.get('error') == 'DECISION_FILE_FAIL')

    if fails:
        print(f'[selftest] {fails} FAIL')
        return 1
    print('[selftest] OK')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        crash_dir = os.path.join(ROOT, 'output', 'uia_runs')
        os.makedirs(crash_dir, exist_ok=True)
        tb = traceback.format_exc()
        with open(os.path.join(crash_dir, 'last_crash_decfile.txt'), 'w',
                  encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S\n') + tb)
        print('[decfile] 未捕获异常（已写 last_crash_decfile.txt）:\n' + tb)
        sys.exit(9)
