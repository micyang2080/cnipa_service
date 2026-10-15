#!/usr/bin/env python3
"""探测：填账号 → 点登录 → 捕获滑块 DOM。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from cnipa_common import TYSF_LOGIN_URL, _click_sso_submit_button, load_credentials, launch_browser_session
from playwright.sync_api import sync_playwright

OUT = os.path.join(ROOT, 'output', 'sso_probe')


def main():
    args = {'credentials_path': os.path.join(ROOT, '..', '..', '..', 'Skills', 'patent-reexam-invalid-report', 'secrets', 'cnipa_credentials.json')}
    creds = load_credentials(args)
    with sync_playwright() as pw:
        bs = launch_browser_session(pw, headless=False, storage_state=None, session_dir=os.path.join(ROOT, 'output', 'cnipa_session'))
        page = bs.context.new_page()
        try:
            page.goto(TYSF_LOGIN_URL, wait_until='networkidle', timeout=120_000)
            page.wait_for_selector('input[placeholder*="手机"]', timeout=30_000)
            page.locator('input[placeholder*="手机"]').first.fill(creds['username'])
            page.locator('input[type="password"]').first.fill(creds['password'])
            _click_sso_submit_button(page)
            page.wait_for_timeout(3000)
            page.screenshot(path=os.path.join(OUT, '03_after_login_click.png'), full_page=True)
            info = {'url': page.url, 'sliders': []}
            for sel in ('.yidun', '[class*="yidun"]', '[class*="slider"]', '[class*="drag"]', '[class*="captcha"]'):
                c = page.locator(sel).count()
                if c:
                    info['sliders'].append({'sel': sel, 'count': c})
            with open(os.path.join(OUT, '03_after_login_click.json'), 'w', encoding='utf-8') as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            print(json.dumps(info, ensure_ascii=False, indent=2))
        finally:
            from cnipa_common import close_browser_session
            close_browser_session(bs)


if __name__ == '__main__':
    main()
