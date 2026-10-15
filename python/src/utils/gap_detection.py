"""
拼图验证码缺口检测模块。

参考 GeekedTest (Geetest v4 solver) 的成熟方案：
- 核心算法：Canny 边缘检测 + matchTemplate 模板匹配
- 输入：干净的背景图和拼图块图（从 img/canvas 元素获取，非容器截图）
- 输出：缺口 x 坐标（像素）

设计原则：
1. 简洁：核心算法 ~30 行，不堆砌多种检测方法
2. 可靠：基于边缘形状匹配，不受颜色/亮度影响
3. 可调试：每步可保存中间结果，便于排查问题
"""

import io
import logging
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def detect_gap_by_template_matching(
    bg_bytes: bytes,
    piece_bytes: bytes,
    *,
    canny_low: int = 100,
    canny_high: int = 200,
    min_gap_x: int = 80,
    save_debug: bool = False,
    debug_dir: str = None,
) -> Optional[int]:
    """
    基于模板匹配的缺口检测（参考 GeekedTest 方案）。

    算法流程：
    1. 背景图 → Canny 边缘检测 → 边缘图
    2. 拼图块 → Canny 边缘检测 → 边缘图
    3. matchTemplate(TM_CCOEFF_NORMED) → minMaxLoc → gap_x

    Args:
        bg_bytes: 背景图字节（PNG/JPEG）
        piece_bytes: 拼图块字节（PNG，建议带透明通道）
        canny_low: Canny 低阈值（默认 100）
        canny_high: Canny 高阈值（默认 200）
        min_gap_x: 最小缺口 x（过滤左侧误检，默认 80）
        save_debug: 是否保存调试图片
        debug_dir: 调试图片保存目录

    Returns:
        缺口 x 坐标（像素），失败返回 None
    """
    try:
        # 1. 加载图片
        bg_img = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
        piece_img = Image.open(io.BytesIO(piece_bytes)).convert('RGBA')

        bg_arr = np.array(bg_img)
        piece_arr = np.array(piece_img)

        bh, bw = bg_arr.shape[:2]
        ph, pw = piece_arr.shape[:2]

        logger.info(f'背景图: {bw}x{bh}, 拼图块: {pw}x{ph}')

        # 拼图块不能比背景大
        if pw >= bw or ph >= bh:
            logger.warning(f'拼图块尺寸异常: piece({pw}x{ph}) >= bg({bw}x{bh})')
            return None

        # 2. 转灰度
        bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2GRAY)
        piece_gray = cv2.cvtColor(piece_arr, cv2.COLOR_RGBA2GRAY)

        # 3. Canny 边缘检测
        bg_edges = cv2.Canny(bg_gray, canny_low, canny_high)
        piece_edges = cv2.Canny(piece_gray, canny_low, canny_high)

        logger.debug(f'Canny 完成: bg_edges shape={bg_edges.shape}, piece_edges shape={piece_edges.shape}')

        # 4. 如果有透明通道，用 alpha 做 mask（去除拼图块外的空白）
        alpha = piece_arr[:, :, 3]
        if alpha.mean() < 250:  # 有透明通道
            # 用 alpha 通道做 mask，只保留拼图块内容区域
            piece_edges = cv2.bitwise_and(piece_edges, alpha)
            logger.debug('使用 alpha 通道做 mask')

        # 5. 转 3 通道（matchTemplate 要求）
        bg_edges_3ch = cv2.cvtColor(bg_edges, cv2.COLOR_GRAY2BGR)
        piece_edges_3ch = cv2.cvtColor(piece_edges, cv2.COLOR_GRAY2BGR)

        # 6. 模板匹配
        result = cv2.matchTemplate(bg_edges_3ch, piece_edges_3ch, cv2.TM_CCOEFF_NORMED)

        # 7. 找最佳匹配位置
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        gap_x, gap_y = max_loc

        logger.info(f'模板匹配: max_val={max_val:.3f}, gap_x={gap_x}, gap_y={gap_y}')

        # 8. 过滤左侧误检
        if gap_x < min_gap_x:
            logger.warning(f'缺口 x={gap_x} < min_gap_x={min_gap_x}，疑似误检')
            return None

        # 9. 保存调试图片
        if save_debug and debug_dir:
            _save_debug_images(bg_arr, piece_arr, gap_x, gap_y, pw, ph, debug_dir)

        return gap_x

    except Exception as e:
        logger.error(f'缺口检测失败: {e}', exc_info=True)
        return None


def _save_debug_images(
    bg_arr: np.ndarray,
    piece_arr: np.ndarray,
    gap_x: int,
    gap_y: int,
    pw: int,
    ph: int,
    debug_dir: str,
):
    """保存调试图片，便于排查问题。"""
    import os

    os.makedirs(debug_dir, exist_ok=True)

    # 1. 背景图 + 缺口位置标注
    bg_debug = cv2.cvtColor(bg_arr, cv2.COLOR_RGBA2BGR)
    cv2.rectangle(bg_debug, (gap_x, gap_y), (gap_x + pw, gap_y + ph), (0, 255, 0), 2)
    cv2.imwrite(os.path.join(debug_dir, 'debug_gap_detection.png'), bg_debug)

    # 2. 拼图块原图
    piece_debug = cv2.cvtColor(piece_arr, cv2.COLOR_RGBA2BGR)
    cv2.imwrite(os.path.join(debug_dir, 'debug_piece.png'), piece_debug)

    logger.info(f'调试图片已保存: {debug_dir}')


def detect_gap_with_fallback(
    bg_bytes: bytes,
    piece_bytes: bytes,
    **kwargs,
) -> Tuple[Optional[int], str]:
    """
    带回退的缺口检测。

    先尝试模板匹配，失败则尝试简化版（直接灰度匹配）。

    Returns:
        (gap_x, method): 缺口 x 坐标和使用的检测方法
    """
    # 1. 先尝试模板匹配（Canny + matchTemplate）
    gap_x = detect_gap_by_template_matching(bg_bytes, piece_bytes, **kwargs)
    if gap_x is not None:
        return gap_x, 'canny_template'

    # 2. 回退：直接灰度模板匹配（对某些简单场景有效）
    try:
        bg_img = Image.open(io.BytesIO(bg_bytes)).convert('L')
        piece_img = Image.open(io.BytesIO(piece_bytes)).convert('L')

        bg_arr = np.array(bg_img)
        piece_arr = np.array(piece_img)

        result = cv2.matchTemplate(bg_arr, piece_arr, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        gap_x, _ = max_loc
        min_gap_x = kwargs.get('min_gap_x', 80)

        if gap_x >= min_gap_x and max_val > 0.3:
            logger.info(f'回退方法: 灰度匹配 gap_x={gap_x}, max_val={max_val:.3f}')
            return gap_x, 'gray_template'

    except Exception as e:
        logger.warning(f'回退方法也失败: {e}')

    return None, 'failed'


if __name__ == '__main__':
    # standalone 测试
    import sys

    if len(sys.argv) < 3:
        print("用法: python gap_detection.py <bg.png> <piece.png>")
        sys.exit(1)

    with open(sys.argv[1], 'rb') as f:
        bg_bytes = f.read()
    with open(sys.argv[2], 'rb') as f:
        piece_bytes = f.read()

    gap_x, method = detect_gap_with_fallback(bg_bytes, piece_bytes, save_debug=True, debug_dir='.')
    print(f"检测结果: gap_x={gap_x}, method={method}")
