"""
CNIPA 验证码 DOM 诊断脚本。

用于搞清楚 .verify-sub-block 内部的 DOM 结构，确定如何获取干净的拼图块图片。

运行方式：
    在 Playwright 浏览器控制台中执行此脚本的 JavaScript 部分，
    或者在 Python 中调用 diagnose_captcha_dom() 函数。
"""

from playwright.sync_api import Page
import json


def diagnose_captcha_dom(page: Page) -> dict:
    """诊断 CNIPA 验证码的 DOM 结构，返回详细信息。

    检查内容：
    1. .verify-sub-block 内部是否有 <img> 元素
    2. 是否有 <canvas> 元素
    3. 是否有 CSS background-image
    4. 元素的尺寸、位置、样式等

    返回：
        dict: 包含所有诊断信息的字典
    """
    result = page.evaluate("""() => {
        const container = document.querySelector('.verify-sub-block');
        if (!container) {
            return { error: '未找到 .verify-sub-block 容器' };
        }

        const info = {
            container: {
                tagName: container.tagName,
                className: container.className,
                width: container.offsetWidth,
                height: container.offsetHeight,
                style: container.getAttribute('style') || '',
                computedStyle: {
                    width: getComputedStyle(container).width,
                    height: getComputedStyle(container).height,
                    backgroundImage: getComputedStyle(container).backgroundImage,
                    padding: getComputedStyle(container).padding,
                },
            },
            images: [],
            canvases: [],
            children: [],
        };

        // 检查所有 <img> 子元素
        const imgs = container.querySelectorAll('img');
        imgs.forEach((img, idx) => {
            info.images.push({
                index: idx,
                src: img.src ? img.src.substring(0, 100) : '',  // 截断长 URL
                width: img.offsetWidth,
                height: img.offsetHeight,
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight,
                style: img.getAttribute('style') || '',
                className: img.className,
            });
        });

        // 检查所有 <canvas> 子元素
        const canvases = container.querySelectorAll('canvas');
        canvases.forEach((canvas, idx) => {
            info.canvases.push({
                index: idx,
                width: canvas.offsetWidth,
                height: canvas.offsetHeight,
                // canvas 的实际内容需要通过 toDataURL() 获取
                hasContent: canvas.width > 0 && canvas.height > 0,
                canvasWidth: canvas.width,
                canvasHeight: canvas.height,
            });
        });

        // 检查所有直接子元素
        const children = container.children;
        for (let i = 0; i < children.length; i++) {
            const child = children[i];
            info.children.push({
                index: i,
                tagName: child.tagName,
                className: child.className,
                width: child.offsetWidth,
                height: child.offsetHeight,
                style: child.getAttribute('style') || '',
            });
        }

        // 检查 CSS background-image
        const bgImage = getComputedStyle(container).backgroundImage;
        info.hasBackgroundImage = bgImage && bgImage !== 'none';
        info.backgroundImage = bgImage;

        return info;
    }""")

    return result


def print_diagnostic_report(page: Page):
    """打印格式化的诊断报告。"""
    info = diagnose_captcha_dom(page)

    print("=" * 80)
    print("CNIPA 验证码 DOM 诊断报告")
    print("=" * 80)

    if 'error' in info:
        print(f"错误: {info['error']}")
        return

    print("\n【容器 .verify-sub-block】")
    c = info['container']
    print(f"  尺寸: {c['width']}x{c['height']} px")
    print(f"  计算尺寸: {c['computedStyle']['width']} x {c['computedStyle']['height']}")
    print(f"  Padding: {c['computedStyle']['padding']}")
    print(f"  Background: {c['computedStyle']['backgroundImage'][:80]}...")

    print(f"\n【<img> 元素】共 {len(info['images'])} 个")
    for img in info['images']:
        print(f"  [{img['index']}] {img['width']}x{img['height']} (natural: {img['naturalWidth']}x{img['naturalHeight']})")
        print(f"      src: {img['src']}...")
        print(f"      class: {img['className']}")

    print(f"\n【<canvas> 元素】共 {len(info['canvases'])} 个")
    for canvas in info['canvases']:
        print(f"  [{canvas['index']}] {canvas['width']}x{canvas['height']} (canvas size: {canvas['canvasWidth']}x{canvas['canvasHeight']})")
        print(f"      hasContent: {canvas['hasContent']}")

    print(f"\n【直接子元素】共 {len(info['children'])} 个")
    for child in info['children']:
        print(f"  [{child['index']}] <{child['tagName']}> {child['width']}x{child['height']} class='{child['className']}'")

    print(f"\n【结论】")
    if info['images']:
        print("  ✓ 找到 <img> 元素，可以直接截图 img 元素获取干净拼图块")
        print(f"    推荐选择器: .verify-sub-block img")
    elif info['canvases']:
        print("  ✓ 找到 <canvas> 元素，可以截图 canvas 或用 toDataURL() 导出")
        print(f"    推荐选择器: .verify-sub-block canvas")
    elif info['hasBackgroundImage']:
        print("  ✗ 使用 CSS background-image，需要特殊处理")
        print(f"    需要: 提取 background-image URL 并下载")
    else:
        print("  ✗ 未找到图片元素，可能是动态渲染或其他机制")

    print("=" * 80)


if __name__ == '__main__':
    #  standalone 测试用
    print("此脚本需要在 Playwright 浏览器环境中运行。")
    print("请在 Python 代码中调用 diagnose_captcha_dom(page) 或 print_diagnostic_report(page)")
