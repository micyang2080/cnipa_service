#!/usr/bin/env python3
import json, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from cnipa_common import TYSF_LOGIN_URL, _click_sso_submit_button, load_credentials, launch_browser_session, close_browser_session
from playwright.sync_api import sync_playwright

creds = load_credentials({'credentials_path': r'C:\git\Skills\patent-reexam-invalid-report\secrets\cnipa_credentials.json'})
with sync_playwright() as pw:
    bs = launch_browser_session(pw, headless=False, storage_state=None, session_dir=os.path.join(ROOT,'output','cnipa_session'))
    page = bs.context.new_page()
    page.goto(TYSF_LOGIN_URL, wait_until='networkidle', timeout=120000)
    page.locator('input[placeholder*="手机"]').first.fill(creds['username'])
    page.locator('input[type="password"]').first.fill(creds['password'])
    _click_sso_submit_button(page)
    page.wait_for_selector('.verify-move-block', timeout=15000)
    info = page.evaluate('''() => {
      const sub = document.querySelector('.verify-sub-block');
      const panel = document.querySelector('.verify-img-panel');
      const imgs = [...document.querySelectorAll('.captcha img')].map(i => ({src: (i.src||'').slice(0,80), w: i.width, h: i.height}));
      return {
        subStyle: sub ? sub.getAttribute('style') : null,
        subHtml: sub ? sub.outerHTML.slice(0,500) : null,
        panelStyle: panel ? panel.getAttribute('style') : null,
        imgs,
      };
    }''')
    print(json.dumps(info, ensure_ascii=False, indent=2))
    close_browser_session(bs)
