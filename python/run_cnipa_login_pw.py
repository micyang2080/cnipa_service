#!/usr/bin/env python3
"""国知局 SSO 登录并持久化 session。"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cnipa_common import (  # noqa: E402
    CPQUERY_INDEX,
    _can_attempt_sso_login,
    _ensure_stdio_line_buffered,
    _is_cpquery_session_valid,
    _log,
    _mark_sso_attempt,
    _parse_json_input,
    _raise_if_unsafe,
    _should_launch_headless,
    close_browser_session,
    cnipa_sso_login,
    emit_result,
    get_work_page,
    launch_browser_session,
    load_credentials,
    resolve_cdp_url,
    resolve_session_dir,
    save_session_state,
    session_state_path,
)
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    _ensure_stdio_line_buffered()
    args = _parse_json_input()
    try:
        creds = load_credentials(args)
        session_dir = resolve_session_dir(args)
        session_file = session_state_path(session_dir, creds['username'])
        force_login = bool(args.get('force_login'))
        headless = _should_launch_headless(args)

        with sync_playwright() as pw:
            # force_login 不用旧 cookie/半残 session，避免 cpquery 白屏且不跳登录页
            use_state = (
                None
                if force_login
                else (session_file if os.path.isfile(session_file) else None)
            )
            if force_login:
                os.environ.setdefault('CNIPA_PERSISTENT_PROFILE', '0')
            bs = launch_browser_session(
                pw,
                headless=headless,
                storage_state=use_state,
                session_dir=session_dir,
            )
            page = get_work_page(bs.context)
            try:
                if bs.mode == 'cdp':
                    if _is_cpquery_session_valid(page):
                        emit_result(
                            {
                                'success': True,
                                'session_valid': True,
                                'summary': '已接管 Chrome，cpquery 处于已登录状态，无需 SSO',
                                'session_file': session_file,
                                'login_type': creds['login_type'],
                                'browser_mode': 'cdp',
                            }
                        )
                        return
                    raise RuntimeError(
                        '已连接 Chrome，但 cpquery 未登录。请在该浏览器窗口手动打开 cpquery 登录，'
                        '不要调用 cnipa_login 反复打 SSO。'
                    )

                if not force_login:
                    page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=120_000)
                    page.wait_for_timeout(1500)
                    if _is_cpquery_session_valid(page):
                        save_session_state(bs.context, session_file, bs)
                        emit_result(
                            {
                                'success': True,
                                'session_valid': True,
                                'summary': '已有有效 session，跳过 SSO 登录',
                                'session_file': session_file,
                                'login_type': creds['login_type'],
                                'browser_mode': bs.mode,
                            }
                        )
                        return

                ok, remaining = _can_attempt_sso_login(session_dir)
                # force_login 或 CNIPA_SSO_COOLDOWN_SEC=0 时跳过冷却（便于联调）
                if not ok and not force_login:
                    raise RuntimeError(
                        f'距上次 SSO 登录不足冷却时间（还需约 {remaining} 秒）。'
                        '请勿频繁访问国知局；可设置 CNIPA_CDP_URL 接管已登录 Chrome。'
                        '测试时可设 CNIPA_SSO_COOLDOWN_SEC=0 或 force_login=true。'
                    )

                _mark_sso_attempt(session_dir)
                max_slider = int(args.get('max_slider_attempts') or 6)
                cnipa_sso_login(page, creds, max_slider_attempts=max_slider)
                save_session_state(bs.context, session_file, bs)
                _raise_if_unsafe(page)
                emit_result(
                    {
                        'success': True,
                        'session_valid': True,
                        'summary': '国知局 SSO 登录成功，cpquery 可访问',
                        'session_file': session_file,
                        'login_type': creds['login_type'],
                        'final_url': page.url,
                        'browser_mode': bs.mode,
                    }
                )
            finally:
                close_browser_session(bs)
    except Exception as exc:
        _log(f'[cnipa_login] error: {exc}')
        hint = ''
        if not resolve_cdp_url():
            hint = ' 提示：若你已在 Chrome 手动登录 cpquery，可设 CNIPA_CDP_URL=http://127.0.0.1:9222 接管该浏览器。'
        emit_result({'success': False, 'error': str(exc) + hint, 'session_valid': False})


if __name__ == '__main__':
    main()
