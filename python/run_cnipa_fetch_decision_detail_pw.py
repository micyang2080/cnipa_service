#!/usr/bin/env python3
"""国知局 cpquery 单条决定详情。

联调版（2026-08-31）：
- StepDumper 落盘截图/HTML/元素清单（raw_fields 拿不全时离线补映射）
- missing_fields 对照报告 12 列逐项核对缺失情况
- _ensure_page_rendered 防 SPA/挑战壳未渲染完就解析
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cnipa_common import (  # noqa: E402
    StepDumper,
    _ensure_page_rendered,
    _ensure_stdio_line_buffered,
    _log,
    _parse_json_input,
    _should_launch_headless,
    build_detail_url,
    close_browser_session,
    dismiss_cpquery_usage_dialog,
    emit_result,
    ensure_logged_in,
    get_work_page,
    launch_browser_session,
    load_credentials,
    map_detail_fields,
    parse_label_value_pairs,
    resolve_session_dir,
    save_json_output,
    save_session_state,
    session_state_path,
    set_step_dumper,
)
from playwright.sync_api import sync_playwright  # noqa: E402

# 报告摘要表 12 列中来自详情页的 11 列（附图列走 pss，不在此列）+ 决定号
DETAIL_REPORT_KEYS = [
    'case_no',
    'decision_no',
    'decision_date',
    'invention_title',
    'petitioner',
    'patentee',
    'application_no',
    'request_date',
    'legal_basis',
    'decision_summary',
    'decision_result',
    'panel_members',
]


def main() -> None:
    _ensure_stdio_line_buffered()
    args = _parse_json_input()
    dumper = StepDumper('detail')
    set_step_dumper(dumper)
    page = None
    try:
        creds = load_credentials(args)
        session_dir = resolve_session_dir(args)
        session_file = session_state_path(session_dir, creds['username'])
        url = build_detail_url(
            detail_url=args.get('detail_url'),
            zhuanlisqh=args.get('zhuanlisqh'),
            anjianbh=args.get('anjianbh'),
            search_type=str(args.get('search_type') or '8'),
        )
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
                page.goto(url, wait_until='domcontentloaded', timeout=120_000)
                _ensure_page_rendered(page, min_len=150, max_wait_ms=10_000)
                page.wait_for_timeout(1200)
                dismiss_cpquery_usage_dialog(page)
                dumper.dump(page, '01_detail_loaded')

                pairs = parse_label_value_pairs(page)
                detail = map_detail_fields(pairs)
                detail['detail_url'] = url
                detail['raw_fields'] = pairs

                missing = [k for k in DETAIL_REPORT_KEYS if not (detail.get(k) or '').strip()]
                if missing:
                    _log(f'[detail] 报告列缺失 {len(missing)} 项: {missing}（凭 01_detail_loaded 修映射）')
                dumper.dump(page, '02_detail_parsed')

                payload = {
                    'success': True,
                    'summary': f'详情已抓取: {detail.get("case_no") or detail.get("application_no") or url}'
                    + (f'；缺 {len(missing)} 列' if missing else '；12 列齐'),
                    'detail': detail,
                    'detail_url': url,
                    'missing_fields': missing,
                    'debug_dir': dumper.dir,
                    'session_file': session_file,
                }
                cache_file = save_json_output(payload, args.get('output_path'))
                payload['cache_file'] = cache_file
                save_session_state(bs.context, session_file, bs)
                emit_result(payload)
            finally:
                close_browser_session(bs)
    except Exception as exc:
        _log(f'[cnipa_detail] error: {exc}')
        dumper.dump_fail(page, 'detail_main', exc)
        emit_result({'success': False, 'error': str(exc), 'debug_dir': dumper.dir})


if __name__ == '__main__':
    main()
