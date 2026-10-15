#!/usr/bin/env python3
"""国知局 pss 摘要附图检索与下载。

联调版（2026-08-31）：
- StepDumper 落盘每步截图/HTML/元素清单（pss 是独立域名，可能有自己的瑞数挑战）
- _ensure_page_rendered：pss 首跳若是 412 JS 壳则等待重载出真实内容
- img_candidates：页面全部图片 URL 候选清单写进 payload，
  附图定位失败时离线即可修选择器，不必反复试探运行
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cnipa_common import (  # noqa: E402
    PSS_SEARCH,
    StepDumper,
    _click_by_text,
    _ensure_page_rendered,
    _ensure_stdio_line_buffered,
    _log,
    _parse_json_input,
    _should_launch_headless,
    close_browser_session,
    emit_result,
    ensure_logged_in,
    get_work_page,
    handle_pss_disclaimer,
    launch_browser_session,
    load_credentials,
    normalize_application_no,
    resolve_session_dir,
    save_session_state,
    session_state_path,
    set_step_dumper,
)
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    _ensure_stdio_line_buffered()
    args = _parse_json_input()
    dumper = StepDumper('figure')
    set_step_dumper(dumper)
    page = None
    try:
        application_no = str(args.get('application_no') or '').strip()
        if not application_no:
            raise ValueError('application_no 必填')

        search_key = normalize_application_no(application_no)
        creds = load_credentials(args)
        session_dir = resolve_session_dir(args)
        session_file = session_state_path(session_dir, creds['username'])
        save_path = str(args.get('save_path') or '').strip()
        headless = _should_launch_headless(args)

        with sync_playwright() as pw:
            bs = launch_browser_session(
                pw,
                headless=headless,
                storage_state=session_file if os.path.isfile(session_file) else None,
                session_dir=session_dir,
            )
            page = get_work_page(bs.context)
            try:
                ensure_logged_in(
                    page,
                    creds,
                    session_file,
                    session_dir=session_dir,
                    allow_sso_login=False,
                    browser_mode=bs.mode,
                )
                page.goto(PSS_SEARCH, wait_until='domcontentloaded', timeout=120_000)
                # pss 首跳可能也是瑞数 412 JS 壳：等真实内容渲染（v10 原生指纹应能过）
                rendered = _ensure_page_rendered(page, min_len=200, max_wait_ms=15_000)
                _log(f'[pss] 页面渲染{"完成" if rendered else "超时（可能是挑战页，看 dump）"}')
                dumper.dump(page, '01_pss_loaded')

                handle_pss_disclaimer(page)

                if not _select_application_no_tab(page):
                    _log('[warn] 未能明确切换到申请号 Tab')
                dumper.dump(page, '02_tab_selected')

                filled = _fill_search_input(page, search_key)
                if not filled:
                    dumper.dump(page, 'FAIL_search_input')
                    raise RuntimeError('无法在 pss 页定位申请号输入框')

                if not _click_by_text(page, ['检索', '查询', '搜索'], exact=False):
                    page.keyboard.press('Enter')
                page.wait_for_timeout(3000)

                candidates = _list_img_candidates(page)
                figure_url = _extract_figure_url(page)
                dumper.dump(page, '03_searched' if figure_url else 'FAIL_no_figure')
                if not figure_url:
                    raise RuntimeError(
                        f'未找到摘要附图: {search_key}（候选 {len(candidates)} 张已记入 img_candidates，离线分析 03 步 dump）'
                    )

                figure_path = ''
                if save_path:
                    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                    resp = page.request.get(figure_url)
                    if not resp.ok:
                        raise RuntimeError(f'下载附图失败 HTTP {resp.status}')
                    with open(save_path, 'wb') as f:
                        f.write(resp.body())
                    figure_path = os.path.abspath(save_path)

                payload = {
                    'success': True,
                    'summary': f'摘要附图: {search_key}',
                    'application_no': application_no,
                    'search_key': search_key,
                    'figure_url': figure_url,
                    'figure_path': figure_path,
                    'img_candidates': candidates[:20],
                    'debug_dir': dumper.dir,
                    'session_file': session_file,
                }
                save_session_state(bs.context, session_file, bs)
                emit_result(payload)
            finally:
                close_browser_session(bs)
    except Exception as exc:
        _log(f'[cnipa_figure] error: {exc}')
        dumper.dump_fail(page, 'figure_main', exc)
        emit_result(
            {
                'success': False,
                'error': str(exc),
                'application_no': args.get('application_no'),
                'debug_dir': dumper.dir,
            }
        )


def _select_application_no_tab(page) -> bool:
    # 先试 radio/tab 形态（常见），再退化为纯文本
    for sel in (
        '[role="tab"]:has-text("申请号")',
        'label:has-text("申请号")',
        '[class*="radio"]:has-text("申请号")',
    ):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=800):
                el.click(timeout=3000)
                page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    return _click_by_text(page, ['申请号'], exact=True) or _click_by_text(page, ['申请号'], exact=False)


def _fill_search_input(page, value: str) -> bool:
    selectors = (
        'input[name*="sqh"]',
        'input[placeholder*="申请"]',
        'input[type="text"]',
    )
    for selector in selectors:
        try:
            inputs = page.locator(selector)
            for i in range(inputs.count()):
                el = inputs.nth(i)
                if el.is_visible():
                    el.click()
                    el.fill(value)
                    return True
        except Exception:
            continue
    return False


def _list_img_candidates(page) -> list:
    """页面全部非 data: 图片 URL（含 logo/icon，离线排除用）。"""
    out: list = []
    base = page.url
    try:
        imgs = page.locator('img')
        for i in range(min(imgs.count(), 60)):
            try:
                src = imgs.nth(i).get_attribute('src') or ''
                if src and not src.startswith('data:'):
                    out.append(urljoin(base, src))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _extract_figure_url(page) -> str:
    """摘要附图：优先结果表格/列表内的图，跳过 logo/icon/banner 类。"""
    selectors = (
        'table img',
        '.result img',
        '[class*="result"] img',
        '[class*="list"] img',
        'img[src*="abstract"]',
        'img[src*="thumb"]',
        'img[src*="figure"]',
        'img[src*="image"]',
        'img',
    )
    base = page.url
    for selector in selectors:
        try:
            imgs = page.locator(selector)
            for i in range(min(imgs.count(), 20)):
                src = imgs.nth(i).get_attribute('src') or ''
                if not src or src.startswith('data:'):
                    continue
                low = src.lower()
                if any(x in low for x in ('logo', 'icon', 'banner', '.svg', 'default')):
                    continue
                return urljoin(base, src)
        except Exception:
            continue
    return ''


if __name__ == '__main__':
    main()
