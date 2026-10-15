#!/usr/bin/env python3
"""国知局 cpquery 复审/无效决定列表检索。

联调版（2026-08-31）：
- StepDumper 每步落盘截图/HTML/元素清单（output/debug_steps/）
- filter_report 逐项记录筛选字段是否填写成功及命中方式
- wait_search_results 区分 found/empty/timeout，不再盲等 2 秒
- 结果 payload 附 table_headers / total_text / results_status / debug_dir
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cnipa_common import (  # noqa: E402
    StepDumper,
    _ensure_stdio_line_buffered,
    _log,
    _parse_json_input,
    _should_launch_headless,
    close_browser_session,
    dismiss_cpquery_usage_dialog,
    emit_result,
    ensure_logged_in,
    expand_more_search_items,
    extract_total_text,
    fill_search_filters,
    get_work_page,
    goto_cpquery_search_page,
    launch_browser_session,
    load_credentials,
    parse_list_table,
    resolve_session_dir,
    save_json_output,
    save_session_state,
    select_library_type,
    session_state_path,
    set_step_dumper,
    visible_table_headers,
    wait_search_results,
    _search_form_present,
)
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    _ensure_stdio_line_buffered()
    args = _parse_json_input()
    dumper = StepDumper('search')
    set_step_dumper(dumper)
    try:
        creds = load_credentials(args)
        session_dir = resolve_session_dir(args)
        session_file = session_state_path(session_dir, creds['username'])
        library_type = str(args.get('library_type') or 'invalid').strip().lower()
        if library_type not in ('invalid', 'reexam'):
            library_type = 'invalid'
        filters = args.get('filters') or {}
        if not isinstance(filters, dict):
            filters = {}
        max_items = int(args.get('max_items') or 50)
        headless = _should_launch_headless(args)
        manual_click = bool(args.get('manual_click'))

        with sync_playwright() as pw:
            bs = launch_browser_session(
                pw,
                headless=headless,
                storage_state=session_file if os.path.isfile(session_file) else None,
                session_dir=session_dir,
            )
            page = get_work_page(bs.context)

            # 全程 /api/ 响应采集：判别“只有 search 被拒”还是“整个会话的 api
            # 全被拒”（2026-09-01 实测 search 返回 HTTP 400 空体，页面静默停在
            # 默认共0条——需要知道其它 api 是否 200）。
            api_calls: list = []

            def _on_api_response(r: Any) -> None:
                try:
                    if '/api/' in r.url:
                        api_calls.append(
                            {'url': r.url.split('?')[0][-90:], 'status': int(r.status)}
                        )
                except Exception:
                    pass

            page.on('response', _on_api_response)
            try:
                ensure_logged_in(
                    page,
                    creds,
                    session_file,
                    force_login=bool(args.get('force_login')),
                    session_dir=session_dir,
                    allow_sso_login=False,
                    browser_mode=bs.mode,
                )
                dumper.dump(page, '01_after_login')

                goto_cpquery_search_page(page)
                dumper.dump(page, '02_on_search_page')

                # ★ 熔断：未确认进入复审无效检索页（决定号/案件编号可见）绝不查询。
                # 2026-09-01 联调教训：探测误判会在「案件查询」tab 查错库 → 共0条。
                if not _search_form_present(page):
                    dumper.dump(page, 'FAIL_wrong_tab_not_search_page')
                    raise RuntimeError(
                        '未进入「复审无效审查决定查询」检索页（未见 决定号/案件编号 字段），'
                        '已拒绝在错误页面提交查询；请把 debug_dir 发给 Agent 离线修选择器'
                    )

                library_verified = select_library_type(page, library_type)
                dumper.dump(page, '03_library_selected')

                expand_more_search_items(page)
                dumper.dump(page, '04_expanded_all_fields')

                filter_report = fill_search_filters(page, filters)
                dumper.dump(page, '05_filters_filled')

                # cookie 盘点（只记元数据不记值）：判别瑞数 cookie 是否缺失/异常。
                # 2026-09-01 实测：同一自动化浏览器里手工点击也 400 → 环境被标记，
                # 需要与真实浏览器（CDP 接管）的 cookie 面做离线比对。
                try:
                    cookie_inventory = [
                        {
                            'name': c.get('name'),
                            'domain': c.get('domain'),
                            'httpOnly': bool(c.get('httpOnly')),
                            'secure': bool(c.get('secure')),
                            'sameSite': str(c.get('sameSite')),
                            'expires': c.get('expires'),
                        }
                        for c in bs.context.cookies()
                        if 'cnipa.gov.cn' in (c.get('domain') or '')
                    ]
                except Exception:
                    cookie_inventory = []

                # 拟人停顿：手工从填完到点查询有约 1s 间隔（行为熵友好，
                # 也给站点 blur 规范化留时间）。
                page.wait_for_timeout(1_000)

                # ★ 网络门禁：点击查询后必须真的发出 xhr/fetch 请求才算点中。
                # 2026-09-01 联调：旧 _click_by_text('查询', exact=False) 子串匹配点到
                # 页头「中国专利审查信息查询」按钮，页面纹丝不动；而结果区默认就有
                # 「共0条」空态，wait_search_results 被骗直接判 empty。
                search_api_url = ''
                search_api_post_data = ''
                search_api_resp_body = ''
                search_api_resp_headers: Dict[str, str] = {}
                search_api_req_headers: Dict[str, str] = {}
                search_api_status = 0
                try:
                    # 谓词收窄到真实查询 API（两轮实测 /api/search/decision/wxxx/search），
                    # 避免瑞数心跳等无关 xhr 抢先命中门禁。
                    gate_timeout = 150_000 if manual_click else 8_000
                    with page.expect_response(
                        lambda r: r.request.resource_type in ('xhr', 'fetch')
                        and '/api/search/' in r.url,
                        timeout=gate_timeout,
                    ) as resp_info:
                        if manual_click:
                            _log(
                                '★ 手工协助模式：请在弹出的浏览器窗口里，'
                                '用鼠标手工点击「查询」按钮（2.5 分钟内）——'
                                '用于判别：同一浏览器里手工点击是否也被拒（400）。'
                            )
                        elif not _click_search(page):
                            raise RuntimeError('未能点击查询按钮')
                    resp = resp_info.value
                    search_api_url = resp.url
                    search_api_status = int(resp.status)
                    try:
                        search_api_post_data = (resp.request.post_data or '')[:1000]
                    except Exception:
                        search_api_post_data = ''
                    try:
                        search_api_resp_body = (resp.text() or '')[:2000]
                    except Exception:
                        search_api_resp_body = ''
                    try:
                        keep = ('content-type', 'server', 'content-length', 'date')
                        search_api_resp_headers = {
                            k: v
                            for k, v in (resp.headers or {}).items()
                            if k.lower() in keep
                        }
                    except Exception:
                        search_api_resp_headers = {}
                    try:
                        rh = resp.request.headers or {}
                        keep_req = (
                            'user-agent', 'content-type', 'accept', 'origin',
                            'referer', 'x-requested-with', 'cookie',
                        )
                        search_api_req_headers = {}
                        for k in keep_req:
                            v = rh.get(k) or rh.get(k.title())
                            if v:
                                # cookie 只记长度，避免敏感值落盘
                                search_api_req_headers[k] = (
                                    f'<{len(v)} chars>' if k == 'cookie' else v[:400]
                                )
                    except Exception:
                        search_api_req_headers = {}
                    _log(
                        f'[search] 查询请求已发出 HTTP {search_api_status} → {search_api_url[:200]}'
                        + (f' body={search_api_post_data[:300]}' if search_api_post_data else '')
                    )
                    if search_api_resp_headers:
                        _log(f'[search] 响应头 → {search_api_resp_headers}')
                    if search_api_req_headers:
                        _log(f'[search] 请求头 → {search_api_req_headers}')
                    if search_api_resp_body:
                        _log(f'[search] 服务端响应 → {search_api_resp_body[:300]}')
                except RuntimeError:
                    raise
                except Exception as exc:
                    dumper.dump(page, 'FAIL_search_not_fired')
                    if manual_click:
                        raise RuntimeError(
                            '手工协助模式：2.5 分钟内未捕获查询请求——'
                            '请确认点击的是浏览器窗口里的「查询」按钮'
                            f'（{exc}）'
                        ) from exc
                    raise RuntimeError(
                        '点击查询后 8s 内未见任何 xhr/fetch 请求——查询按钮未被有效点击'
                        f'（{exc}）'
                    ) from exc

                status = wait_search_results(page, 20_000)
                dismiss_cpquery_usage_dialog(page)
                # 空结果时不解析：parse_card_results 的正文兜底会把「案件编号：」筛选项
                # 标签误当结果行（2026-09-01 实测产出 假 items）。
                items = [] if status == 'empty' else parse_list_table(page, max_items)
                dumper.dump(page, f'06_results_{status}')

                headers = visible_table_headers(page)
                total_text = extract_total_text(page)

                payload = {
                    'success': True,
                    'summary': f'cpquery 检索完成（{status}），解析 {len(items)} 条'
                    + (f'，服务端 {total_text}' if total_text else ''),
                    'library_type': library_type,
                    'library_verified': library_verified,
                    'search_form_verified': True,
                    'search_api_url': search_api_url,
                    'search_api_status': search_api_status,
                    'search_api_post_data': search_api_post_data,
                    'search_api_resp_body': search_api_resp_body,
                    'search_api_resp_headers': search_api_resp_headers,
                    'search_api_req_headers': search_api_req_headers,
                    'api_calls': api_calls[-50:],
                    'cookie_inventory': cookie_inventory,
                    'manual_click': manual_click,
                    'filters': filters,
                    'results_status': status,
                    'filter_report': filter_report,
                    'table_headers': headers,
                    'total_text': total_text,
                    'items': items,
                    'debug_dir': dumper.dir,
                    'session_file': session_file,
                    'browser_mode': bs.mode,
                }
                cache_file = save_json_output(payload, args.get('output_path'))
                payload['cache_file'] = cache_file
                save_session_state(bs.context, session_file, bs)
                emit_result(payload)
            finally:
                close_browser_session(bs)
    except Exception as exc:
        _log(f'[cnipa_search] error: {exc}')
        dumper.dump_fail(locals().get('page'), 'search_main', exc)
        emit_result(
            {
                'success': False,
                'error': str(exc),
                'items': [],
                'filter_report': locals().get('filter_report') or {},
                'debug_dir': dumper.dir,
            }
        )


def _click_search(page) -> bool:
    """点「查询」按钮。

    2026-09-01 联调实测 DOM：真查询按钮是 Quasar
    <button class="q-btn"><span class="q-btn__wrapper"><span class="q-btn__content">
      <i class="material-icons">search</i><span class="block">查询</span>…
    页头还有「中国专利审查信息查询」「多国发明专利审查信息查询」两个 q-btn，
    子串匹配（exact=False）必误点它们 → 必须用文本恰好为「查询」的精确选择器。
    """
    # 精确选择器优先：span 文本恰为“查询”的 q-btn（真按钮，全页唯一）
    for selector in (
        'button.q-btn:has(span:text-is("查询"))',
        'button[aria-label="查询"]',
        'button:has(> span:has(> span:has(> span:text-is("查询"))))',
    ):
        try:
            el = page.locator(selector).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=5000)
                _log(f'[search] 已点击查询按钮（{selector}）')
                return True
        except Exception:
            continue
    # 兜底（其他非 Quasar 页面）：先精确角色名，再旧的子串方案
    try:
        el = page.get_by_role('button', name='查询', exact=True).first
        if el.count() > 0:
            el.click(timeout=5000)
            _log('[search] 已点击查询按钮（get_by_role exact）')
            return True
    except Exception:
        pass
    from cnipa_common import _click_by_text

    return _click_by_text(page, ['查 询', '搜 索', '检索'], exact=False)


if __name__ == '__main__':
    main()
