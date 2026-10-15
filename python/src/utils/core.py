"""
国知局 Playwright 辅助：人工延迟与滑块/拼图验证码。

重构说明（2026-08-31）：
- 参考 GeekedTest (Geetest v4 solver) 的成熟方案重写缺口检测
- 核心算法：Canny 边缘检测 + matchTemplate 模板匹配（~30 行核心代码）
- 模块化拆分：gap_detection.py（缺口检测）、image_capture.py（图片获取）
- 设计原则：简洁、可靠、可调试
"""

from __future__ import annotations

import io
import logging
import random
import time
from typing import Optional, Tuple

from playwright.sync_api import Locator, Page

# 导入新模块
from .gap_detection import detect_gap_by_template_matching, detect_gap_with_fallback
from .image_capture import get_captcha_images, get_yidun_images, restore_piece_visibility

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cnipa_playwright.log', encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def human_delay(min_delay: float = 0.5, max_delay: float = 2.0) -> None:
    time.sleep(random.uniform(min_delay, max_delay))


def human_drag(
    page: Page,
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float | None = None,
) -> None:
    """按住左键拖到目标位后再松开（简化版：先测试能否成功，后续再加拟人化）。"""
    end_y = end_y if end_y is not None else start_y
    logger.info('人工拖动: (%.0f,%.0f) -> (%.0f,%.0f)', start_x, start_y, end_x, end_y)

    page.mouse.move(start_x, start_y)
    time.sleep(random.uniform(0.05, 0.12))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.15))

    # 简化版：直接拖到目标位置，分 30 步，先快后慢
    total_steps = 30
    for i in range(total_steps):
        t = (i + 1) / total_steps
        # ease-out 曲线：先快后慢
        ease = 1 - (1 - t) ** 2
        cx = start_x + (end_x - start_x) * ease
        cy = start_y + (end_y - start_y) * ease
        page.mouse.move(cx, cy)
        # 延迟递增：前快后慢
        delay = 0.005 + 0.02 * t
        time.sleep(delay)

    # 精确到位后松手
    page.mouse.move(end_x, end_y)
    time.sleep(random.uniform(0.05, 0.1))
    page.mouse.up()


def _read_piece_offset(
    page: Page, panel: Locator, piece: Locator, *, content_offset_css: float = 0.0,
) -> Optional[float]:
    """读拼图块**视觉内容**左边缘相对面板左边缘的 CSS 像素偏移。

    content_offset_css: 拼图块元素内部，可见 jigsaw 内容相对元素左边缘的偏移（CSS px）。
    未传入时退化为原始的元素左边缘偏移。
    """
    try:
        offset = page.evaluate(
            """([panelSel, pieceSel]) => {
                const p = document.querySelector(panelSel);
                const s = document.querySelector(pieceSel);
                if (!p || !s) return null;
                return s.getBoundingClientRect().left - p.getBoundingClientRect().left;
            }""",
            ['.verify-img-panel', '.verify-sub-block'],
        )
        if offset is None:
            return None
        return float(offset) + content_offset_css
    except Exception:
        return None


def _detect_piece_content_offset(piece_bytes: bytes) -> int:
    """检测拼图块截图内可见 jigsaw 内容相对元素左边缘的设备像素偏移。

    拼图块元素（.verify-sub-block）内部通常有空白 padding，可见 jigsaw 形状
    并不从元素左边缘开始。需要从截图中找到内容实际起始列。

    Playwright 截图可能是**不透明**的（alpha 全 255），此时 alpha 通道无效，
    必须用边缘/颜色/方差等方法检测内容起始。

    策略：
    1. 先检查 alpha 通道是否有效（非全不透明）：有效时用 alpha 法。
    2. 不透明时，用 Canny 边缘密度：padding 区域边缘少，内容区域边缘多。
    3. 再回退列灰度方差：纹理 vs 纯色。
    4. 最后回退白色/纯色检测：找第一个非纯色列。
    """
    try:
        from PIL import Image
        import numpy as np

        img = Image.open(io.BytesIO(piece_bytes)).convert('RGBA')
        arr = np.array(img)
        alpha = arr[:, :, 3]
        h, w = alpha.shape

        # 判断截图是否真正有透明通道（alpha 不全为 255）
        alpha_mean = float(alpha.mean())
        is_opaque = alpha_mean > 250

        if not is_opaque:
            # --- 方法 1: alpha 通道（仅透明截图有效）---
            for x in range(w):
                col = alpha[:, x]
                opaque_ratio = float(np.count_nonzero(col > 30)) / h
                if opaque_ratio > 0.08:
                    logger.info('拼图内容左边缘偏移: %dpx (alpha), opaque_ratio=%.2f', x, opaque_ratio)
                    return x

        gray = np.mean(arr[:, :, :3], axis=2)  # RGBA → 灰度

        # --- 方法 2: Canny 边缘密度（不透明截图的主方法）---
        try:
            import cv2
            gray_u8 = gray.astype(np.uint8)
            edges = cv2.Canny(gray_u8, 50, 150)
            # 逐列统计边缘像素占比
            edge_density = edges.sum(axis=0) / (255.0 * h)  # 每列边缘比例
            # 跳过最左 2%（可能的边框/白边）
            min_x = max(1, int(w * 0.02))
            # 滑动窗口平滑（窗口 5px），避免单列噪声
            kernel_w = 5
            if len(edge_density) >= kernel_w:
                kernel = np.ones(kernel_w) / kernel_w
                edge_smooth = np.convolve(edge_density, kernel, mode='same')
            else:
                edge_smooth = edge_density
            # 找第一个边缘密度显著升高的位置
            # 基准：取最左 10% 区域的平均密度作为 padding 基线
            baseline_end = max(min_x + 1, int(w * 0.10))
            baseline = float(edge_smooth[min_x:baseline_end].mean()) if baseline_end > min_x else 0.0
            threshold = max(baseline * 3.0, 0.03)  # 至少 3 倍基线或 3% 密度
            for x in range(min_x, w):
                if edge_smooth[x] > threshold:
                    logger.info(
                        '拼图内容左边缘偏移: %dpx (edge), density=%.3f baseline=%.3f thr=%.3f',
                        x, edge_smooth[x], baseline, threshold,
                    )
                    return x
        except ImportError:
            pass

        # --- 方法 3: 列灰度方差（纹理 vs 纯色）---
        min_x = max(1, int(w * 0.03))
        prev_std = 0.0
        for x in range(min_x, w):
            col = gray[:, x]
            cur_std = float(np.std(col))
            if cur_std > 8.0 and cur_std > prev_std * 2.5 and cur_std > 5.0:
                logger.info('拼图内容左边缘偏移: %dpx (variance), std=%.1f', x, cur_std)
                return x
            prev_std = max(prev_std * 0.9, cur_std)

        # --- 方法 4: 纯色检测（找第一个非白/非纯色列）---
        # padding 通常是白色或接近白色，内容区域有明显颜色变化
        min_x = max(1, int(w * 0.02))
        for x in range(min_x, w):
            col = gray[:, x]
            col_std = float(np.std(col))
            col_mean = float(col.mean())
            # 非白色（mean < 240）且有变化（std > 5）
            if col_mean < 240 and col_std > 5.0:
                logger.info(
                    '拼图内容左边缘偏移: %dpx (color), mean=%.0f std=%.1f',
                    x, col_mean, col_std,
                )
                return x

        logger.info('拼图内容偏移未检出（所有方法均未命中），默认 0')
        return 0
    except Exception as exc:
        logger.warning('拼图内容偏移检测失败: %s', exc)
        return 0


