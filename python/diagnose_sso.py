"""诊断 SSO 登录后的 cookie 和跳转行为。

用 Playwright 拦截所有网络请求，记录：
1. SSO 登录成功后 Set-Cookie 的域名和名称
2. 访问 cpquery 时带的 Cookie
3. 302 跳转的 Location
4. cponline 页面「专利审查信息查询」链接的实际 href

运行方式：
  python diagnose_sso.py --username 手机号 --password 密码
"""
from __future__ import annotations

import argparse
import sys
import os

# 确保能导入 cnipa_common
CNIPA_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CNIPA_ROOT)
sys.path.insert(0, os.path.join(CNIPA_ROOT, 'src'))

from playwright.sync_api import sync_playwright, Page, Request, Response

SSO_URL = 'https://sso.cponline.cnipa.gov.cn'
CPQUERY_INDEX = 'https://cpquery.cponline.cnipa.gov.cn/chinesepatent/index'
TYSF_PORTAL = 'https://tysfjk.cponline.cnipa.gov.cn/portal/#/home/welcome'


def log(msg: str) -> None:
    print(f'[diag] {msg}', flush=True)


def on_request(request: Request) -> None:
    url = request.url
    # 只记录关键域名的请求
    for domain in ('sso.cponline', 'cpquery.cponline', 'tysfjk.cponline', 'cponline.cnipa'):
        if domain in url:
            cookies_header = request.headers.get('cookie', '')
            log(f'REQ  {request.method} {url[:120]}')
            if cookies_header:
                # 只显示 cookie 名称，不显示值
                cookie_names = [c.split('=')[0].strip() for c in cookies_header.split(';') if '=' in c]
                log(f'     Cookie names: {cookie_names}')
            break


def on_response(response: Response) -> None:
    url = response.url
    status = response.status
    for domain in ('sso.cponline', 'cpquery.cponline', 'tysfjk.cponline', 'cponline.cnipa'):
        if domain in url:
            log(f'RESP {status} {url[:120]}')
            # 检查 Set-Cookie
            set_cookies = response.headers.get('set-cookie', '')
            if set_cookies:
                for sc in set_cookies.split('\n'):
                    sc = sc.strip()
                    if sc:
                        name = sc.split('=')[0] if '=' in sc else sc[:30]
                        # 提取 domain 和 path
                        parts = sc.lower().split(';')
                        domain_part = ''
                        path_part = ''
                        for p in parts:
                            p = p.strip()
                            if p.startswith('domain='):
                                domain_part = p[7:]
                            elif p.startswith('path='):
                                path_part = p[5:]
                        log(f'     Set-Cookie: {name}  domain={domain_part}  path={path_part}')

            # 检查 302 Location
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get('location', '')
                log(f'     → Location: {location}')
            break


