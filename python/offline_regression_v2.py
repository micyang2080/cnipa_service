# -*- coding: utf-8 -*-
"""离线端到端回归（技能侧契约 §9.3，2026-09-03）。

用 2026-09-01 真实跑（run_20260901_171031 卡片 + OCR 夹具）固定值，验证
契约 v2 全链路投影（零窗口操作、零站点交互）：

  ① search：runner 原始卡片（内部旧键）→ clean_search → 固定 20 键、
     application_no 含点、detail_url 模板串、library_type 规范 token；
  ② detail：卡片 × OCR 夹具 merge（12/12 列）→ clean_detail → 决定日/
     请求日/法律依据/要点/合议组全量 + detail_url；
  ③ list_item 回环：① 的 item 原样喂 translate_inputs → 库别/对拍号/
     card_fields 转译正确（技能 ingest 前置链）；
  ④ detail_url 回显：detail 入参带了 detail_url 时优先回显；
  ⑤ figure：clean_figure 含点回显（figure-map 的键形态）；
  ⑥ 零映射：items[]/detail 键集 == 技能 schema 权威键名（ingest 无需改名）。

用法：python -X utf8 offline_regression_v2.py   （exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_contract as ct  # noqa: E402
import cnipa_ocr as co  # noqa: E402
import ocr_fixtures_detail as fx  # noqa: E402
from run_cnipa_fetch_decision_detail_uia import (  # noqa: E402
    _CARD_FIXTURE,
    merge_detail,
    translate_inputs,
)

FAILS = 0


def check(name: str, cond: bool, extra: str = '') -> None:
    global FAILS
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  {extra}' if extra and not cond else ''))
    FAILS += 0 if cond else 1


EXPECT_URL_4W = ('https://cpquery.cponline.cnipa.gov.cn/detail/index'
                 '?zhuanlisqh=200610004173.6&anjianbh=4W107577&searchType=8')

# ── ① search：真实卡片（runner 内部键，run_20260901 实测值）→ 契约投影 ──
real_card = dict(_CARD_FIXTURE)          # patent_type/title/case_no/decision_no/
                                          # issue_date/app_no/patentee/app_date/
                                          # class_no/conclusion/invalid_requester
s = ct.clean_search({'ok': True, 'library_radio': '无效决定库',
                     'total_after': 1, 'results': [real_card]})
check('R1) search success + library_type 规范 token',
      s.get('success') is True and s.get('library_type') == 'invalid'
      and s.get('total') == 1 and s.get('truncated') is False, repr(s.get('library_type')))
it = (s.get('items') or [{}])[0]
check('R2) items 固定 20 键且顺序一致（零映射）',
      tuple(it.keys()) == ct.CASE_OUT_KEYS, str(list(it.keys())))
check('R3) application_no 含点', it['application_no'] == '200610004173.6', it['application_no'])
check('R4) detail_url 模板串', it['detail_url'] == EXPECT_URL_4W, it['detail_url'])
check('R5) 键值改译：title→invention_title / conclusion→decision_result / '
      'invalid_requester→petitioner / class_no→ipc_class',
      it['invention_title'] == real_card['title']
      and it['decision_result'] == '维持有效'
      and it['petitioner'] == '李瑶'
      and it['ipc_class'] == 'C07C229/24'
      and it['patentee'] == '远大医药（天津）有限公司'
      and it['decision_no'] == '38430' and it['case_no'] == '4W107577')
check('R6) 日期 YYYY.MM.DD + 列表层空字段',
      it['issue_date'] == '2018.12.28' and it['decision_date'] == ''
      and it['figure_path'] == '' and it['request_date'] == '')
check('R7) review_type/source/patent_type',
      it['review_type'] == 2 and it['source'] == 'cnipa' and it['patent_type'] == '发明专利')
check('R8) 废弃旧键绝迹',
      not any(k in it for k in ('app_no', 'title', 'conclusion',
                                'invalid_requester', 'reexam_requester')))

# ── ② detail：卡片 × 真实 OCR 夹具 merge → 契约投影 ──
ocr = co.parse_detail_pages([fx.PAGE1, fx.PAGE_SIG, fx.PAGE2])
merged = merge_detail(_CARD_FIXTURE, ocr)
d = ct.clean_detail({'ok': True, 'library_radio': 'invalid', 'detail': dict(merged)})
det = d.get('detail') or {}
check('D1) detail success + 固定 20 键（零映射）',
      d.get('success') is True and tuple(det.keys()) == ct.CASE_OUT_KEYS,
      str(list(det.keys())))
check('D2) 决定日（OCR 决定书落款，优先卡片发文日回退）',
      det['decision_date'] == '2018.12.21', det['decision_date'])
check('D3) 请求日/法律依据/决定要点/合议组',
      det['request_date'] == '2018.07.24'
      and det['legal_basis'] == '专利法第22条第2款，第22条第3款'
      and det['decision_summary'].startswith('在无效程序中')
      and det['panel_members'].startswith('合议组组长'))
check('D4) application_no 含点 + detail_url',
      det['application_no'] == '200610004173.6' and det['detail_url'] == EXPECT_URL_4W)
check('D5) 卡片精确值覆盖 OCR 噪声（patentee/结论）',
      det['patentee'] == '远大医药（天津）有限公司' and '维持' in det['decision_result'])

# ── ③ list_item 回环：search item 原样 → translate_inputs ──
t = translate_inputs({'list_item': it})
check('T1) review_type=2 → invalid 库 + 对拍号回填',
      t['lib'] == 'invalid' and t['anjianbh'] == '4W107577'
      and t['zhuanlisqh'] == '200610004173.6')
check('T2) item → card_fields 转译（新键→内部键）',
      t['card_fields'].get('app_no') == '2006100041736'
      and t['card_fields'].get('invalid_requester') == '李瑶'
      and t['card_fields'].get('title') == real_card['title']
      and t['card_fields'].get('conclusion') == '维持有效')

# ── ④ detail_url 回显优先 ──
d2 = ct.clean_detail({'ok': True, 'library_radio': 'invalid',
                      'detail': {**merged, 'detail_url': 'https://example/echo'}})
check('U1) detail_url 入参回显优先于模板构造',
      d2['detail']['detail_url'] == 'https://example/echo')

# ── ⑤ figure：含点回显（figure-map 键形态） ──
g = ct.clean_figure({'ok': True, 'application_no': '2006100041736',
                     'figure_path': r'D:\x\200610004173.6.png', 'method': 'saveas'})
check('F1) figure application_no 含点回显',
      g['application_no'] == '200610004173.6'
      and g['figure_path'].endswith('200610004173.6.png'))

print()
print('OFFLINE REGRESSION v2: ' + ('OK' if FAILS == 0 else f'{FAILS} FAIL'))
sys.exit(0 if FAILS == 0 else 1)