def human_drag_align_piece(
    page: Page,
    *,
    move: Locator,
    piece: Locator,
    panel: Locator,
    gap_css: float,
    tol: float = 2.0,
    content_offset_css: float = 0.0,
) -> None:
    """开环粗拖 + 闭环微调（最多 2 轮，过冲可回拉）。

    1. 开环：拖到 (目标 − 12px)，宁可短勿过。
    2. 闭环（不松鼠标）：读拼图实时位置，欠拖小步补、过冲回拉。
    3. 前向修正限幅 10px；总位移不超过 need+2px。

    content_offset_css: 拼图块元素内部可见内容相对元素左边缘的 CSS 像素偏移。
    用于将「元素边缘」坐标转换为「视觉内容边缘」坐标，与缺口检测对齐。
    """
    move_box = move.bounding_box()
    panel_box = panel.bounding_box()
    piece_box = piece.bounding_box()
    if not move_box or not panel_box or not piece_box:
        raise RuntimeError('verify 滑块/拼图/面板 bounding_box 缺失')

    start_x = move_box['x'] + move_box['width'] / 2
    start_y = move_box['y'] + move_box['height'] / 2
    # 元素左边缘偏移 + 内容在元素内的偏移 = 视觉内容的实际 CSS 位置
    start_offset = (piece_box['x'] - panel_box['x']) + content_offset_css
    need = gap_css - start_offset

    if need < 80:
        raise ValueError(f'缺口拖距过小 need={need:.1f}（疑似误检），拒绝松手')

    margin = 12.0
    coarse_drag = max(80.0, need - margin)
    coarse_drag = min(coarse_drag, max(80.0, need - 4.0))
    logger.info(
        '开环粗拖: start_offset=%.1f gap_css=%.1f need=%.1f coarse_drag=%.1f (margin=%.0f)',
        start_offset, gap_css, need, coarse_drag, margin,
    )

    page.mouse.move(start_x, start_y)
    time.sleep(random.uniform(0.05, 0.10))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.14))
    _drag_to(page, start_x, start_y, start_x + coarse_drag, start_y, steps=25)

    max_loops = 2
    for loop in range(max_loops):
        time.sleep(random.uniform(0.08, 0.15))
        cur_offset = _read_piece_offset(page, panel, piece, content_offset_css=content_offset_css)
        if cur_offset is None:
            break
        err = gap_css - cur_offset
        logger.info(
            '闭环残差#%d: cur=%.1f target=%.1f err=%.1f',
            loop + 1, cur_offset, gap_css, err,
        )
        if abs(err) <= tol:
            break
        if err > 0:
            # 欠拖：保守前补，避免检测偏右时过冲
            correction = min(err, 10.0) * 0.85
        else:
            # 过冲：允许回拉
            correction = max(err, -15.0)
        if abs(correction) < 0.5:
            break
        logger.info('闭环修正#%d: %.1fpx', loop + 1, correction)
        _micro_drag(page, correction, start_y)
        time.sleep(random.uniform(0.06, 0.12))

    # 最终硬限：仍过缺口则再回拉一次
    cur_offset = _read_piece_offset(page, panel, piece, content_offset_css=content_offset_css)
    if cur_offset is not None:
        err = gap_css - cur_offset
        if err < -tol:
            pull = max(err, -12.0)
            logger.info('闭环过冲回拉: %.1fpx', pull)
            _micro_drag(page, pull, start_y)
            time.sleep(random.uniform(0.05, 0.10))
            cur_offset = _read_piece_offset(page, panel, piece, content_offset_css=content_offset_css)
        if cur_offset is not None:
            final_err = gap_css - cur_offset
            logger.info(
                '闭环最终: offset=%.1f err=%.1f %s',
                cur_offset, final_err,
                '✓' if abs(final_err) <= tol else '仍超差',
            )

    # 松手前等待足够时间，让服务器端轨迹分析看到完整的停止阶段
    # 过短的停止期（<100ms）易被识别为机器人
    time.sleep(random.uniform(0.15, 0.30))
    page.mouse.up()


def _drag_to(
    page: Page,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    *, steps: int = 25,
) -> None:
    """ease-out 曲线拖动（鼠标已按住）。"""
    for i in range(steps):
        t = (i + 1) / steps
        ease = 1 - (1 - t) ** 2
        cx = start_x + (end_x - start_x) * ease
        cy = start_y + (end_y - start_y) * ease
        page.mouse.move(cx, cy)
        delay = 0.004 + 0.016 * t
        time.sleep(delay)
    page.mouse.move(end_x, end_y)


