"""
验证码图片获取模块。

负责从页面获取干净的背景图和拼图块图。

设计原则：
1. 优先从 img/canvas 元素获取（干净图片）
2. 回退到截图（带空白，需要后续处理）
3. 支持多种验证码类型（CNIPA verify、易盾等）
"""

import base64
import logging
from typing import Optional, Tuple

from playwright.sync_api import Page, Locator

logger = logging.getLogger(__name__)


def get_captcha_images(
    page: Page,
    *,
    bg_selector: str = '.verify-img-panel',
    piece_selector: str = '.verify-sub-block',
    piece_img_selector: str = '.verify-sub-block img',
    piece_canvas_selector: str = '.verify-sub-block canvas',
) -> Tuple[Optional[bytes], Optional[bytes], dict]:
    """
    获取验证码的背景图和拼图块图。

    策略：
    1. 背景图：截图 bg_selector（通常是 .verify-img-panel）
    2. 拼图块：
       a. 优先找 piece_img_selector（img 元素）→ 截图或从 src 获取
       b. 其次找 piece_canvas_selector（canvas 元素）→ 截图或 toDataURL
       c. 回退到截图 piece_selector（容器，可能带空白）

    Args:
        page: Playwright Page 对象
        bg_selector: 背景图容器选择器
        piece_selector: 拼图块容器选择器
        piece_img_selector: 拼图块 img 元素选择器
        piece_canvas_selector: 拼图块 canvas 元素选择器

    Returns:
        (bg_bytes, piece_bytes, meta):
        - bg_bytes: 背景图字节
        - piece_bytes: 拼图块字节
        - meta: 元信息（图片来源、尺寸等）
    """
    meta = {
        'bg_source': None,
        'piece_source': None,
        'bg_size': None,
        'piece_size': None,
    }

    bg_bytes = None
    piece_bytes = None

    # 0. DOM 诊断（首次尝试前，了解拼图块渲染方式）
    try:
        dom_info = _diagnose_piece_dom(page, piece_selector)
        meta['piece_dom'] = dom_info
        logger.info(f'拼图块 DOM 诊断: {dom_info}')
    except Exception as e:
        logger.debug(f'DOM 诊断跳过: {e}')

    # 1. 获取背景图
    try:
        bg_locator = page.locator(bg_selector).first
        if bg_locator.count() > 0 and bg_locator.is_visible():
            # 截图前隐藏拼图块，避免干扰
            _hide_piece_temporarily(page, piece_selector)

            bg_bytes = bg_locator.screenshot()
            meta['bg_source'] = 'screenshot'

            # ★ 关键：背景截图后立即恢复拼图块可见性，否则后续所有拼图块获取方法都会失败
            restore_piece_visibility(page, piece_selector)

            from PIL import Image
            import io
            img = Image.open(io.BytesIO(bg_bytes))
            meta['bg_size'] = img.size

            logger.info(f'背景图获取成功: {img.size[0]}x{img.size[1]} via screenshot')
    except Exception as e:
        logger.warning(f'背景图获取失败: {e}')
        # 即使背景截图失败，也要恢复可见性
        restore_piece_visibility(page, piece_selector)

    # 2. 获取拼图块图（按优先级尝试）
    # 2a. 优先从 img 元素获取
    if not piece_bytes:
        try:
            img_locator = page.locator(piece_img_selector).first
            img_count = img_locator.count()
            logger.info(f'拼图块 img 查找: selector={piece_img_selector}, count={img_count}')
            if img_count > 0 and img_locator.is_visible():
                src = img_locator.get_attribute('src') or ''
                logger.info(f'拼图块 img src: {src[:80]}...' if len(src) > 80 else f'拼图块 img src: {src}')
                if src.startswith('data:'):
                    piece_bytes = base64.b64decode(src.split(',', 1)[1])
                    meta['piece_source'] = 'img_data_url'
                elif src.startswith('http'):
                    resp = page.request.get(src)
                    if resp.ok:
                        piece_bytes = resp.body()
                        meta['piece_source'] = 'img_http'

                if piece_bytes:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(piece_bytes))
                    meta['piece_size'] = img.size
                    logger.info(f'拼图块获取成功: {img.size[0]}x{img.size[1]} via img src')
            else:
                logger.warning(f'拼图块 img 不存在或不可见: count={img_count}')
        except Exception as e:
            logger.warning(f'img 元素获取失败: {e}')

    # 2b. 从 canvas 元素获取
    if not piece_bytes:
        try:
            canvas_locator = page.locator(piece_canvas_selector).first
            canvas_count = canvas_locator.count()
            logger.info(f'拼图块 canvas 查找: selector={piece_canvas_selector}, count={canvas_count}')
            if canvas_count > 0 and canvas_locator.is_visible():
                data_url = page.evaluate("""(canvas) => {
                    return canvas.toDataURL('image/png');
                }""", canvas_locator.element_handle())

                if data_url and data_url.startswith('data:'):
                    piece_bytes = base64.b64decode(data_url.split(',', 1)[1])
                    meta['piece_source'] = 'canvas_toDataURL'

                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(piece_bytes))
                    meta['piece_size'] = img.size
                    logger.info(f'拼图块获取成功: {img.size[0]}x{img.size[1]} via canvas toDataURL')
            else:
                logger.warning(f'拼图块 canvas 不存在或不可见: count={canvas_count}')
        except Exception as e:
            logger.warning(f'canvas 元素获取失败: {e}')

    # 2c. 尝试 CSS background-image（容器本身及所有子元素）
    if not piece_bytes:
        try:
            bg_img_url = page.evaluate("""(sel) => {
                const container = document.querySelector(sel);
                if (!container) return null;
                // 检查容器本身和所有后代元素
                const candidates = [container, ...container.querySelectorAll('*')];
                for (const el of candidates) {
                    const bg = getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none' && bg.startsWith('url(')) {
                        return {
                            url: bg.slice(4, -1).replace(/['"]/g, ''),
                            fromChild: el !== container,
                            tag: el.tagName,
                            cls: el.className,
                            w: el.offsetWidth,
                            h: el.offsetHeight,
                        };
                    }
                }
                return null;
            }""", piece_selector)
            if bg_img_url:
                url = bg_img_url['url'] if isinstance(bg_img_url, dict) else bg_img_url
                logger.info(f'拼图块 CSS background-image: from={bg_img_url.get("tag", "?")}, url={url[:80]}')
                if url.startswith('data:'):
                    piece_bytes = base64.b64decode(url.split(',', 1)[1])
                    meta['piece_source'] = 'css_bg_data_url'
                elif url.startswith('http'):
                    resp = page.request.get(url)
                    if resp.ok:
                        piece_bytes = resp.body()
                        meta['piece_source'] = 'css_bg_http'
                if piece_bytes:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(piece_bytes))
                    meta['piece_size'] = img.size
                    logger.info(f'拼图块获取成功: {img.size[0]}x{img.size[1]} via CSS background-image')
            else:
                logger.info('拼图块及其子元素均无 CSS background-image')
        except Exception as e:
            logger.warning(f'CSS background-image 获取失败: {e}')

    # 2d. 回退到截图容器（可能带空白，但总比没有好）
    if not piece_bytes:
        try:
            piece_locator = page.locator(piece_selector).first
            if piece_locator.count() > 0 and piece_locator.is_visible():
                piece_bytes = piece_locator.screenshot()
                meta['piece_source'] = 'container_screenshot'

                from PIL import Image
                import io
                img = Image.open(io.BytesIO(piece_bytes))
                meta['piece_size'] = img.size
                logger.warning(f'拼图块获取（回退）: {img.size[0]}x{img.size[1]} via container screenshot（需要 content_offset 修正）')
            else:
                vis = page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return 'not_found';
                    return { vis: getComputedStyle(el).visibility, disp: getComputedStyle(el).display, parent: el.offsetParent !== null };
                }""", piece_selector)
                logger.warning(f'拼图块容器不可见: count={piece_locator.count()}, visibility_info={vis}')
        except Exception as e:
            logger.warning(f'拼图块容器截图失败: {e}')

    # 3. 尺寸校正：确保拼图块与容器 CSS 显示尺寸一致（模板匹配的前提）
    #    例：img src 自然尺寸 47x155，但 CSS 显示为 60x200，背景缺口也是 60x200
    #    不缩放则模板匹配在错误尺度上工作，max_val 极低，位置完全错误
    if piece_bytes:
        try:
            container_css = page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                return { w: el.offsetWidth, h: el.offsetHeight };
            }""", piece_selector)
            if container_css and container_css['w'] > 0 and container_css['h'] > 0:
                from PIL import Image
                import io
                piece_img = Image.open(io.BytesIO(piece_bytes))
                natural_size = piece_img.size
                css_size = (container_css['w'], container_css['h'])
                if natural_size != css_size:
                    logger.info(f'缩放拼图块: {natural_size[0]}x{natural_size[1]} -> {css_size[0]}x{css_size[1]} (匹配容器CSS尺寸)')
                    piece_img = piece_img.resize(css_size, Image.LANCZOS)
                    buf = io.BytesIO()
                    piece_img.save(buf, 'PNG')
                    piece_bytes = buf.getvalue()
                    meta['piece_resized'] = True
                    meta['piece_original_size'] = natural_size
                    meta['piece_size'] = css_size
                else:
                    logger.info(f'拼图块尺寸匹配: {natural_size[0]}x{natural_size[1]} = CSS {css_size[0]}x{css_size[1]}')
        except Exception as e:
            logger.warning(f'拼图块尺寸校正失败: {e}')

    return bg_bytes, piece_bytes, meta


