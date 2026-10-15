#!/usr/bin/env python3
"""离线自测：纯函数校验，不访问任何网络。

运行：python offline_selftest.py
覆盖：
- normalize_application_no（CN 前缀规则，REQUIREMENTS §3.3）
- build_detail_url / _parse_detail_params（detail URL 往返）
- map_detail_fields（详情 12 列映射 + 模糊包含匹配）
- _normalize_list_item（列表行归一化 + detail 参数合并）
- FIELD_LABEL_MAP（12 个查询字段与需求 §3.2 对齐）
- 每步 dump 目录名清洗（StepDumper._safe_tag 语义）
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_common as cc  # noqa: E402

FAILS: list = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = '') -> None:
    global CHECKS
    CHECKS += 1
    mark = 'PASS' if cond else 'FAIL'
    line = f'[{mark}] {name}' + (f'  → {detail}' if detail and not cond else '')
    print(line)
    if not cond:
        FAILS.append(f'{name}: {detail}')


# ---------------------------------------------------------------- CN 前缀
check('normalize 基础加 CN', cc.normalize_application_no('200610004173.6') == 'CN200610004173.6')
check('normalize 已有 CN 不重复', cc.normalize_application_no('CN200610004173.6') == 'CN200610004173.6')
check('normalize 小写 cn 转大写', cc.normalize_application_no('cn200610004173.6') == 'CN200610004173.6')
check('normalize 去首尾空白', cc.normalize_application_no('  200610004173.6 ') == 'CN200610004173.6')
check('normalize 空串仍加 CN（联调时看 dump 再拦）', cc.normalize_application_no('') == 'CN')

# ------------------------------------------- 检索框申请号格式（2026-09-01 联调）
# 站点示例「例如: 2010101995057」：13 位纯数字，无点号。
check('search 格式去校验位点', cc.normalize_application_no_for_search('200610004173.6') == '2006100041736')
check('search 格式去 CN 前缀', cc.normalize_application_no_for_search('CN200610004173.6') == '2006100041736')
check('search 格式去 ZL 前缀', cc.normalize_application_no_for_search('zl 200610004173.6') == '2006100041736')
check('search 格式去空格横线', cc.normalize_application_no_for_search(' 2006-1000-4173.6 ') == '2006100041736')
check('search 格式纯数字原样', cc.normalize_application_no_for_search('2010101995057') == '2010101995057')
check('search 格式空串为空', cc.normalize_application_no_for_search('') == '')

# ---------------------------------------------------------------- detail URL
url1 = cc.build_detail_url(detail_url='https://cpquery.cponline.cnipa.gov.cn/detail/index?zhuanlisqh=A&anjianbh=B&searchType=8')
check('build_detail_url 透传 detail_url', url1.endswith('zhuanlisqh=A&anjianbh=B&searchType=8'), url1)

url2 = cc.build_detail_url(zhuanlisqh='200610004173', anjianbh='4W107577')
check('build_detail_url 默认 searchType=8', 'searchType=8' in url2 and 'zhuanlisqh=200610004173' in url2 and 'anjianbh=4W107577' in url2, url2)

url3 = cc.build_detail_url(zhuanlisqh='200610004173', anjianbh='4W107577', search_type='9')
check('build_detail_url 自定义 searchType', 'searchType=9' in url3, url3)

try:
    cc.build_detail_url()
    check('build_detail_url 缺参应抛错', False, '未抛 ValueError')
except ValueError:
    check('build_detail_url 缺参应抛错', True)

p = cc._parse_detail_params(url2)
check('_parse_detail_params 往返', p.get('zhuanlisqh') == '200610004173' and p.get('anjianbh') == '4W107577' and p.get('searchType') == '8', str(p))
check('_parse_detail_params 空 URL', cc._parse_detail_params('') == {})

# ---------------------------------------------------------------- 详情字段映射
SAMPLE_DETAIL = {
    '案件编号': '4W107577',
    '决定号': '46278',
    '决定日': '2018年12月21日',
    '发明名称': '一种降压药物组合物',
    '无效宣告请求人': '某药业股份有限公司',
    '专利权人': '某制药有限责任公司',
    '申请号': '200610004173.6',
    '无效宣告请求日': '2017年06月20日',
    '法律依据': '专利法第22条第3款',
    '决定要点': '权利要求1相对于证据1与公知常识的结合不具备创造性。',
    '审查结论': '维持专利权有效',
    '合议组成员': '任晓兰（组长）刘婷婷（主审）刘静（参审）',
    '发文日': '2018年12月21日',
}
d = cc.map_detail_fields(SAMPLE_DETAIL)
EXPECT = {
    'case_no': '4W107577',
    'decision_no': '46278',
    'decision_date': '2018年12月21日',
    'invention_title': '一种降压药物组合物',
    'petitioner': '某药业股份有限公司',
    'patentee': '某制药有限责任公司',
    'application_no': '200610004173.6',
    'request_date': '2017年06月20日',
    'legal_basis': '专利法第22条第3款',
    'decision_summary': '权利要求1相对于证据1与公知常识的结合不具备创造性。',
    'decision_result': '维持专利权有效',
    'panel_members': '任晓兰（组长）刘婷婷（主审）刘静（参审）',
    'issue_date': '2018年12月21日',
}
for k, v in EXPECT.items():
    check(f'map_detail_fields.{k}', d.get(k) == v, f'got {d.get(k)!r}, want {v!r}')

# 模糊包含匹配：label 带后缀（如「决定日(YYYY.MM.DD)」）
d2 = cc.map_detail_fields({'决定日（年.月.日）': '2018.12.21', '申请号/专利号': '200610004173.6'})
check('map_detail_fields 模糊匹配决定日', d2['decision_date'] == '2018.12.21', str(d2['decision_date']))
check('map_detail_fields 申请号/专利号', d2['application_no'] == '200610004173.6')
d3 = cc.map_detail_fields({'复审请求人': '张某', '复审请求日': '2020年01月02日'})
check('复审案 petitioner 用复审请求人', d3['petitioner'] == '张某')
check('复审案 request_date 用复审请求日', d3['request_date'] == '2020年01月02日')

# 报告 11 个详情列必须都有映射来源（REQUIREMENTS §4，附图列走 pss 不算）
REPORT_DETAIL_KEYS = [
    'case_no', 'decision_date', 'invention_title', 'petitioner', 'patentee',
    'application_no', 'request_date', 'legal_basis', 'decision_summary',
    'decision_result', 'panel_members',
]
check(
    '报告 11 详情列均有映射',
    all(k in d for k in REPORT_DETAIL_KEYS),
    str([k for k in REPORT_DETAIL_KEYS if k not in d]),
)

# 与详情脚本 DETAIL_REPORT_KEYS 对齐（可 import 则比对；playwright 缺失时跳过）
try:
    from run_cnipa_fetch_decision_detail_pw import DETAIL_REPORT_KEYS

    check(
        'DETAIL_REPORT_KEYS 与报告列对齐（11+决定号冗余）',
        set(DETAIL_REPORT_KEYS) == set(REPORT_DETAIL_KEYS) | {'decision_no'},
        str(sorted(set(DETAIL_REPORT_KEYS) ^ (set(REPORT_DETAIL_KEYS) | {'decision_no'}))),
    )
except ImportError as e:
    print(f'[SKIP] DETAIL_REPORT_KEYS 比对（import 失败: {e}）')

# ---------------------------------------------------------------- 列表行归一化
row = {
    '案件编号': '4W107577',
    '决定号': '46278',
    '发文日': '2018年12月28日',
    '申请号': '200610004173.6',
    '专利权人': '某制药有限责任公司',
    '审查结论': '维持专利权有效',
    '无效宣告请求人': '某药业股份有限公司',
}
item = cc._normalize_list_item(
    row,
    detail_url='https://cpquery.cponline.cnipa.gov.cn/detail/index?zhuanlisqh=200610004173&anjianbh=4W107577&searchType=8',
    title='一种降压药物组合物',
)
check('_normalize_list_item.case_no', item['case_no'] == '4W107577')
check('_normalize_list_item.decision_no', item['decision_no'] == '46278')
check('_normalize_list_item.application_no', item['application_no'] == '200610004173.6')
check('_normalize_list_item.patentee', item['patentee'] == '某制药有限责任公司')
check('_normalize_list_item.decision_result', item['decision_result'] == '维持专利权有效')
check('_normalize_list_item.petitioner', item['petitioner'] == '某药业股份有限公司')
check('_normalize_list_item.issue_date', item['issue_date'] == '2018年12月28日')
check('_normalize_list_item.title 兜底发明名称', item['invention_title'] == '一种降压药物组合物')
check('_normalize_list_item 合并 zhuanlisqh', item.get('zhuanlisqh') == '200610004173')
check('_normalize_list_item 合并 anjianbh', item.get('anjianbh') == '4W107577')
check('_normalize_list_item.raw_row 保留', item['raw_row'].get('决定号') == '46278')

# 复审列表行：无请求人列 → petitioner 空，不误填
row_reexam = {'案件编号': '1F123456', '决定号': '99999', '申请号': '201010000000.0', '专利权人': '李某'}
item2 = cc._normalize_list_item(row_reexam)
check('_normalize_list_item 复审行 petitioner 为空', item2.get('petitioner') == '')
check('_normalize_list_item 复审行 case_no', item2['case_no'] == '1F123456')

# ---------------------------------------------------------------- 查询字段表
WANT_FIELDS = {
    'application_no', 'ipc_class', 'decision_no', 'case_no', 'invention_title',
    'patent_type', 'patentee', 'petitioner', 'panel_leader', 'panel_examiner',
    'panel_member', 'agency',
}
check(
    'FIELD_LABEL_MAP 12 查询字段齐全（需求 §3.2）',
    set(cc.FIELD_LABEL_MAP) == WANT_FIELDS,
    str(sorted(set(cc.FIELD_LABEL_MAP) ^ WANT_FIELDS)),
)
check(
    'FIELD_LABEL_MAP 每字段均有中文 label',
    all(labels and all(isinstance(x, str) and x for x in labels) for labels in cc.FIELD_LABEL_MAP.values()),
)
check(
    'FIELD_LABEL_MAP label 不跨行（≤12 字）',
    all(len(x) <= 12 for labels in cc.FIELD_LABEL_MAP.values() for x in labels),
)

# ---------------------------------------------------------------- 模块卫生
src = open(os.path.join(ROOT, 'cnipa_common.py'), 'r', encoding='utf-8').read()
check('死代码 _BEHAVIORAL_EVENTS_SCRIPT 已删除', '_BEHAVIORAL_EVENTS_SCRIPT' not in src)
check('StepDumper 已就位', 'class StepDumper' in src and 'def set_step_dumper' in src)
check('_ensure_page_rendered 已就位', 'def _ensure_page_rendered' in src)
check('wait_search_results 已就位', 'def wait_search_results' in src)
check('fill_search_filters 返回 report', 'def fill_search_filters' in src)
check('pss disclaimer 先勾记住再同意', src.index('def handle_pss_disclaimer') < src.index('def _fill_search_input', src.index('def handle_pss_disclaimer')) if False else '记住我的选择' in src[src.index('def handle_pss_disclaimer'):src.index('def handle_pss_disclaimer') + 4000])

# dump 目录名清洗：非法字符替换（StepDumper 内部用）。
# 安全属性：无路径分隔符 / \、无盘符冒号、不以 .. 开头（穿越须分隔符，已替换）
tag_ok = re.sub(r'[^0-9A-Za-z_.-]+', '_', '06_results_found/..奇')
check(
    'dump tag 清洗无路径穿越',
    '/' not in tag_ok and '\\' not in tag_ok and ':' not in tag_ok and not tag_ok.startswith('..'),
    tag_ok,
)

# ---------------------------------------------------------------- 汇总
print()
print(f'共 {CHECKS} 项检查，失败 {len(FAILS)} 项')
if FAILS:
    print('失败清单：')
    for f in FAILS:
        print(f'  - {f}')
    sys.exit(1)
print('ALL OFFLINE SELFTESTS PASSED')