def _micro_drag(page: Page, dx: float, y: float) -> None:
    """小幅修正拖动（3-6 步，模拟人手微调）。"""
    # Playwright 没有直接读鼠标位置的 API，用 evaluate 读 .verify-move-block 位置
    cur_x = 0.0
    try:
        cur_x = page.evaluate(
            """() => {
                const m = document.querySelector('.verify-move-block');
                return m ? m.getBoundingClientRect().left + m.getBoundingClientRect().width / 2 : 0;
            }"""
        )
    except Exception:
        cur_x = 0
    if cur_x <= 0:
        return
    steps = max(3, min(6, int(abs(dx) / 2)))
    for i in range(steps):
        t = (i + 1) / steps
        page.mouse.move(cur_x + dx * t, y)
        time.sleep(random.uniform(0.02, 0.05))
    page.mouse.move(cur_x + dx, y)


def _detect_yidun_gap_offset(bg_bytes: bytes, jigsaw_bytes: bytes) -> Tuple[Optional[int], set, int]:
    """拼图缺口 x（设备像素）+ 检测方法集合 + 拼图内容左边缘偏移（设备像素）。

    返回三元组 (gap_x, methods, content_offset_x0)：
    - gap_x: 缺口在背景图中的 x 坐标（设备像素）
    - methods: 使用的检测方法集合
    - content_offset_x0: 拼图内容在元素内的左边缘偏移（设备像素），
      与缺口检测使用同一参考点，确保拖拽对齐一致性。

    绝对亮度会随图变化（亮雪景/暗夜景）导致有时准有时偏；
    缺口特征是相对周围更暗 + 白描边/形状，与整图绝对灰度无关。
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        bg = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
        piece = Image.open(io.BytesIO(jigsaw_bytes)).convert('RGBA')
        bg_arr = np.array(bg)
        piece_arr = np.array(piece)
        ph, pw = piece_arr.shape[:2]
        bh, bw = bg_arr.shape[:2]
        if pw >= bw or ph >= bh:
            return None, set(), 0

        search_start = max(pw, int(bw * 0.15))
        min_gap = max(70, int(bw * 0.18))
        bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2GRAY)
        piece_gray = cv2.cvtColor(piece_arr, cv2.COLOR_RGBA2GRAY)
        bg_f = bg_gray.astype(np.float32)

        # --- 拼图内容框 ---
        _, otsu = cv2.threshold(piece_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if (
            int(otsu[0, 0]) + int(otsu[0, -1]) + int(otsu[-1, 0]) + int(otsu[-1, -1])
        ) // 4 > 127:
            otsu = cv2.bitwise_not(otsu)
        cw = ch = min(60, pw, ph)
        x0 = 0
        y0 = max(0, (ph - ch) // 2)
        content_mask = np.full((ch, cw), 255, dtype=np.uint8)
        best_pc, best_sc = None, -1.0
        content_offset_x0 = 0  # 拼图内容左边缘偏移（与缺口检测同一参考点）
        p90 = float(np.percentile(piece_gray, 90))
        for thr in (max(160.0, p90 - 20), p90, min(250.0, p90 + 15), 200.0, 230.0, 240.0):
            bright_pc = cv2.threshold(piece_gray, int(thr), 255, cv2.THRESH_BINARY)[1]
            for src in (bright_pc,):
                cnts, _ = cv2.findContours(src, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for c in cnts:
                    x, y, w, h = cv2.boundingRect(c)
                    if not (35 <= w <= 95 and 35 <= h <= 95):
                        continue
                    ar = w / max(h, 1)
                    if not (0.65 <= ar <= 1.5):
                        continue
                    area = float(cv2.contourArea(c))
                    fill = area / float(w * h)
                    # 优先白描边环（fill 低），拒绝大实心块（fill>0.55）
                    if fill > 0.55 or fill < 0.05:
                        continue
                    sc = area / (1.0 + abs(1.0 - ar)) * (2.5 if fill < 0.28 else 0.6)
                    # 拼图块通常在竖条中上部，底部大块常是背景纹理误检
                    if y > ph * 0.55:
                        sc *= 0.25
                    if sc > best_sc:
                        best_sc, best_pc = sc, (x, y, w, h)
        # 再试 Otsu，但同样拒绝高 fill
        cnts, _ = cv2.findContours(otsu, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (35 <= w <= 95 and 35 <= h <= 95):
                continue
            ar = w / max(h, 1)
            if not (0.65 <= ar <= 1.5):
                continue
            area = float(cv2.contourArea(c))
            fill = area / float(w * h)
            if fill > 0.55 or fill < 0.05:
                continue
            sc = area / (1.0 + abs(1.0 - ar)) * 0.5
            if y > ph * 0.55:
                sc *= 0.25
            if sc > best_sc:
                best_sc, best_pc = sc, (x, y, w, h)
        if best_pc is not None:
            x0, y0, cw, ch = best_pc
            content_offset_x0 = x0  # 轮廓检测的内容左边缘
            local = otsu[y0: y0 + ch, x0: x0 + cw]
            # 用白边环或 Otsu 局部；高 fill 时改用椭圆近似 mask
            if cv2.countNonZero(local) > cw * ch * 0.55:
                content_mask = np.zeros((ch, cw), np.uint8)
                cv2.ellipse(
                    content_mask, (cw // 2, ch // 2), (cw // 2 - 2, ch // 2 - 2),
                    0, 0, 360, 255, -1,
                )
            elif cv2.countNonZero(local) > cw * ch * 0.12:
                content_mask = local
            else:
                content_mask = np.full((ch, cw), 255, np.uint8)
            logger.info(
                '拼图内容(轮廓) bbox=(%d,%d,%d,%d) fill≈%.2f',
                x0, y0, cw, ch, best_sc,
            )
        else:
            # --- 暗区检测：找拼图块内部暗色 jigsaw 区域的实际边界 ---
            # 拼图块截图常包含大面积白色 padding，jigsaw 形状（暗色内部 + 白边框）
            # 只占元素的一部分。用 Otsu 阈值分离暗色 jigsaw 内部与亮色背景/padding。
            dark_mask = piece_gray < max(80, float(otsu.mean()) * 0.6) if otsu.mean() > 0 else piece_gray < 128
            dark_coords = np.where(dark_mask)
            content_found = False  # 标记是否找到正确的内容区域
            if len(dark_coords[0]) > pw * ph * 0.05:
                dy_min, dy_max = int(dark_coords[0].min()), int(dark_coords[0].max())
                dx_min, dx_max = int(dark_coords[1].min()), int(dark_coords[1].max())
                dw = dx_max - dx_min + 1
                dh = dy_max - dy_min + 1
                if dw >= 20 and dh >= 20:
                    pad = 3
                    x0 = max(0, dx_min - pad)
                    y0 = max(0, dy_min - pad)
                    cw = min(dw + 2 * pad, pw - x0)
                    ch = min(dh + 2 * pad, ph - y0)
                    dark_region = otsu[y0:y0 + ch, x0:x0 + cw]
                    if cv2.countNonZero(dark_region) > cw * ch * 0.15:
                        content_mask = dark_region if cv2.countNonZero(dark_region) > cw * ch * 0.12 else np.full((ch, cw), 255, np.uint8)
                        dark_mean = float(piece_gray[y0:y0 + ch, x0:x0 + cw].mean())
                        content_found = True
                        logger.info(
                            '拼图内容(暗区) bbox=(%d,%d,%d,%d) dark_mean=%.0f',
                            x0, y0, cw, ch, dark_mean,
                        )
            if not content_found:
                # 内容框检测失败：没有找到正确的 jigsaw 内容区域
                # 使用默认区域做模板匹配毫无意义，直接拒绝这次检测
                logger.warning('拼图内容检测失败（轮廓/暗区均未找到有效区域），拒绝本次检测，刷新重试')
                return None, set(), 0

        def _save(gap_x: int, y: int = 0, h: int = 60, w: int = 60) -> None:
            try:
                import os

                out_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'output',
                )
                os.makedirs(out_dir, exist_ok=True)
                dbg = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2BGR)
                cv2.rectangle(dbg, (gap_x, y), (gap_x + w, y + h), (0, 255, 0), 2)
                cv2.imwrite(os.path.join(out_dir, 'debug_gap_detection.png'), dbg)
            except Exception:
                pass

        cands: list[tuple[int, float, str, str]] = []
        min_contrast_hole = 8
        min_contrast_hollow = 10

        # --- 0: 形状约束的相对暗洞（主方法，抗绝对亮度变化）---
        # hole_map = 邻域模糊 − 原图：相对更暗处为正；用拼图 mask 做模板匹配
        blur = cv2.GaussianBlur(bg_gray, (41, 41), 0)
        hole_map = cv2.subtract(blur, bg_gray).astype(np.float32)
        hole_map[:, :search_start] = 0
        tmpl_f = content_mask.astype(np.float32)
        if (
            tmpl_f.shape[0] <= hole_map.shape[0]
            and tmpl_f.shape[1] <= hole_map.shape[1]
            and cv2.countNonZero(content_mask) > 50
        ):
            res_h = cv2.matchTemplate(hole_map, tmpl_f, cv2.TM_CCORR_NORMED)
            if res_h.shape[1] > search_start:
                res_h[:, :search_start] = -1
            _, mv_h, _, ml_h = cv2.minMaxLoc(res_h)
            if ml_h[0] >= min_gap and mv_h == mv_h and mv_h > 0.12:
                xx, yy = ml_h[0], ml_h[1]
                # 亚像素精修：抛物线插值
                xx_f = float(xx)
                if 1 <= xx < res_h.shape[1] - 1:
                    left_v = float(res_h[yy, xx - 1])
                    center_v = float(res_h[yy, xx])
                    right_v = float(res_h[yy, xx + 1])
                    denom = left_v - 2.0 * center_v + right_v
                    if abs(denom) > 1e-6:
                        sub_px = (left_v - right_v) / (2.0 * denom)
                        if abs(sub_px) < 1.0:
                            xx_f = xx + sub_px
                xi = int(round(xx_f))  # 整数索引用于数组切片
                inner = float(bg_f[yy: yy + ch, xi: xi + cw].mean())
                pad = 10
                y1, y2 = max(0, yy - pad), min(bh, yy + ch + pad)
                x1, x2 = max(0, xi - pad), min(bw, xi + cw + pad)
                outer = bg_f[y1:y2, x1:x2].copy()
                outer[yy - y1: yy - y1 + ch, xi - x1: xi - x1 + cw] = np.nan
                outer_m = float(np.nanmean(outer)) if np.any(np.isfinite(outer)) else inner
                contrast = outer_m - inner
                if contrast >= min_contrast_hole or mv_h > 0.45:
                    cands.append(
                        (xx_f, float(mv_h) * 4.5 + min(max(contrast, 0), 60) / 20.0 + 2.5,
                         'shape-hole',
                         f'conf={mv_h:.3f} contrast={contrast:.0f} y={yy}')
                    )

        # --- A: 自适应高亮 → 镂空描边（深绿底上白描边仍是最稳特征）---
        p85 = float(np.percentile(bg_gray, 85))
        p92 = float(np.percentile(bg_gray, 92))
        thr_set = {int(p85), int(p92), int((p85 + p92) / 2), 200, 220, 240}
        for thr in sorted(thr_set):
            thr = int(np.clip(thr, 120, 250))
            bright = cv2.threshold(bg_gray, thr, 255, cv2.THRESH_BINARY)[1]
            cnts, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                x, y, w, h = cv2.boundingRect(c)
                if x < search_start or not (40 <= w <= 100 and 40 <= h <= 100):
                    continue
                ar = w / max(h, 1)
                if not (0.65 <= ar <= 1.55):
                    continue
                area = float(cv2.contourArea(c))
                fill = area / float(w * h)
                if fill > 0.20:
                    continue
                pad = 6
                y1, y2 = max(0, y - pad), min(bh, y + h + pad)
                x1, x2 = max(0, x - pad), min(bw, x + w + pad)
                outer = bg_f[y1:y2, x1:x2].copy()
                outer[y - y1: y - y1 + h, x - x1: x - x1 + w] = np.nan
                om = np.nanmean(outer)
                outer_mean = float(om) if np.isfinite(om) else 255.0
                inner_mean = float(bg_f[y: y + h, x: x + w].mean())
                contrast = outer_mean - inner_mean
                if fill > 0.12 and contrast < min_contrast_hollow:
                    continue
                if fill <= 0.12 and contrast < 2:
                    continue
                weight = (0.20 - fill) * 8.0 + min(max(contrast, 0), 80) / 35.0
                cands.append(
                    (x, weight + 2.0, 'hollow',
                     f'thr={thr} fill={fill:.3f} contrast={contrast:.0f}')
                )

        # --- B: 形状边缘（对颜色不敏感）---
        bg_blur = cv2.GaussianBlur(bg_gray, (3, 3), 0)
        bg_edges = cv2.Canny(bg_blur, 60, 140)
        bg_edges[:, :search_start] = 0
        piece_edges = cv2.Canny(content_mask, 40, 120)
        k = max(3, int(min(cw, ch) * 0.1) | 1)
        if k % 2 == 0:
            k += 1
        ring = cv2.subtract(content_mask, cv2.erode(content_mask, np.ones((k, k), np.uint8)))
        tmpl = cv2.bitwise_and(piece_edges, ring)
        if cv2.countNonZero(tmpl) < 40:
            tmpl = piece_edges
        # 在整高搜索（拼图 Y 可能不准），取最大 conf
        if tmpl.shape[0] <= bh and tmpl.shape[1] <= bw:
            res = cv2.matchTemplate(bg_edges, tmpl, cv2.TM_CCOEFF_NORMED)
            # 左侧遮罩：把 result 中对应 search_start 之前置极低
            if res.size:
                # result 宽 = bw-tmpl_w+1，列 j 对应 x=j
                cut = max(0, search_start)
                if cut < res.shape[1]:
                    res[:, :cut] = -1
                _, mv, _, ml = cv2.minMaxLoc(res)
                if ml[0] >= search_start and 0.20 <= mv <= 1.0 and mv == mv:
                    cands.append((ml[0], float(mv) * 3.0 + 1.0, 'edge', f'conf={mv:.3f}'))

        # --- C: 相对暗洞 ---
        mask_bin = (content_mask > 0).astype(np.float32)
        ms = float(mask_bin.sum()) or 1.0
        best_rel = None  # (score, x, y)
        pad = 8
        min_rel = 15
        for yy in range(0, max(1, bh - ch), 3):
            for x in range(search_start, bw - cw, 2):
                inner = bg_f[yy: yy + ch, x: x + cw]
                if inner.shape != (ch, cw):
                    continue
                inner_m = float((inner * mask_bin).sum() / ms)
                y1, y2 = max(0, yy - pad), min(bh, yy + ch + pad)
                x1, x2 = max(0, x - pad), min(bw, x + cw + pad)
                outer = bg_f[y1:y2, x1:x2].copy()
                oy, ox = yy - y1, x - x1
                outer[oy: oy + ch, ox: ox + cw] = np.nan
                if not np.isfinite(np.nanmean(outer)):
                    continue
                outer_m = float(np.nanmean(outer))
                contrast = outer_m - inner_m
                if contrast < min_rel:
                    continue
                if best_rel is None or contrast > best_rel[0]:
                    best_rel = (contrast, x, yy)
        if best_rel is not None:
            contrast, x, yy = best_rel
            w_rel = min(contrast, 90) / 40.0 + 1.0
            cands.append(
                (x, w_rel, 'rel-dark',
                 f'contrast={contrast:.0f} y={yy}')
            )

        cands = [c for c in cands if c[0] >= min_gap]
        if not cands:
            logger.warning('缺口检测无可靠候选（相对对比度/形状均未命中）')
            return None, set(), content_offset_x0

        # --- 背景亮度自适应：暗背景下调整方法权重 ---
        # shape-hole / rel-dark 依赖亮度对比，暗背景下精度下降；
        # edge / hollow 基于结构特征（边缘/白描边），对亮度不敏感。
        bg_brightness = float(bg_gray.mean())
        cluster_radius = 18
        if bg_brightness < 100:
            darkness = max(0.3, bg_brightness / 100.0)
            bright_cands = []
            for x, w, m, meta in cands:
                if m in ('edge', 'hollow'):
                    bright_cands.append((x, w / darkness, m, meta))
                elif m in ('shape-hole', 'rel-dark'):
                    bright_cands.append((x, w * darkness, m, meta))
                else:
                    bright_cands.append((x, w, m, meta))
            cands = bright_cands
            cluster_radius = 22
            logger.info(
                '背景偏暗 brightness=%.0f → 提升 edge/hollow 权重(×%.2f)，'
                '降低 shape-hole/rel-dark 权重(×%.2f)，聚类半径=%d',
                bg_brightness, 1.0 / darkness, darkness, cluster_radius,
            )

        cands.sort(key=lambda t: t[0])
        clusters: list[list[tuple[int, float, str, str]]] = []
        for item in cands:
            if not clusters or abs(item[0] - clusters[-1][0][0]) > cluster_radius:
                clusters.append([item])
            else:
                clusters[-1].append(item)

        def _cluster_score(cl: list[tuple[int, float, str, str]]) -> tuple[float, float, int]:
            wsum = sum(w for _, w, _, _ in cl)
            xbar = sum(x * w for x, w, _, _ in cl) / wsum
            methods = {m for _, _, m, _ in cl}
            bonus = 0.5 * max(0, len(methods) - 1)
            if methods & {'hollow', 'edge', 'shape-hole'}:
                bonus += 1.2
            if 'shape-hole' in methods:
                bonus += 1.0
            # 仅 rel-dark 时降权（无多方法交叉验证，可信度低）
            if methods == {'rel-dark'}:
                bonus -= 0.5
            return wsum + bonus, xbar, len(cl)

        scored = [(_cluster_score(cl), cl) for cl in clusters]
        scored.sort(key=lambda t: t[0][0], reverse=True)
        (score, xbar, n), best_cl = scored[0]
        gap_x = int(round(xbar))
        # 提取最佳聚类使用的检测方法集合
        best_methods = {m for _, _, m, _ in best_cl}
        # 记录 y：优先 hollow/edge 同簇的 y，否则 rel-dark 的 y
        yy_out = y0
        for _, _, m, meta in best_cl:
            if 'y=' in meta:
                try:
                    yy_out = int(meta.split('y=')[1].split()[0])
                except Exception:
                    pass

        # --- 亮度剖面左边缘精修 ---
        # 模板匹配峰值对应阴影最匹配位置，但缺口视觉左边缘是亮度骤降处。
        # 从峰值向左扫描，找到亮度梯度最大处作为精确左边缘。
        # 注意：仅做微修（≤5px），大幅偏移说明梯度检测误判，应保留原始位置。
        gap_x_refined = gap_x
        try:
            scan_y_start = max(0, yy_out - 5)
            scan_y_end = min(bh, yy_out + ch + 5)
            strip = bg_f[scan_y_start:scan_y_end, :]
            col_mean = strip.mean(axis=0)  # 每列平均亮度
            # 在峰值附近 ±15px 范围内扫描亮度梯度（缩小范围，避免误检远处梯度）
            scan_left = max(search_start, gap_x - 15)
            scan_right = min(bw - 1, gap_x + 15)
            if scan_right > scan_left + 5:
                gradient = np.abs(np.diff(col_mean[scan_left:scan_right + 1]))
                # 找梯度最大处 = 缺口左边缘（从亮到暗的骤降）
                peak_grad = int(np.argmax(gradient)) + scan_left
                # 验证：该处左侧应比右侧亮（缺口是暗区），且亮度差足够大
                left_brightness = float(col_mean[max(0, peak_grad - 8):peak_grad + 1].mean())
                right_brightness = float(col_mean[peak_grad:min(bw, peak_grad + 9)].mean())
                brightness_drop = left_brightness - right_brightness
                if brightness_drop > 8:  # 要求更显著的亮度下降（原阈值 3）
                    # 亮度下降方向正确且幅度足够，使用精修位置
                    shift = gap_x - peak_grad
                    if 0 < shift <= 5:  # 仅允许向左微修 ≤5px（原 12px 过大）
                        gap_x_refined = peak_grad
                        logger.info(
                            '缺口左边缘精修: %d → %d (shift=%+d, left_bright=%.0f right_bright=%.0f drop=%.0f)',
                            gap_x, gap_x_refined, shift, left_brightness, right_brightness, brightness_drop,
                        )
        except Exception:
            pass
        gap_x = gap_x_refined

        # --- 暗背景下门禁：单方法检测不可靠 ---
        if bg_brightness < 100 and len(best_methods) == 1 and score < 8.0:
            logger.warning(
                '暗背景下仅 %s 独赢 (score=%.1f<8.0)，拒绝拖动，刷新重试',
                best_methods, score,
            )
            return None, set(), content_offset_x0

        detail = '; '.join(f'{m}:{meta}' for _, _, m, meta in best_cl)
        logger.info(
            '拼图缺口检测: x=%s score=%.2f n=%d methods=%s cluster=[%s]',
            gap_x, score, n, sorted(best_methods), detail,
        )
        _save(gap_x, yy_out, ch, cw)
        return gap_x, best_methods, content_offset_x0
    except Exception as exc:
        logger.warning('拼图缺口检测失败: %s', exc)
        return None, set(), 0


def _get_yidun_images(page: Page) -> Tuple[Optional[bytes], Optional[bytes]]:
    bg_selectors = (
        '.yidun_bg-img',
        'img.yidun_bg-img',
        '.yidun_bgimg',
        '[class*="yidun"] img[class*="bg"]',
    )
    piece_selectors = (
        '.yidun_jigsaw',
        'img.yidun_jigsaw',
        '[class*="yidun"] img[class*="jigsaw"]',
    )
    bg_bytes: Optional[bytes] = None
    piece_bytes: Optional[bytes] = None

    for sel in bg_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                src = el.get_attribute('src') or ''
                if src.startswith('data:'):
                    import base64

                    bg_bytes = base64.b64decode(src.split(',', 1)[1])
                elif src.startswith('http'):
                    resp = page.request.get(src)
                    if resp.ok:
                        bg_bytes = resp.body()
                if bg_bytes:
                    break
        except Exception:
            continue

    for sel in piece_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                src = el.get_attribute('src') or ''
                if src.startswith('data:'):
                    import base64

                    piece_bytes = base64.b64decode(src.split(',', 1)[1])
                elif src.startswith('http'):
                    resp = page.request.get(src)
                    if resp.ok:
                        piece_bytes = resp.body()
                if piece_bytes:
                    break
        except Exception:
            continue
    return bg_bytes, piece_bytes


def _detect_puzzle_gap_offset(bg_bytes: bytes, piece_bytes: bytes) -> Tuple[Optional[int], set, int]:
    """
    拼图缺口 x 偏移（像素）+ 检测方法集合 + 拼图内容左边缘偏移。

    重构后（2026-08-31）：调用新的 detect_gap_with_fallback，返回兼容旧接口的三元组。
    """
    gap_x, method = detect_gap_with_fallback(bg_bytes, piece_bytes)
    if gap_x is None:
        return None, set(), 0
    # 干净图片不需要 content_offset
    return gap_x, {method}, 0


def handle_cnipa_verify_captcha(page: Page, *, max_attempts: int = 3) -> bool:
    """
    国知局 tysf 登录页 verify 滑块（点登录后出现 .verify-move-block）。

    重构后（2026-08-31）：
    - 使用新模块 get_captcha_images 获取干净图片
    - 使用新模块 detect_gap_with_fallback 检测缺口（Canny + matchTemplate）
    - 简化逻辑：去掉复杂的 content_offset 计算（干净图片不需要）
    """
    try:
        page.wait_for_selector('.verify-move-block', timeout=12_000)
    except Exception:
        logger.info('未出现 verify 滑块，可能无需验证码')
        return True

    for attempt in range(max_attempts):
        logger.info('verify 滑块尝试 %s/%s', attempt + 1, max_attempts)
        try:
            page.wait_for_selector('.verify-img-panel', timeout=5000)
            time.sleep(0.6)

            # 1. 获取干净的背景图和拼图块图
            bg_bytes, piece_bytes, meta = get_captcha_images(
                page,
                bg_selector='.verify-img-panel',
                piece_selector='.verify-sub-block',
                piece_img_selector='.verify-sub-block img',
                piece_canvas_selector='.verify-sub-block canvas',
            )

            if not bg_bytes or not piece_bytes:
                logger.warning(f'图片获取失败: bg={bool(bg_bytes)}, piece={bool(piece_bytes)}')
                _click_verify_refresh(page)
                time.sleep(1.5)
                continue

            logger.info(f'图片获取成功: bg={meta.get("bg_source")}, piece={meta.get("piece_source")}')

            # 保存调试图片
            import os as _os
            _out = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', 'output')
            _os.makedirs(_out, exist_ok=True)
            open(_os.path.join(_out, 'captcha_bg.png'), 'wb').write(bg_bytes)
            open(_os.path.join(_out, 'captcha_piece.png'), 'wb').write(piece_bytes)

            # 2. 检测缺口位置（使用新模块）
            gap_x, method = detect_gap_with_fallback(
                bg_bytes, piece_bytes,
                min_gap_x=80,
                save_debug=True,
                debug_dir=_out,
            )

            if gap_x is None:
                logger.warning('缺口检测失败')
                _click_verify_refresh(page)
                time.sleep(1.5)
                continue

            logger.info(f'缺口检测成功: gap_x={gap_x}, method={method}')

            # 3. 计算 CSS 坐标
            from PIL import Image as _Img
            panel_img = _Img.open(io.BytesIO(bg_bytes))
            panel = page.locator('.verify-img-panel').first
            panel_box = panel.bounding_box()

            if not panel_box or panel_box['width'] <= 0:
                logger.warning('面板 bounding_box 无效')
                continue

            # screenshot 为设备像素，bounding_box 为 CSS 像素
            dpr = panel_img.width / panel_box['width']
            gap_css = gap_x / dpr

            # 容器截图包含空白 padding，需要检测内容起始位置并修正
            content_offset_css = 0.0
            if meta.get('piece_source') == 'container_screenshot':
                # 容器截图 = 包含空白 padding，matchTemplate 找到的是内容在背景中的位置，
                # 但拼图块元素左边缘可能从 padding 开始，需要修正
                content_offset_px = _detect_piece_content_offset(piece_bytes)
                content_offset_css = content_offset_px / dpr
                logger.info(f'容器截图内容偏移: {content_offset_px}px (设备) = {content_offset_css:.1f}px (CSS)')
            else:
                logger.info(f'干净图片源 ({meta.get("piece_source")})，无需内容偏移修正')

            logger.info(f'坐标转换: gap_x={gap_x}px, dpr={dpr:.2f}, gap_css={gap_css:.1f}, content_offset_css={content_offset_css:.1f}')

            # 4. 执行拖拽
            move = page.locator('.verify-move-block').first
            piece = page.locator('.verify-sub-block').first

            if not move.bounding_box():
                logger.warning('滑块 bounding_box 无效')
                continue

            try:
                human_drag_align_piece(
                    page, move=move, piece=piece, panel=panel, gap_css=gap_css,
                    content_offset_css=content_offset_css,
                )
            except ValueError as ve:
                logger.warning('拖拽失败: %s', ve)
                _click_verify_refresh(page)
                time.sleep(1.5)
                continue

            time.sleep(1.2)

            # 5. 验证结果
            if _verify_captcha_passed(page):
                logger.info('verify 滑块验证成功')
                restore_piece_visibility(page, '.verify-sub-block')
                return True

            logger.info('verify 拖动未通过，刷新重试')
            _click_verify_refresh(page)
            # 等待新验证码图片加载完成（刷新后图片 URL 会变）
            time.sleep(1.5)
        except Exception as exc:
            logger.warning('verify 滑块异常: %s', exc)
            _click_verify_refresh(page)
            # 异常后刷新同样等待新图加载，与正常刷新分支保持一致
            time.sleep(1.5)

    return False


def _click_verify_refresh(page: Page) -> None:
    for sel in ('.verify-refresh', '.icon-refresh', '[class*="refresh"]'):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click()
                return
        except Exception:
            continue


def _verify_captcha_passed(page: Page) -> bool:
    """判断 verify 滑块验证是否通过。

    拖完后页面可能需要短暂动画时间，先等滑块消失或状态文案出现再判断。
    避免「滑块仍可见 + 文案未更新」时误判为失败。
    """
    # 等待一段时间观察滑块是否消失（成功时滑块通常会被隐藏或移除）
    try:
        page.locator('.verify-move-block').first.wait_for_element_state('hidden', timeout=2000)
        return True
    except Exception:
        pass

    # 滑块仍在，检查状态文案
    try:
        if page.locator('.verify-move-block').count() == 0:
            return True
    except Exception:
        return True

    # 检查成功/失败文案
    try:
        msg = page.locator('.verify-msg').first.inner_text(timeout=800)
        if any(k in (msg or '') for k in ('成功', '通过')):
            return True
        if any(k in (msg or '') for k in ('失败', '错误', '重试')):
            return False
    except Exception:
        pass

    # 检查是否有成功遮罩或面板消失（部分实现成功后面板会淡出）
    try:
        panel = page.locator('.verify-img-panel').first
        if not panel.is_visible(timeout=500):
            return True
    except Exception:
        return True

    # 检查 verify 整体容器是否出现了成功样式类
    try:
        verify_el = page.locator('.verify, [class*="verify-box"]').first
        if verify_el.count() > 0:
            cls = verify_el.get_attribute('class') or ''
            if 'success' in cls or 'pass' in cls:
                return True
    except Exception:
        pass

    # 无法确定状态时，返回 False 让外层刷新重试
    return False


def handle_slider_captcha(
    page: Page,
    slider_selector: str | None = None,
    track_selector: str | None = None,
    provider: str = 'CNIPA',
) -> bool:
    try:
        logger.info('[%s] 开始处理滑块验证码...', provider)

        if provider == 'CNIPA':
            if page.locator('.verify-move-block').count() > 0:
                return handle_cnipa_verify_captcha(page)
            try:
                page.wait_for_selector('.verify-move-block', timeout=3000)
                return handle_cnipa_verify_captcha(page)
            except Exception:
                pass

        if provider == 'CNIPA':
            try:
                page.wait_for_selector(
                    '.yidun_slider__button, .yidun, [class*="slider-btn"], [class*="drag-btn"]',
                    timeout=8000,
                )
            except Exception:
                pass

        slider_button_selectors = [
            slider_selector,
            '.yidun_slider__button',
            '.c-slider-btn',
            '.drag-btn',
            '.dragBtn',
            '[class*="slider-btn"]',
            '[class*="drag-btn"]',
            '[class*="dragBtn"]',
        ]
        track_selectors = [
            track_selector,
            '.yidun_slider',
            '.yidun_control',
            '.c-slider',
            '.drag-progress',
            '.dragProgress',
            '[class*="slider-track"]',
            '[class*="drag-progress"]',
        ]

        slider_button = None
        for selector in slider_button_selectors:
            if not selector:
                continue
            try:
                element = page.wait_for_selector(selector, timeout=3000)
                if element and element.is_visible():
                    box = element.bounding_box()
                    if box and box['width'] < 120:
                        slider_button = element
                        logger.info('找到滑块按钮: %s', selector)
                        break
            except Exception:
                continue

        if not slider_button:
            if provider == 'CNIPA':
                logger.warning('未找到滑块，登录可能未弹出验证码')
                return False
            logger.info('未找到滑块按钮，可能不需要验证码')
            return True

        track = None
        for selector in track_selectors:
            if not selector:
                continue
            try:
                element = page.wait_for_selector(selector, timeout=2000)
                if element and element.is_visible():
                    track = element
                    break
            except Exception:
                continue

        puzzle_offset: Optional[float] = None
        if provider == 'CNIPA':
            # 使用新模块获取图片和检测缺口
            bg_bytes, piece_bytes = get_yidun_images(page)
            if bg_bytes and piece_bytes:
                gap_x, method = detect_gap_with_fallback(bg_bytes, piece_bytes)
                if gap_x is not None and track:
                    track_box = track.bounding_box()
                    bg_img = None
                    try:
                        from PIL import Image

                        bg_img = Image.open(io.BytesIO(bg_bytes))
                    except Exception:
                        pass
                    if track_box and bg_img:
                        scale = track_box['width'] / bg_img.width
                        puzzle_offset = gap_x * scale
                        logger.info('拼图拖动距离: %.1fpx (gap=%s scale=%.3f method=%s)', puzzle_offset, gap_x, scale, method)

        max_attempts = 5 if provider == 'CNIPA' else 3
        for attempt in range(max_attempts):
            logger.info('滑块拖动尝试 %s/%s', attempt + 1, max_attempts)
            # 等待 DOM 稳定后再获取位置，避免动画中间状态
            time.sleep(0.5)
            slider_box = slider_button.bounding_box()
            if not slider_box:
                continue

            start_x = slider_box['x'] + slider_box['width'] / 2
            start_y = slider_box['y'] + slider_box['height'] / 2

            if puzzle_offset is not None:
                final_x = start_x + puzzle_offset + random.uniform(-2, 4)
                final_y = start_y
            elif track:
                track_box = track.bounding_box()
                if track_box:
                    final_x = track_box['x'] + track_box['width'] + random.uniform(40, 70)
                    final_y = track_box['y'] + track_box['height'] / 2
                else:
                    final_x = start_x + 280
                    final_y = start_y
            else:
                final_x = start_x + 280
                final_y = start_y

            try:
                human_drag(page, start_x=start_x, start_y=start_y, end_x=final_x, end_y=final_y)
                time.sleep(0.35)
            except Exception as exc:
                logger.warning('拖动失败: %s', exc)
                continue

            time.sleep(1.8)
            if _check_captcha_success(page, slider_button):
                logger.info('滑块验证成功')
                return True

            if provider == 'CNIPA' and attempt < max_attempts - 1:
                # 先刷新验证码，再重新检测缺口位置（刷新后缺口会变）
                _click_refresh_captcha(page)
                time.sleep(1.2)
                bg_bytes, piece_bytes = get_yidun_images(page)
                if bg_bytes and piece_bytes:
                    gap_x, method = detect_gap_with_fallback(bg_bytes, piece_bytes)
                    if gap_x is not None and track:
                        track_box = track.bounding_box()
                        try:
                            from PIL import Image

                            bg_img = Image.open(io.BytesIO(bg_bytes))
                            if track_box:
                                puzzle_offset = gap_x * (track_box['width'] / bg_img.width)
                                logger.info('刷新后重新检测: gap=%s scale=%.3f offset=%.1f method=%s', gap_x, track_box['width'] / bg_img.width, puzzle_offset, method)
                        except Exception:
                            pass

        logger.warning('滑块验证未成功')
        return False
    except Exception as exc:
        logger.error('处理滑块验证码时发生异常: %s', exc)
        return False


def _click_refresh_captcha(page: Page) -> None:
    for sel in ('.yidun_refresh', '[class*="refresh"]', 'button:has-text("刷新")'):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click()
                return
        except Exception:
            continue


def _check_captcha_success(page: Page, slider: Locator) -> bool:
    try:
        time.sleep(0.6)
        body_text = ''
        try:
            body_text = page.inner_text('body') or ''
        except Exception:
            pass
        if any(k in body_text for k in ('拖动成功', '验证成功', '验证通过')):
            return True
        # 使用 text= 选择器检测失败提示，比 :has-text() 伪类更可靠
        for fail_text in ('验证失败', '请重试', '拖动失败'):
            try:
                el = page.locator(f'text={fail_text}').first
                if el.is_visible(timeout=800):
                    return False
            except Exception:
                continue
        try:
            if not slider.is_visible():
                return True
            slider.wait_for_element_state('hidden', timeout=1200)
            return True
        except Exception:
            pass
        captcha_panel = page.locator('.yidun, [class*="yidun"]').first
        if captcha_panel.count() == 0 or not captcha_panel.is_visible():
            return True
        return False
    except Exception:
        return False