def diagnose(username: str, password: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        )
        page = context.new_page()

        # 拦截网络请求
        page.on('request', on_request)
        page.on('response', on_response)

        # 禁用 debugger 暂停（绕过反调试）+ 隐藏自动化特征
        page.add_init_script("""
            // 覆盖 debugger 行为
            Object.defineProperty(window, 'debugger', {
                get: function() { return function() {}; }
            });
            // 覆盖 console.debug 中的 debugger
            const origEval = window.eval;
            window.eval = function(code) {
                if (typeof code === 'string' && code.includes('debugger')) {
                    return;
                }
                return origEval.apply(this, arguments);
            };
            // 隐藏 navigator.webdriver（关键！SSO 可能检测这个）
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
            // 覆盖 chrome.runtime（Playwright 缺失这个）
            window.chrome = window.chrome || {};
            window.chrome.runtime = window.chrome.runtime || {};
        """)

        log('===== 步骤1: 访问 cpquery =====')
        # 先检查浏览器指纹
        fingerprint = page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            languages: navigator.languages,
            hasChrome: !!window.chrome,
            hasChromeRuntime: !!(window.chrome && window.chrome.runtime),
        })""")
        log(f'浏览器指纹: {fingerprint}')

        page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(3000)
        log(f'当前 URL: {page.url}')

        log('===== 步骤2: 访问 SSO 登录页 =====')
        from urllib.parse import quote
        sso_url = f'{SSO_URL}/login?service={quote(CPQUERY_INDEX, safe="")}'
        page.goto(sso_url, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(2000)
        log(f'当前 URL: {page.url}')

        # 检查登录表单
        has_form = page.locator('input[placeholder*="手机"], input[type="password"]').count() > 0
        log(f'登录表单可见: {has_form}')

        if not has_form:
            log('SSO 页面无登录表单，尝试其他方式...')
            # 截图
            page.screenshot(path=os.path.join(CNIPA_ROOT, 'output', 'diag_sso_no_form.png'), full_page=True)
            log('已截图: output/diag_sso_no_form.png')
            browser.close()
            return

        log('===== 步骤3: 填写登录信息 =====')
        user_input = page.locator('input[placeholder*="手机"]').first
        pass_input = page.locator('input[type="password"]').first
        user_input.click()
        # 用 keyboard.type() 避免 fill() 对特殊字符（如 $）的处理问题
        page.keyboard.type(username, delay=50)
        pass_input.click()
        page.keyboard.type(password, delay=50)
        page.wait_for_timeout(500)

        log('===== 步骤4: 点击登录按钮 =====')
        # 找登录按钮
        for sel in ('button:has-text("登录")', 'button.login-btn', '.btn-login', 'input[type="submit"]'):
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click()
                log(f'点击了登录按钮: {sel}')
                break
        else:
            log('未找到登录按钮')
            page.screenshot(path=os.path.join(CNIPA_ROOT, 'output', 'diag_no_login_btn.png'), full_page=True)
            browser.close()
            return

        page.wait_for_timeout(2000)

        # 检查是否有滑块验证码
        has_slider = page.locator('.verify-img-panel, .slide_verify, .nc-container').count() > 0
        if has_slider:
            log('检测到滑块验证码，需要手动完成')
            log('请在浏览器中手动完成滑块验证，然后按 Enter 继续...')
            input()
        else:
            log('未检测到滑块验证码')

        log('===== 步骤5: 等待登录后跳转 =====')
        page.wait_for_timeout(5000)
        log(f'登录后 URL: {page.url}')

        # 记录所有 cookie
        log('===== 步骤6: 记录所有 cookie =====')
        cookies = context.cookies()
        log(f'Cookie 总数: {len(cookies)}')
        for c in cookies:
            log(f'Cookie: name={c["name"]}  domain={c["domain"]}  path={c["path"]}  secure={c.get("secure", False)}  httpOnly={c.get("httpOnly", False)}  value_len={len(c.get("value", ""))}')

        # 关键测试：现在直接访问 cpquery，看会发生什么
        log('===== 步骤6b: 测试直接访问 cpquery =====')
        page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=15000)
        page.wait_for_timeout(3000)
        log(f'直接访问 cpquery 后 URL: {page.url}')
        # 检查是否被重定向
        if 'tysfjk' in page.url or '/portal/' in page.url:
            log('  → 被重定向到门户！')
        elif 'cpquery' in page.url:
            log('  → 成功到达 cpquery！')
            # 检查页面内容
            body = page.inner_text('body')[:200]
            log(f'  → 页面内容预览: {body}')

        # 截图
        page.screenshot(path=os.path.join(CNIPA_ROOT, 'output', 'diag_after_login.png'), full_page=True)
        log('已截图: output/diag_after_login.png')

        # 如果落在门户，尝试「返回系统」
        if 'tysfjk' in page.url or '/portal/' in page.url:
            log('===== 步骤7: 落在门户，尝试「返回系统」=====')
            for sel in ('a:has-text("返回系统")', 'text=返回系统'):
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=3000):
                    el.click()
                    log('已点「返回系统」')
                    break
            page.wait_for_timeout(3000)
            log(f'「返回系统」后 URL: {page.url}')

            # 再记录 cookie
            cookies2 = context.cookies()
            log(f'Cookie 数量: {len(cookies2)}')
            for c in cookies2:
                log(f'Cookie: name={c["name"]}  domain={c["domain"]}  path={c["path"]}')

            # 如果在 cponline 首页，找「专利审查信息查询」链接
            if 'cponline.cnipa.gov.cn' in page.url and 'cpquery' not in page.url and 'tysf' not in page.url:
                log('===== 步骤8: 在 cponline 首页，查找导航链接 =====')
                for sel in ('a:has-text("专利审查信息查询")', 'a:has-text("审查信息查询")'):
                    el = page.locator(sel).first
                    if el.count() > 0:
                        href = el.get_attribute('href')
                        log(f'导航链接 href: {href}')
                        # 也检查 onclick
                        onclick = el.get_attribute('onclick')
                        if onclick:
                            log(f'导航链接 onclick: {onclick}')
                        break

        log('===== 诊断完成 =====')
        log('浏览器保持打开，你可以手动操作查看。按 Enter 关闭...')
        input()
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True, help='手机号')
    parser.add_argument('--password', required=True, help='密码')
    args = parser.parse_args()
    os.makedirs(os.path.join(CNIPA_ROOT, 'output'), exist_ok=True)
    diagnose(args.username, args.password)
