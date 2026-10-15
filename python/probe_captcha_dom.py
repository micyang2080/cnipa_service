#!/usr/bin/env python3
import os, sys, json
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from cnipa_common import TYSF_LOGIN_URL, _click_sso_submit_button, load_credentials, launch_browser_session
from playwright.sync_api import sync_playwright

def main():
    creds = load_credentials({'credentials_path': r'C:\git\Skills\patent-reexam-invalid-report\secrets\cnipa_credentials.json'})
    with sync_playwright() as pw:
        bs = launch_browser_session(pw, headless=False, storage_state=None, session_dir=os.path.join(ROOT,'output','cnipa_session'))
        page = bs.context.new_page()
        page.goto(TYSF_LOGIN_URL, wait_until='networkidle', timeout=120000)
        page.wait_for_selector('input[placeholder*="手机"]', timeout=30000)
        page.locator('input[placeholder*="手机"]').first.fill(creds['username'])
        page.locator('input[type="password"]').first.fill(creds['password'])
        _click_sso_submit_button(page)
        page.wait_for_timeout(2500)
        html = page.locator('[class*="captcha"]').first.inner_html()[:3000]
        outer = page.evaluate('''() => {
          const el = document.querySelector('[class*="captcha"]');
          if (!el) return null;
          const walk = (node, depth=0) => {
            if (depth > 4) return null;
            const cls = node.className && typeof node.className === 'string' ? node.className : '';
            const tag = node.tagName;
            const children = [...node.children].slice(0,8).map(c => walk(c, depth+1)).filter(Boolean);
            return {tag, cls, children: children.length ? children : undefined};
          };
          return walk(el);
        }''')
        print(json.dumps({'html_snip': html, 'tree': outer}, ensure_ascii=False, indent=2))
        from cnipa_common import close_browser_session
        close_browser_session(bs)

if __name__ == '__main__':
    main()
