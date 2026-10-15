#!/usr/bin/env python3
"""一次性探测 SSO 登录页 DOM（不提交账号）。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from cnipa_common import CPQUERY_INDEX, SSO_URL, launch_browser_session
from playwright.sync_api import sync_playwright

OUT = os.path.join(ROOT, 'output', 'sso_probe')


def probe(page, label: str):
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f'{label}.png'), full_page=True)
    info = {
        'label': label,
        'url': page.url,
        'title': page.title(),
        'buttons': [],
        'inputs': [],
        'tabs': [],
        'slider_candidates': [],
    }
    for i in range(page.locator('button').count()):
        b = page.locator('button').nth(i)
        try:
            if b.is_visible():
                info['buttons'].append({'i': i, 'text': (b.inner_text() or '').strip(), 'class': b.get_attribute('class') or ''})
        except Exception:
            pass
    for i in range(page.locator('input').count()):
        inp = page.locator('input').nth(i)
        try:
            if inp.is_visible():
                info['inputs'].append({
                    'i': i,
                    'type': inp.get_attribute('type') or '',
                    'placeholder': inp.get_attribute('placeholder') or '',
                    'name': inp.get_attribute('name') or '',
                })
        except Exception:
            pass
    for sel in ('[role="tab"]', '.el-tabs__item', '.tab-item'):
        loc = page.locator(sel)
        for i in range(min(loc.count(), 10)):
            t = loc.nth(i)
            try:
                if t.is_visible():
                    info['tabs'].append({'sel': sel, 'i': i, 'text': (t.inner_text() or '').strip()})
            except Exception:
                pass
    for sel in ('.yidun_slider__button', '[class*="slider"]', '[class*="drag"]', '[class*="yidun"]'):
        loc = page.locator(sel)
        c = loc.count()
        if c:
            info['slider_candidates'].append({'sel': sel, 'count': c})
    with open(os.path.join(OUT, f'{label}.json'), 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(json.dumps(info, ensure_ascii=False, indent=2))


def main():
    with sync_playwright() as pw:
        bs = launch_browser_session(pw, headless=False, storage_state=None, session_dir=os.path.join(ROOT, 'output', 'cnipa_session'))
        page = bs.context.new_page()
        try:
            page.goto(CPQUERY_INDEX, wait_until='networkidle', timeout=120_000)
            page.wait_for_timeout(3000)
            probe(page, '01_cpquery_redirect')
            page.goto(SSO_URL, wait_until='networkidle', timeout=120_000)
            page.wait_for_timeout(3000)
            probe(page, '02_sso_direct')
        finally:
            from cnipa_common import close_browser_session
            close_browser_session(bs)
    print('probe done ->', OUT)


if __name__ == '__main__':
    main()