def _hide_piece_temporarily(page: Page, piece_selector: str):
    """截图背景前临时隐藏拼图块，避免干扰。"""
    try:
        page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (el) {
                el.dataset._cnipaVis = el.style.visibility;
                el.style.visibility = 'hidden';
            }
        }""", piece_selector)
    except Exception:
        pass


def restore_piece_visibility(page: Page, piece_selector: str):
    """恢复拼图块可见性。"""
    try:
        page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (el) {
                el.style.visibility = el.dataset._cnipaVis || '';
                delete el.dataset._cnipaVis;
            }
        }""", piece_selector)
    except Exception:
        pass


def get_yidun_images(page: Page) -> Tuple[Optional[bytes], Optional[bytes]]:
    """
    获取易盾验证码的图片（兼容旧接口）。

    易盾验证码的 class 名以 yidun_ 开头。
    """
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

    bg_bytes = None
    piece_bytes = None

    # 背景图
    for sel in bg_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                src = el.get_attribute('src') or ''
                if src.startswith('data:'):
                    bg_bytes = base64.b64decode(src.split(',', 1)[1])
                elif src.startswith('http'):
                    resp = page.request.get(src)
                    if resp.ok:
                        bg_bytes = resp.body()
                if bg_bytes:
                    break
        except Exception:
            continue

    # 拼图块
    for sel in piece_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                src = el.get_attribute('src') or ''
                if src.startswith('data:'):
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


