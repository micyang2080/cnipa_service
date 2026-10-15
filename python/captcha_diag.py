# -*- coding: utf-8 -*-
"""离线/在线诊断：滑块 CV 定位为什么失败（只读，不做任何页面交互）。

用法（用户手工执行）：
  A. 分析已有截图（复现生产 CV 判定 + 密度热图 + 候选明细）：
     python -X utf8 python/captcha_diag.py <png> [png2 ...]
  B. 在线模式（Chrome 停在滑块页面时）：现场截图一张 + 找「滑动」a11y 元素
     python -X utf8 python/captcha_diag.py --live

输出全部为文字：热图每格=该区域 Canny 边缘像素占比(%)，照片区应显著偏高；
候选明细给出每个轮廓的 w/h/密度/宽高比 及生产门槛判定（哪条不过一目了然）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cv2
import numpy as np
from PIL import Image

import run_cnipa_login_uia as R   # 复用生产同款门槛/阈值（main 有 __main__ 守卫）


def heatmap(png: str):
    img = Image.open(png)
    arr = np.asarray(img.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 100, 200)
    H, W = edges.shape
    gh, gw = 13, 24
    print('[热图] 行=图像y(每格 %dpx) 列=图像x(每格 %dpx)；数字=边缘密度%%，照片区通常 >2'
          % (H // gh, W // gw))
    print('      ' + ''.join('%3s' % (c * (W // gw) // 100) for c in range(gw))
          + '   (列标: x/100px)')
    for r_ in range(gh):
        row = []
        for c in range(gw):
            cell = edges[r_ * H // gh:(r_ + 1) * H // gh,
                         c * W // gw:(c + 1) * W // gw]
            row.append('%3d' % int(round(cell.mean() / 255.0 * 100)))
        print('y%-4d ' % (r_ * H // gh) + ''.join(row))


def candidates(png: str, scale: float):
    """镜像生产第四版管线（内容带横线 + 尺寸网格稠密搜索），打印中间量与候选。"""
    img = Image.open(png)
    arr = np.asarray(img.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 100, 200)
    H, W = edges.shape[:2]
    rowsum = edges.sum(axis=1) / 255.0
    busy = np.where(rowsum >= 0.7 * W)[0]
    lines = []
    if len(busy):
        s, p = int(busy[0]), int(busy[0])
        for y in busy[1:]:
            y = int(y)
            if y - p <= 3:
                p = y
            else:
                lines.append((s + p) // 2)
                s = p = y
        lines.append((s + p) // 2)
    print('[带线] 全宽横线 y=%r' % lines)
    y_lo, y_hi = 6, H - int(0.08 * H) - 6
    if len(lines) >= 2:
        gaps = [(lines[i + 1] - lines[i], lines[i], lines[i + 1])
                for i in range(len(lines) - 1)]
        gap, a, b = max(gaps)
        ok = gap >= 0.4 * H
        print('[内容带] 最大空档 %dpx y[%d..%d] ≥0.4H(%d)? %s'
              % (gap, a, b, int(0.4 * H), 'YES' if ok else 'NO(用兜底带)'))
        if ok:
            y_lo, y_hi = max(6, a + 4), min(H - 6, b - 4)
    y_lo = max(y_lo, int(80 * scale))
    print('[内容带] 最终搜索 y∈[%d,%d] (min_y=%d)'
          % (y_lo, y_hi, int(80 * scale)))
    q = 4
    es = edges[:H // q * q, :W // q * q].reshape(
        H // q, q, W // q, q).mean(axis=(1, 3))
    ii = cv2.integral(es)
    if isinstance(ii, tuple):
        ii = ii[0]
    lo4, hi4 = y_lo // q, y_hi // q
    sub = ii[lo4:hi4 + 1, :]
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    rows = []
    for w4 in range(max(10, int(200 * scale) // q),
                    int(560 * scale) // q + 1, 7):
        for h4 in range(max(6, int(70 * scale) // q),
                        int(240 * scale) // q + 1, 6):
            if not 1.5 <= w4 / float(h4) <= 6.0:
                continue
            if sub.shape[0] <= h4 or es.shape[1] <= w4:
                continue
            D = (sub[h4:, w4:] - sub[:-h4, w4:]
                 - sub[h4:, :-w4] + sub[:-h4, :-w4]) / float(w4 * h4)
            iy, ix = np.unravel_index(int(np.argmax(D)), D.shape)
            d = float(D[iy, ix])
            if d < 0.05:
                continue
            x, y = int(ix) * q, (lo4 + int(iy)) * q
            w, h = w4 * q, h4 * q
            sat_std = float(hsv[y:y + h, x:x + w, 1].std())
            rows.append((d, d + 0.4 * min(sat_std, 90.0) / 90.0, x, y, w, h))
    rows.sort(reverse=True)
    if not rows:
        print('[候选] 无任何窗口密度≥0.05 —— 内容带内近乎无纹理')
        return
    print('[候选] top %d（按密度降序；生产 floor=0.05）' % min(len(rows), 8))
    for d, sc, x, y, w, h in rows[:8]:
        print('  (%4d,%4d) %4dx%-4d density=%.3f score=%.3f'
              % (x, y, w, h, d, sc))


def analyze(png: str):
    img = Image.open(png)
    scale = img.width / 1096.0 if img.width > 1300 else 1.0
    print('=' * 72)
    print('文件: %s  size=%dx%d  scale≈%.2f' % (png, img.width, img.height, scale))
    rect, score = R._find_photo_rect(img, scale=scale, min_y=int(80 * scale))
    print('生产判定 _find_photo_rect → %r  score=%.3f' % (rect, score))
    candidates(png, scale)
    heatmap(png)


def live():
    uia = R.uia
    wins = uia.find_any_chrome() or []
    if not wins:
        print('[live] 未找到 Chrome 窗口（需 --force-renderer-accessibility 启动）')
        return
    win, title = None, ''
    for w in wins:
        try:
            t = (w.window_text() or '')
        except Exception:
            t = ''
        if any(k in t for k in ('认证', 'cponline', '专利')):
            win, title = w, t
            break
    if win is None:
        win = wins[0]
        try:
            title = (win.window_text() or '')
        except Exception:
            title = ''
    print('[live] 窗口: %s' % title[:60])
    r = uia._rect(win)
    print('[live] 窗口矩形(屏px): %r  大小 %dx%d' % (r, r[2] - r[0], r[3] - r[1]))
    print('[live] 「滑动」元素（生产 _slider_bar_rect 输入）:')
    hits = 0
    for el in uia.descendants(win, name_contains='滑动'):
        rr = uia._rect(el)
        name = (uia._name(el) or '')[:24]
        if rr:
            w_, h_ = rr[2] - rr[0], rr[3] - rr[1]
            gate = 120 <= w_ <= 700 and 8 <= h_ <= 90
            print('  name=%r rect=%r %dx%d %s'
                  % (name, rr, w_, h_, 'GATE-PASS' if gate else 'GATE-FAIL'))
            hits += 1
        else:
            print('  name=%r rect=None' % name)
    if not hits:
        print('  （一个都没找到 → bar nudge 从不触发的原因）')
    out = os.path.join(HERE, 'output', 'uia_runs', 'diag_live.png')
    uia.screenshot_window(win, out)
    print('[live] 现场截图 → %s' % out)
    analyze(out)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '--live' in sys.argv:
        live()
        return 0
    if not args:
        print(__doc__)
        return 1
    for png in args:
        if not os.path.exists(png):
            print('找不到文件: %r' % png)
            continue
        analyze(png)
    return 0


if __name__ == '__main__':
    sys.exit(main())
