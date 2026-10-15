#!/usr/bin/env python3
"""离线选择器回放：用 debug_steps 里落盘的真实 HTML 验证选择器，不访问国知局。

用法：
    python -X utf8 offline_selector_check.py [dump目录]

默认用 output/debug_steps/ 下最新的 search_* 目录里的 05/06 dump。
2026-09-01 新增：查询按钮精确选择器 / Quasar radio 选库 / loading 判据 / 假结果防护。
"""

from __future__ import annotations

import glob
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402

import cnipa_common as cc  # noqa: E402


def find_latest_dump() -> str:
    runs = sorted(glob.glob(os.path.join(ROOT, 'output', 'debug_steps', 'search_*')))
    if not runs:
        raise SystemExit('no search_* dump dir found')
    run = runs[-1]
    for tag in ('06_05_filters_filled', '05_04_expanded_all_fields'):
        p = os.path.join(run, f'{tag}.html')
        if os.path.isfile(p):
            return p
    files = sorted(glob.glob(os.path.join(run, '*.html')))
    return files[-1]


def main() -> None:
    html_path = sys.argv[1] if len(sys.argv) > 1 else find_latest_dump()
    with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
        html = f.read()
    print(f'[replay] {html_path} ({len(html)} chars)')

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        print(f'[{"PASS" if ok else "FAIL"}] {name}' + (f'  {detail}' if detail else ''))
        if not ok:
            failures.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto('about:blank')
        page.set_content(html)
        page.wait_for_timeout(300)

        # 1) 查询按钮精确选择器：全页唯一，且不是页头两个大按钮
        btn = page.locator('button.q-btn:has(span:text-is("查询"))')
        n = btn.count()
        check('查询按钮唯一', n == 1, f'count={n}')
        if n == 1:
            # 注意：按钮 inner_text 含 material icon ligature「search」→「search查询」；
            # 精确文本在 span.block 上。
            span_txt = (btn.first.locator('span.block').first.inner_text() or '').strip()
            check('查询按钮文本正确', span_txt == '查询', repr(span_txt))
            check('查询按钮可见', btn.first.is_visible())

        # 2) 页头按钮不含精确文本“查询”（证明 exact 语义有效）
        header = page.locator('button.q-btn:has(span:text-is("中国专利审查信息查询"))')
        check('页头按钮可区分', header.count() == 1 and n == 1)

        # 3) 选库 radio（aria-checked 实测写法）
        for label, want in (('无效决定库', True), ('复审决定库', False)):
            sel = f'[role="radio"][aria-checked="true"][aria-label="{label}"]'
            cnt = page.locator(sel).count()
            check(f'{label} aria-checked', cnt == (1 if want else 0), f'count={cnt}')

        # 4) loading 判据在静态（无请求）页面应为 0，避免误等
        loading = page.locator(
            '.el-loading-mask:visible, .q-inner-loading:visible, '
            '.q-table__progress:visible, .q-linear-progress:visible, '
            'button.q-btn:has(.q-spinner):visible'
        ).count()
        check('静态页无 loading', loading == 0, f'count={loading}')

        # 5) 假结果防护：检索页正文不应解析出任何结果条目
        try:
            items = cc.parse_card_results(page, 50)
        except Exception as exc:  # noqa: BLE001
            items = None
            check('parse_card_results 不抛异常', False, str(exc))
        if items is not None:
            check('筛选页解析不出假结果', len(items) == 0, f'items={len(items)}')
            for it in items:
                if it.get('case_no'):
                    print(f'  ⚠ item.case_no={it["case_no"]!r}')

        # 6) 复审无效检索页探测的 DOM 子条件（_search_form_present 首步校验 cpquery
        #    域名，file:// 回放必为 False，故这里直接验证其 DOM 判据）：
        #    a) 完整 DOM 含「决定号」且含「案件编号」（决定号在展开区，content() 可见）
        #    b) 激活 q-tab 的 text/title 含 复审/无效 + 查询
        html_now = page.content() or ''
        check('页面含决定号字段', '决定号' in html_now)
        check('页面含案件编号字段', '案件编号' in html_now)
        active_ok = False
        active_detail = ''
        try:
            tabs = page.locator('.q-tab--active')
            for i in range(min(tabs.count(), 30)):
                t = tabs.nth(i)
                text = (t.inner_text() or '').strip() or (t.get_attribute('title') or '').strip()
                if ('复审' in text or '无效' in text) and '查询' in text:
                    active_ok = True
                    active_detail = text
                    break
        except Exception:
            pass
        check('激活tab为复审无效查询', active_ok, active_detail)

        browser.close()

    if failures:
        print(f'\n{len(failures)} FAILED: {failures}')
        raise SystemExit(1)
    print('\nSELECTOR REPLAY ALL PASSED')


if __name__ == '__main__':
    main()