def _diagnose_piece_dom(page: Page, piece_selector: str) -> dict:
    """诊断拼图块容器的 DOM 结构，确定图片渲染方式。

    检查 .verify-sub-block 内部是否有 img/canvas/CSS background-image，
    以及子元素的层级关系。
    """
    try:
        return page.evaluate("""(sel) => {
            const container = document.querySelector(sel);
            if (!container) return { error: 'container not found' };

            const info = {
                tag: container.tagName,
                cls: container.className,
                w: container.offsetWidth,
                h: container.offsetHeight,
                visible: container.offsetParent !== null,
                visibility: getComputedStyle(container).visibility,
                display: getComputedStyle(container).display,
                bgImage: (getComputedStyle(container).backgroundImage || 'none').substring(0, 120),
                childCount: container.children.length,
                children: [],
                hasImg: container.querySelector('img') !== null,
                hasCanvas: container.querySelector('canvas') !== null,
            };

            // 检查所有子元素（递归 2 层）
            function describe(el, depth) {
                if (depth > 2) return null;
                const cs = getComputedStyle(el);
                return {
                    tag: el.tagName,
                    cls: el.className ? el.className.substring(0, 60) : '',
                    w: el.offsetWidth,
                    h: el.offsetHeight,
                    bgImage: (cs.backgroundImage !== 'none') ? cs.backgroundImage.substring(0, 100) : null,
                    src: el.src ? el.src.substring(0, 100) : null,
                    children: depth < 2 ? Array.from(el.children).map(c => describe(c, depth + 1)).filter(Boolean) : [],
                };
            }
            info.children = Array.from(container.children).map(c => describe(c, 0)).filter(Boolean);

            // 检查所有 img（包括深层嵌套）
            const allImgs = container.querySelectorAll('img');
            info.allImgCount = allImgs.length;
            info.allImgSrcs = Array.from(allImgs).map(img => (img.src || '').substring(0, 100));

            // 检查所有 canvas
            const allCanvas = container.querySelectorAll('canvas');
            info.allCanvasCount = allCanvas.length;

            return info;
        }""", piece_selector)
    except Exception as e:
        return {'error': str(e)}
