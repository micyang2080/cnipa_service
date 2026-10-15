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
    page.wait_for_selector('input[placeholder*="手机"]', timeout=30000)
    page.locator('input[placeholder*="手机"]').first.fill(creds['username'])
    page.locator('input[type="password"]').first.fill(creds['password'])
    _click_sso_submit_button(page)
    page.wait_for_selector('.captcha', timeout=15000)
    data = page.evaluate('''() => {
      const out = {classes: [], boxes: []};
      document.querySelectorAll('.captcha *').forEach(e => {
        if (e.className && typeof e.className === 'string' && e.className.trim())
          out.classes.push(e.className.trim());
      });
      out.classes = [...new Set(out.classes)];
      for (const sel of ['.verify-move-block','.verify-sub-block','.verify-bar-area','.verify-left-bar','.verify-msg','[class*=verify]']) {
        const el = document.querySelector('.captcha ' + sel) || document.querySelector(sel);
        if (el) {
          const r = el.getBoundingClientRect();
          out.boxes.push({sel, x:r.x, y:r.y, w:r.width, h:r.height, text: el.innerText?.slice(0,30)});
        }
      }
      return out;
    }''')
    print(json.dumps(data, ensure_ascii=False, indent=2))
    close_browser_session(bs)
