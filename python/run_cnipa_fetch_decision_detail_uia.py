# -*- coding: utf-8 -*-
"""04 UIA 版：复审/无效决定详情页抓取（免 CDP，真实键鼠驱动用户日常 Chrome）。

进页方式（2026-09-01 19:40 三案定案，用户点破）：
    ✗ 旧案 Ctrl+T 直敲 detail/index URL —— 实跑 40s 只渲染出壳（数据接口
      一个字段不吐，run=detail_20260901_172905）。page1 地址栏实锤：URL 参数
      是会话加密 token（zhuanlisqh=pZTOc…%3D%3D），离站直入必然失效。
    ✗ 二案 瞬移点击卡片标题 —— 19:02 实跑同页跳转、右栏空壳（瞬移+零按压
      被瑞数行为风控判非人，站点弃 window.open 退化 SPA 跳转）。
    ✓ 三案 human_click 拟人点击卡片标题（轨迹+悬停+90ms 按压）→ 新标签
      打开 /detail/index，右栏**多已直接渲染**决定书（19:34 实跑 12/12 列，
      nav_drill 点击实属多余）→ wait_doc 三位置探测确认；未就绪才走
      **页内左导航下钻**兜底（18:56 树 dump 实证：复审无效审查决定(分组)
      → 无效/复审审查决定(分支) → 案件项(4W…)；分支择路按卡片
      invalid_requester 非空→无效审查决定）。

内容获取（2026-09-01 17:46 树 dump + page1/page2 OCR 实证）：
    右栏决定书是**整页图片**渲染（约 9 张 Image、零 Text）→ UIA 文本通道
    永远拿不到正文。唯一文本化通道 = 滚动截图 + Windows zh-CN OCR：
      · 首屏：顶栏摘要条 + 决定书头（（第38430号））+ **勾选框结论**
        （囗=未选 / 冈=勾选：冈 维持专利权有效）；
      · 第二屏：信息表（标签列×值列**逐行同 y 对齐** → 同排右值连接）给
        决定日/请求日/法律依据/分类号等；表末「决定要点」正文全宽多行，
        可能溢出到第三屏（用户 18:04 提示）→ 续屏收割至要点不再增长或遇
        合议组行，上限 4 屏（用户 18:01：前 2 页通常已含全部 12 列）。
    12 列合并策略：卡片 8 列（02/03 结果页 a11y 精确值）覆盖 OCR 噪声值；
    request_date/legal_basis/decision_summary/panel_members 仅 OCR 提供。

防串窗：每屏截图前 set_focus（17:40 教训：40s 等待期间用户切走，截到 Cowork）。

用法（用户本机执行，保持结果页在前台）：
    python -X utf8 run_cnipa_fetch_decision_detail_uia.py <input.json>
    python -X utf8 run_cnipa_fetch_decision_detail_uia.py --selftest
input.json（技能侧契约 §3.1，2026-09-03 扩充）：
    application_no / case_no（建议，对拍与导航；等价旧键 zhuanlisqh/anjianbh）、
    library_type（"invalid"|"reexam"，择路左导航分支）、
    detail_url（可选：模板链接，缺号时从中抽 anjianbh/zhuanlisqh，并原样回显）、
    list_item（可选：cnipa_search_decisions 的 item 原样传入，卡片精确值
    优先于 OCR——内部转译为 card_fields）、
    card_fields（02/03 卡片字段，可选）、
    keep_tab（默认 false：跑完关详情标签回结果页）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_contract  # noqa: E402
import cnipa_cache  # noqa: E402
import cnipa_dev  # noqa: E402
import cnipa_uia as uia  # noqa: E402
import cnipa_ocr as co  # noqa: E402
from run_cnipa_search_decisions_uia import (  # noqa: E402
    TYPE_RE,
    PAIR_RE,
    _dedup_items,
    _norm_cpquery_appno,
    flat_tree_dump,
    locate_card_for_list_item,
    ordered_texts,
)

DETAIL_REPORT_KEYS = [
    'case_no', 'decision_no', 'decision_date', 'invention_title',
    'petitioner', 'patentee', 'application_no', 'request_date',
    'legal_basis', 'decision_summary', 'decision_result', 'panel_members',
]

# 与 cnipa_common.map_detail_fields 同一套标签（含各库叫法差异的备选）
DETAIL_LABELS = {
    'case_no': ('案件编号',),
    'decision_no': ('决定号',),
    'decision_date': ('决定日', '发文日'),
    'invention_title': ('发明名称', '发明创造名称', '名称'),
    'petitioner': ('无效宣告请求人', '复审请求人', '请求人'),
    'patentee': ('专利权人',),
    'application_no': ('申请号/专利号', '申请号', '专利号'),
    'request_date': ('无效宣告请求日', '复审请求日', '请求日'),
    'legal_basis': ('法律依据',),
    'decision_summary': ('决定要点',),
    'decision_result': ('审查结论', '决定结果'),
    'panel_members': ('合议组成员', '合议组'),
}
BARE_LABELS = {lab for labs in DETAIL_LABELS.values() for lab in labs}

READY_MIN_FIELDS = 6   # a11y 文本通道的就绪阈值（仅顶栏摘要，正文是图片）

# 卡片(02/03 a11y 精确值) → 详情列：同名直覆盖（OCR 噪声值让位）
CARD_TO_DETAIL = {
    'case_no': 'case_no',
    'decision_no': 'decision_no',
    'title': 'invention_title',
    'invalid_requester': 'petitioner',
    'patentee': 'patentee',
    'app_no': 'application_no',
}
# 合并优先级：卡片 > a11y > OCR；decision_date/result 缺省回填卡片 issue_date/conclusion
# decision_result：a11y 若罗列多选项则改用 OCR/卡片

# ---------------------------------------------------------------------------
# 页内左导航下钻（19:02 实跑改判；18:56 树 dump 冻结实证）
# ---------------------------------------------------------------------------
# 18:56 树（run_20260901_185633/tree_flat.txt，展开态）：
#   TreeItem '\ue791 \ue784 复审无效审查决定' @(258,557,606,662)
#     Text ' 复审无效审查决定' @(306,562,422,579)     ← 分组（可展开）
#   TreeItem '\ue791 \ue784 复审审查决定'   @(258,583,606,610)
#     Text ' 复审审查决定'      @(324,588,412,605)     ← 分支 A
#   TreeItem '\ue791 \ue784 无效审查决定'   @(258,609,606,662)
#     Text ' 无效审查决定'      @(324,614,412,631)     ← 分支 B
#   TreeItem '\ue791 \ue785 4W107577'       @(258,635,606,662)
#     Text ' 4W107577'          @(342,640,414,657)     ← 案件项（点击目标）
#   右栏顶栏摘要同名值 ' 4W107577' @(654,324,738,344) ← x=654 是干扰项，
#   以 x < NAV_X_MAX(620) 划界导航区。收起态（19:02）只见分组项。
NAV_X_MAX = 620
NAV_GROUP = '复审无效审查决定'
NAV_BRANCHES = ('无效审查决定', '复审审查决定')


def next_nav_action(items: list, case_no: str, prefer_branch: str = ''):
    """(text,rect) 列表 → 下一步点击目标 ('case'|'branch'|'group', text, rect)。

    优先级：案件项 > 分支项 > 分组项——分组已展开时树里必有分支/案件，永远
    走更深层级，不会误点分组把它收起。prefer_branch 命中时优先于另一分支
    （两分支常同时可见，18:56 树序 复审审查决定 在上，不择路会点错）。
    实矩形（_real）且在导航区（x<NAV_X_MAX）才可点；0 矩形=屏外，交由调用方处理。
    """
    items = _dedup_items(items)
    branch_hit = None
    group_hit = None
    for n, r in items:
        s = (n or '').strip()
        if not s or not _real(r) or r[0] >= NAV_X_MAX:
            continue
        if case_no and s == case_no:
            return ('case', s, r)
        if s in NAV_BRANCHES and (branch_hit is None or s == prefer_branch):
            branch_hit = ('branch', s, r)
        elif s == NAV_GROUP and group_hit is None:
            group_hit = ('group', s, r)
    if branch_hit:
        return branch_hit
    return group_hit


# doc-ready 判定（右栏决定书已渲染）：右栏(x>NAV_X_MAX)大字 OCR 锚词。
# 左导航小字（14px 级，误识多：中国→中苤）被 x 划界天然排除。
DOC_MARKERS = ('请求审查决定', '法律依据', '决定要点', '无效宣告请求人', '复审请求人')

# 新决定内嵌 PDF：顶屏 1 张 + 下滚 5 步各 1 张 = 共 6 屏（不依赖页码 OCR）
NEW_DECISION_OCR_PDF_PAGES = 2
NEW_DECISION_VIEWER_MAX_SHOTS = 6
NEW_DECISION_VIEWER_SCROLL_ROUNDS = NEW_DECISION_VIEWER_MAX_SHOTS - 1
NEW_DECISION_VIEWER_SCROLL_NOTCHES = 5

# 列表发文日补充判型：≥ 此年且页内 unknown 时倾向新决定（不保证准确）
ISSUE_DATE_HINT_NEW_FROM_YEAR = 2020


def _issue_date_year(card_fields: dict) -> int | None:
    """从列表卡片 issue_date / decision_date 提取四位年。"""
    cf = card_fields if isinstance(card_fields, dict) else {}
    raw = str(cf.get('issue_date') or cf.get('decision_date') or '').strip()
    if not raw:
        return None
    m = re.search(r'(20\d{2})', raw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def resolve_decision_type_kind(page_kind: str, card_fields: dict) -> tuple[str, str]:
    """页内 old/new 优先；页内 unknown 时用列表发文日补充：
    ≥2020 → new；有年且 <2020 → old；否则 ('', '') 由调用方失败。"""
    pk = str(page_kind or '').strip().lower()
    if pk in ('old', 'new'):
        return pk, 'page'
    yr = _issue_date_year(card_fields)
    if yr is None:
        return '', ''
    if yr >= ISSUE_DATE_HINT_NEW_FROM_YEAR:
        return 'new', f'issue_date_{yr}'
    return 'old', f'issue_date_{yr}'


def doc_marker(items: list) -> str:
    """OCR 行列表 → 命中的右栏决定书锚词（''=未见决定书）。"""
    for tx, bx in items:
        if bx[0] <= NAV_X_MAX:
            continue
        n = co.norm(tx)
        for m in DOC_MARKERS:
            if m in n:
                return m
    return ''


# ---------------------------------------------------------------------------
# 离线可测的纯解析
# ---------------------------------------------------------------------------

def _a11y_collect_prose(texts: list, start_i: int, *, max_lines: int = 24,
                        max_chars: int = 900, stop_labs: tuple = ()) -> str:
    """标签后连续多行正文（新决定阅读器 a11y）。"""
    parts = []
    stop_labs = stop_labs or ()
    for j in range(start_i, min(len(texts), start_i + max_lines)):
        raw = (texts[j] or '').strip()
        if not raw:
            continue
        bare = raw.rstrip('：:')
        if PAIR_RE.match(raw) or raw in BARE_LABELS or bare in BARE_LABELS:
            break
        if any(s and s in raw for s in stop_labs):
            break
        if re.match(r'^[一二三四五六七八九十1-9][、.．]?\s*案由', raw):
            break
        if any(h in raw for h in ('合议组组长', '主审员', '参审员', '合议组')):
            break
        if any(h in raw.lower() for h in (
                'savetmp', '.png', 'figure_saveas', 'cpquery')):
            continue
        if '地址和搜索栏' in raw:
            break
        parts.append(raw)
        if len(''.join(parts)) >= max_chars:
            break
    return ''.join(parts)


def _a11y_collect_summary(texts: list, start_i: int) -> str:
    return _a11y_collect_prose(texts, start_i, max_lines=24, max_chars=1200)


def _a11y_panel_members(texts: list) -> str:
    """从 a11y 拼合议组：组长/主审/参审。"""
    roles = {}
    for t in texts or []:
        s = (t or '').strip()
        m = re.match(r'^(合议组组长|组长|主审员|主审|参审员|参审)[：:\s]*(.+)$', s)
        if not m:
            continue
        role, name = m.group(1), m.group(2).strip()
        if not name or len(name) > 20:
            continue
        if '组' in role and '长' in role:
            roles['议组组长'] = name
        elif '主审' in role:
            roles['主审'] = name
        elif '参审' in role:
            roles['参审'] = name
    if not roles:
        return ''
    bits = []
    if roles.get('议组组长'):
        bits.append(f"合议组组长：{roles['议组组长']}")
    if roles.get('主审'):
        bits.append(f"主审员：{roles['主审']}")
    if roles.get('参审'):
        bits.append(f"参审员：{roles['参审']}")
    return '；'.join(bits)


def parse_detail_texts(texts: list) -> dict:
    """详情页 a11y 文本序列（顶栏摘要条）→ 标签值对。兼容三种排布：
      a) 整段 '标签：值' 同一 Text；
      b) '标签：' 后值为空，值在紧邻下一个 Text；
      c) 裸标签（无冒号）+ 紧邻下一个 Text 是值。
      决定要点：续读后续多行（勿只取一行）。
    """
    pairs: dict = {}

    def _put(k: str, v: str) -> None:
        k, v = k.strip(), v.strip()
        if k and v and (k not in pairs or len(v) > len(pairs[k])):
            pairs[k] = v

    for i, t in enumerate(texts):
        t = (t or '').strip()
        if not t:
            continue
        m = PAIR_RE.match(t)
        if m:
            lab, val = m.group(1).strip(), m.group(2).strip()
            if lab == '决定要点':
                body = val + _a11y_collect_summary(texts, i + 1)
                if body:
                    _put(lab, body)
                continue
            if lab == '法律依据':
                body = val + _a11y_collect_prose(
                    texts, i + 1, max_lines=6, max_chars=240,
                    stop_labs=('决定要点',))
                if body:
                    _put(lab, body)
                continue
            _put(lab, val)
            if not val and i + 1 < len(texts):
                nxt = (texts[i + 1] or '').strip()
                if nxt and not PAIR_RE.match(nxt):
                    if lab == '决定要点':
                        _put(lab, _a11y_collect_summary(texts, i + 1))
                    elif lab == '法律依据':
                        _put(lab, val + _a11y_collect_prose(
                            texts, i + 1, max_lines=6, max_chars=240,
                            stop_labs=('决定要点',)))
                    else:
                        _put(lab, nxt)
            continue
        bare = t.rstrip('：:')
        if (t in BARE_LABELS or bare in BARE_LABELS) and i + 1 < len(texts):
            lab = bare if bare in BARE_LABELS else t
            if lab == '决定要点':
                body = _a11y_collect_summary(texts, i + 1)
                if body:
                    _put(lab, body)
                continue
            if lab == '法律依据':
                body = _a11y_collect_prose(
                    texts, i + 1, max_lines=6, max_chars=240,
                    stop_labs=('决定要点',))
                if body:
                    _put(lab, body)
                continue
            nxt = (texts[i + 1] or '').strip()
            if nxt and not PAIR_RE.match(nxt) and nxt not in BARE_LABELS:
                _put(lab, nxt)
    panel = _a11y_panel_members(texts)
    if panel:
        _put('合议组成员', panel)
    # 决定号：（第 583248 号）
    if '决定号' not in pairs:
        for t in texts:
            m = re.search(r'[（(]第\s*(\d{4,7})\s*号[）)]', t or '')
            if m:
                _put('决定号', m.group(1))
                break
    return pairs


def map_detail(pairs: dict) -> dict:
    def pick(*keys: str) -> str:
        for k in keys:
            if pairs.get(k):
                return pairs[k]
        for pk, pv in pairs.items():
            if any(x and x in pk for x in keys) and pv:
                return pv
        return ''

    return {field: pick(*labs) for field, labs in DETAIL_LABELS.items()}


def pick_panel_members(a11y: dict, ocr: dict) -> str:
    """合议组：可信 PDF OCR 优先；否则可信 a11y；两路都脏则空串。"""
    pm_o = ((ocr or {}).get('panel_members') or '').strip()
    pm_a = ((a11y or {}).get('panel_members') or '').strip()
    if pm_o and co.panel_members_trustworthy(pm_o):
        return pm_o
    if pm_a and co.panel_members_trustworthy(pm_a):
        return pm_a
    return ''


def merge_channels(card: dict, a11y: dict, ocr: dict) -> dict:
    """12 列合并：卡片 > a11y > OCR（能 a11y 绝不用 OCR 糊字）。"""
    card = card or {}
    a11y = a11y or {}
    ocr = ocr or {}
    d = {}
    for k in DETAIL_REPORT_KEYS:
        av = (a11y.get(k) or '').strip()
        ov = (ocr.get(k) or '').strip()
        if av:
            d[k] = av
        elif ov:
            d[k] = ov
        else:
            d[k] = ''
    for ck, dk in CARD_TO_DETAIL.items():
        if str(card.get(ck) or '').strip():
            d[dk] = str(card[ck]).strip()
    if not d['decision_date'] and str(card.get('issue_date') or '').strip():
        d['decision_date'] = str(card['issue_date']).strip()
    if not d['decision_result'] and str(card.get('conclusion') or '').strip():
        d['decision_result'] = str(card['conclusion']).strip()
    # 审查结论：a11y 常罗列三个选项全文 → 仍用 OCR/卡片更准
    ar = (a11y.get('decision_result') or '')
    if ar and sum(x in ar for x in ('维持', '无效', '撤销')) >= 2 and (
            (ocr.get('decision_result') or '').strip()
            or str(card.get('conclusion') or '').strip()):
        d['decision_result'] = (
            (ocr.get('decision_result') or '').strip()
            or str(card.get('conclusion') or '').strip())
    return d


def merge_detail(card: dict, ocr: dict) -> dict:
    """兼容旧调用：仅卡片×单通道（视为已合并的 a11y/OCR）。"""
    return merge_channels(card, {}, ocr)


def reconcile_application_no(detail: dict, card: dict, a11y: dict, ocr: dict,
                             input_no: str):
    """申请号多通道纠偏（2026-09-01 21:5x 用户指出 payload 里出现三个不同值：
    OCR 残缺 '2010004173、6' / 卡片与 a11y 的 '2006100041736'）。

    merge_detail 已让卡片覆盖 OCR 残缺值；本函数兜"无卡片跑"（02/03 未传
    card_fields，纯 OCR+a11y）：归一化后与入参 zhuanlisqh 相等的通道值优先
    回填。返回 (最终值, 处置说明)；无入参或已一致则原样不动。
    """
    def norm(v) -> str:
        return re.sub(r'[^0-9A-Za-z]', '', str(v or '')).upper()

    cur = (detail.get('application_no') or '').strip()
    if not input_no:
        return cur, '未对拍(无入参)'
    want = norm(input_no)
    if norm(cur) == want:
        return cur, '一致'
    # input 本身不作为回填源 —— 回填回去等于循环自证，match 永真，
    # 真不一致就永远浮不出来（K3 自测冻结此坑）。
    for src, val in (('card', (card or {}).get('app_no')),
                     ('a11y', (a11y or {}).get('application_no')),
                     ('ocr', (ocr or {}).get('application_no'))):
        if val and norm(val) == want:
            return str(val).strip(), f'纠偏[{src}]'
    return cur, '不一致(无通道与入参相符，保留原值)'


def _channel_view(detail: dict, card: dict, ocr: dict, a11y: dict):
    """三通道去重视图（2026-09-01 20:35 用户反馈：payload 同值大量重复）。
    sources=每个最终字段实际来自哪些通道（值相同者合并，card+ocr 这种）；
    diffs=各通道与最终值**不同**的非空值（截 80 字）——只有这里才有调试增量，
    与最终值相同的通道副本不再落盘。card 先经 merge_detail 映射到 detail 键名
    （title→invention_title 等），否则同名比较无意义。"""
    ch = {'card': merge_detail(card or {}, {}), 'ocr': ocr or {}, 'a11y': a11y or {}}
    sources, diffs = {}, {'card': {}, 'ocr': {}, 'a11y': {}}
    for k in DETAIL_REPORT_KEYS:
        v = (detail.get(k) or '').strip()
        if not v:
            continue
        sources[k] = '+'.join(n for n, d in ch.items()
                              if str(d.get(k) or '').strip() == v) or 'merge'
        for n, d in ch.items():
            cv = str(d.get(k) or '').strip()
            if cv and cv != v:
                diffs[n][k] = cv[:80] + ('…' if len(cv) > 80 else '')
    return sources, diffs


def _real(r) -> bool:
    return r[2] > r[0] and r[3] > r[1]


def locate_card_link(items: list):
    """在已去重、按枚举序的 [(text, rect)] 里定位第一张卡的可点目标。

    03 实测树（run_20260901_171031）：卡片头 = 类型徽标（'发明专利 '）+ 同排
    右侧长标题（视觉链接，a11y 纯 Text）。返回 (kind, text, rect, real)，
    kind ∈ title / case_no_value / type_text；real=False（0 矩形屏外）时由
    调用方滚屏重采。无卡片返回 None。
    """
    items = _dedup_items(items)
    gate_idx = 0
    for idx, (n, r) in enumerate(items):
        if n.startswith('共查询到'):
            gate_idx = idx
            break
    n_items = len(items)
    i = gate_idx + 1
    while i < n_items:
        text, r = items[i]
        if not TYPE_RE.match(text):
            i += 1
            continue
        # ① 首选：同排右侧的非键值长文本 = 标题链接
        if i + 1 < n_items:
            t2, r2 = items[i + 1]
            same_row = abs(((r2[1] + r2[3]) // 2) - ((r[1] + r[3]) // 2)) <= 25
            if (same_row and not TYPE_RE.match(t2)
                    and PAIR_RE.match(t2) is None and len(t2.strip()) >= 4):
                return ('title', t2, r2, _real(r2))
        # ② 备选：'案件编号：值' 的值文本（或 标签：+紧邻值 拆分形态）
        for j in range(i + 1, min(i + 8, n_items)):
            tj, rj = items[j]
            if TYPE_RE.match(tj):
                break
            m = PAIR_RE.match(tj)
            if m and m.group(1).strip() == '案件编号':
                if m.group(2).strip():
                    return ('case_no_value', m.group(2).strip(), rj, _real(rj))
                if j + 1 < n_items:
                    tv, rv = items[j + 1]
                    if tv.strip() and not PAIR_RE.match(tv):
                        return ('case_no_value', tv.strip(), rv, _real(rv))
        # ③ 兜底：类型徽标本身（卡片头部区域）
        return ('type_text', text, r, _real(r))
    return None


def _run_ok(field_count: int, detail: dict) -> bool:
    """成功判定收紧（2026-09-01 19:02 假阳性教训）：字段数达标**且**右栏
    决定书至少捕到 1 个 OCR 专属字段（请求日/法律依据/决定要点/合议组）。
    此前仅看 field_count≥6 → 卡片字段凑 8/12 也 ok=true，掩盖右栏空壳。"""
    ocr_hit = any((detail.get(k) or '').strip()
                  for k in ('request_date', 'legal_basis',
                            'decision_summary', 'panel_members'))
    return field_count >= READY_MIN_FIELDS and ocr_hit


def _field_count(pairs: dict) -> int:
    return sum(1 for labs in DETAIL_LABELS.values()
               if any(pairs.get(x) for x in labs))


def _classify_state(texts: list) -> str:
    """从 a11y 文本判断前台标签是哪类页（三页共用同一窗口标题，无法靠标题）。"""
    joined = '\n'.join(t or '' for t in texts)
    if '国内案件信息详情' in joined or _detail_ready(texts):
        return '详情页（不是检索结果列表；请关掉/切走，或先跑 03 重建结果页）'
    if 'detail/index' in joined or 'anjianbh=' in joined.lower():
        return '详情页（空白/加载中）'
    if '共查询到' in joined:
        n = re.search(r'共查询到\s*(\d+)', joined)
        return (f"结果页但未识别到卡片（共{n.group(1) if n else '?'}条，"
                f"版式可能变化——dump 已落盘发回分析）")
    trivial = {'', '地址和搜索栏', 'Chrome Legacy Window', 'Google Chrome'}
    meaningful = {t.strip() for t in (texts or []) if t and t.strip() not in trivial}
    if len(meaningful) < 3:
        return '空白页（详情加载失败或空标签）'
    return '非结果页（入口/表单/其他标签；请先跑 03 拿到结果页并保持前台）'


def _center(r) -> tuple:
    return (r[0] + r[2]) // 2, (r[1] + r[3]) // 2


def _pane_xy(win, fx: float = 0.72, fy: float = 0.55) -> tuple:
    """详情右栏滚轮落点：按窗口客户区比例算，避免写死 1150,600 在最大化后打空。"""
    r = uia._rect(win)
    if not r or r[2] <= r[0] or r[3] <= r[1]:
        return 1150, 600
    x = int(r[0] + (r[2] - r[0]) * fx)
    y = int(r[1] + (r[3] - r[1]) * fy)
    return x, y


def _link_point(r) -> tuple:
    """卡片标题等长文本的可点落点：**左端**而非几何中心。

    2026-09-01 19:25 用户局部截图红框对拍：红框（屏幕≈327-931,723-779）=
    标题整行文字，与 a11y rect [344,750,912,768] 基本重合；旧中心点
    (628,759) 其实也在文字上 → 真正差异不在落点而在**点击方式**（瞬移 vs
    人手轨迹，见 cnipa_uia.human_click）。左端仍作保险：长容器 rect 的
    几何中心可能落到字间空隙/容器 padding。窄元素（<28px）回退中心。
    """
    w = r[2] - r[0]
    x = r[0] + 14 if w > 28 else (r[0] + r[2]) // 2
    return x, (r[1] + r[3]) // 2


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

_READY_RE = re.compile(r'申请号\s*[/／]\s*专利号\s*[:：]\s*\d')


def _detail_ready(texts: list) -> bool:
    """就绪信号：顶栏「申请号/专利号：」已填值（壳页恒空）。

    结果列表卡片也有「申请号/专利号：13位」——见「共查询到」一律不算详情
    （否则会在结果页上 wait_doc/harvest 死滚）。
    """
    joined = '\n'.join(t or '' for t in texts)
    if '共查询到' in joined:
        return False
    if '国内案件信息详情' in joined:
        return True
    return any(_READY_RE.search(t or '') for t in texts)


def _wait_detail_ready(win, timeout_s: float = 40.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if _detail_ready(uia.all_texts(win)):
            return {'ready': True, 'waited_s': round(time.time() - t0, 1)}
        time.sleep(1.0)
    return {'ready': False, 'waited_s': round(time.time() - t0, 1)}


def _tab_count(win) -> int:
    try:
        return len(win.descendants(control_type='TabItem'))
    except Exception:
        return -1


def _nav_drill(win, case_no: str, prefer_branch: str, step,
               budget_s: float = 45.0, out_dir: str | None = None) -> bool:
    """专利详情页页内左导航下钻到案件项（分组→分支→案件，逐步重枚举）。

    案件项：只点一次，右栏 PDF 慢载时只等不重连点；就绪由 _wait_pdf_viewer_ready。
    右栏已就绪且左树已见本案号 → 不再点案件（慢载转完后常见）。
    """
    from run_cnipa_fetch_decision_file_uia import (  # noqa: E402
        _wait_pdf_viewer_ready, _pdf_viewer_visible_light,
    )

    nav_probe_dir = out_dir or os.path.join(
        tempfile.gettempdir(), 'cnipa_nav_probe')
    os.makedirs(nav_probe_dir, exist_ok=True)
    t0 = time.time()
    clicks, last, stale = 0, None, 0
    case_clicked = False
    nc = _norm_case_no(case_no)

    def _wait_viewer_after_case() -> bool:
        remain = max(8.0, budget_s - (time.time() - t0))
        return _wait_pdf_viewer_ready(
            win, nav_probe_dir, step, timeout_s=min(40.0, remain))

    def _case_already_ready() -> bool:
        if not nc:
            return False
        try:
            if not _pdf_viewer_visible_light(win):
                return False
        except Exception:
            return False
        joined = _norm_case_no(' '.join(uia.all_texts(win) or []))
        return nc in joined

    while time.time() - t0 < budget_s:
        if _case_already_ready():
            step('nav_drill', skipped=True, why='case_and_pdf_already_ready',
                 clicks=clicks)
            return True
        act = next_nav_action(ordered_texts(win), case_no, prefer_branch)
        if act is None:
            # 刷新后左树慢展开：多等几轮再放弃
            if time.time() - t0 < min(20.0, budget_s * 0.6):
                time.sleep(1.5)
                continue
            step('nav_drill', warn='树中无可点导航目标（收起且分组不可见？）',
                 clicks=clicks)
            return case_clicked
        kind, text, r = act
        if kind == 'case' and _case_already_ready():
            step('nav_drill', skipped=True, why='pdf_ready_skip_case_click',
                 case=text)
            return True
        if (kind, text) == last:
            stale += 1
            if kind == 'case' and case_clicked:
                step('nav_drill', wait='pdf_viewer', stale=stale, case=text)
                if _wait_viewer_after_case():
                    return True
                if stale >= 10:
                    step('nav_drill', warn='案件 PDF 长时间未就绪', clicks=clicks)
                    return True
                time.sleep(2.5)
                continue
            if stale >= 3:
                step('nav_drill', warn=f'重复点击 {text} 无推进，止步', clicks=clicks)
                return case_clicked
            time.sleep(2.0)
        x, y = _center(r)
        uia.human_click(win, x, y, settle=0.3)   # 页内导航点击也走拟人轨迹
        clicks += 1
        last, stale = (kind, text), 0
        step('nav_drill', clicked=f'{kind}:{text}', at=(x, y), clicks=clicks)
        if kind == 'case':
            case_clicked = True
            _wait_viewer_after_case()
            return True
        time.sleep(1.3)              # 菜单展开异步
    step('nav_drill', warn=f'超时 {budget_s}s', clicks=clicks)
    return case_clicked


def _wait_doc_ready(win, out_dir: str, step, budget_s: float = 15.0) -> dict:
    """右栏决定书就绪探测。

    优先：当前视口 a11y / 一帧 OCR（不滚）。未命中再顶/+6/+12 三位置；
    命中后若滚过才回顶。避免一进详情就「下滚再上滚」空转。

    PDF 载入失败 / 右栏空壳：立即返回，禁止滚探（滚无法修「载入 PDF 时发生错误」）。
    """
    try:
        from run_cnipa_fetch_decision_file_uia import (
            _pdf_viewer_visible_light, _pdf_load_failed, _detail_shell_empty,
        )
        if _pdf_load_failed(win):
            return {'ready': False, 'waited_s': 0, 'last_err': 'pdf_load_error',
                    'pos': -1, 'need_refresh': True}
        if _pdf_viewer_visible_light(win):
            return {'ready': True, 'marker': 'pdf_viewer', 'pos': 0,
                    'waited_s': 0, 'via': 'pdf_toolbar'}
        if _detail_shell_empty(win):
            return {'ready': False, 'waited_s': 0, 'last_err': 'shell_empty',
                    'pos': -1, 'need_refresh': True}
    except Exception:
        pass
    if _on_results(win):
        return {'ready': False, 'waited_s': 0, 'last_err': '仍在结果页，跳过 doc 滚探',
                'pos': -1}
    t0 = time.time()
    joined = ' '.join(uia.all_texts(win) or [])
    for m in DOC_MARKERS:
        if m in joined:
            return {'ready': True, 'marker': m, 'pos': 0,
                    'waited_s': round(time.time() - t0, 1), 'via': 'a11y'}
    probe = os.path.join(out_dir, 'probe_doc.png')
    try:
        win.set_focus()
        uia.screenshot_window(win, probe)
        m0 = doc_marker(co.ocr_image(probe))
        if m0:
            return {'ready': True, 'marker': m0, 'pos': 0,
                    'waited_s': round(time.time() - t0, 1), 'via': 'ocr_now'}
    except Exception:
        pass
    # 滚探前再查一次：慢载期间可能刚出现红条/空壳
    try:
        from run_cnipa_fetch_decision_file_uia import (
            _pdf_load_failed, _detail_shell_empty,
        )
        if _pdf_load_failed(win) or _detail_shell_empty(win):
            return {'ready': False, 'waited_s': round(time.time() - t0, 1),
                    'last_err': 'pdf_load_error_or_empty', 'pos': -1,
                    'need_refresh': True}
    except Exception:
        pass
    last_err = '三位置均未见决定书锚词'
    rounds = 0
    while time.time() - t0 < budget_s and rounds < 2:
        rounds += 1
        if _on_results(win):
            return {'ready': False, 'waited_s': round(time.time() - t0, 1),
                    'last_err': '滚探中回到结果页', 'pos': -1}
        x, y = _pane_xy(win)
        uia.human_click(win, x, y, settle=0.15)
        uia.scroll_up(win, x, y, notches=8)
        for k in range(3):
            if k:
                uia.scroll_down(win, x, y, notches=6)
                time.sleep(0.6)
            try:
                win.set_focus()
                time.sleep(0.15)
                uia.screenshot_window(win, probe)
                m = doc_marker(co.ocr_image(probe))
            except Exception as e:
                last_err = str(e)[:80]
                continue
            if m:
                if k:
                    sx, sy = _pane_xy(win)
                    uia.scroll_up(win, sx, sy, notches=8 + 6 * k)
                return {'ready': True, 'marker': m, 'pos': k,
                        'waited_s': round(time.time() - t0, 1), 'via': 'ocr_scroll'}
        time.sleep(0.5)
    return {'ready': False, 'waited_s': round(time.time() - t0, 1),
            'last_err': last_err, 'pos': 3, 'rounds': rounds}


RESULT_MARKERS = ('共查询到', '重置', '展开更多搜索项')


def _on_results(win) -> bool:
    """前台是否检索结果页（三页共用窗口标题，只能靠页面文本判）。"""
    joined = ' '.join(uia.all_texts(win))
    return any(m in joined for m in RESULT_MARKERS)


def _looks_like_detail_front(win) -> bool:
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    if _on_results(win):
        return False
    return (
        _detail_ready(texts)
        or _classify_state(texts).startswith('详情页')
        or '国内案件信息详情' in joined
        or any(m in joined for m in DOC_MARKERS)
        or '申请号/专利号' in joined
    )


def _activate_front_detail_tab(win, step, case_no: str = '') -> bool:
    """点卡片后前台常仍停在结果列表：从右往左试 cpquery Tab，切到详情页。

    新开详情多在条带最右；不切过去就误判失败 → worker 立刻点下一条。
    """
    if _looks_like_detail_front(win):
        step('front_detail_tab', ok=True, via='already')
        return True
    key = _norm_case_no(case_no)
    tabs = uia.find_cpquery_strip_tabs(win) or []
    # 最右优先（新开详情）
    order = list(reversed(list(tabs)))
    for i, t in enumerate(order):
        try:
            name = (uia._name(t) or '')[:40]
            uia.click(win, t, settle=0.55)
            time.sleep(0.7)
        except Exception as e:
            step('front_detail_tab', warn=str(e)[:60], i=i)
            continue
        if _on_results(win):
            continue
        texts = uia.all_texts(win) or []
        joined = ' '.join(texts)
        if key and key in _norm_case_no(joined):
            step('front_detail_tab', ok=True, via='case_match', tab=name)
            return True
        if _looks_like_detail_front(win):
            step('front_detail_tab', ok=True, via='detail_ui', tab=name)
            return True
    ok = _looks_like_detail_front(win)
    step('front_detail_tab', ok=ok, tried=len(order))
    return ok


def _norm_case_no(s: str) -> str:
    return re.sub(r'[^0-9A-Za-z]', '', s or '').upper()


def _detail_incomplete_shell(win, case_no: str) -> bool:
    """详情壳在但左树无本案案件号，或右栏决定书未出（点分组无效，需 F5）。

    case_no 来自当前任务（anjianbh/list_item），不写死具体案号。
    """
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    if _on_results(win) or not (_detail_ready(texts) or '国内案件信息详情' in joined):
        return False
    try:
        from run_cnipa_fetch_decision_file_uia import (  # noqa: WPS433
            _detail_shell_empty, _pdf_load_failed, _pdf_viewer_visible_light,
        )
        if _pdf_load_failed(win) or _detail_shell_empty(win):
            return True
        if _pdf_viewer_visible_light(win):
            return False
    except Exception:
        pass
    nc = _norm_case_no(case_no)
    if not nc:
        return False
    # 左树/页内未见本案号 → 半壳（截图1）
    if nc not in _norm_case_no(joined):
        return True
    # 案号已在：右栏已有决定书锚词 → 完整；否则仍半壳
    return not any(k in joined for k in (
        '国家知识产权局', '发文日', '一、案由', '决定要点'))


DETAIL_OPEN_TIMEOUT_S = 20.0


def _detail_usable(win, case_no: str = '') -> bool:
    """详情可继续：右栏 PDF/决定书已出，或左树已见决定分支+本案号（可下钻）。

    仅有「国内案件信息详情」壳 + 转圈/空右栏 → False（勿因「网核案件信息」假就绪）。
    """
    if _on_results(win):
        return False
    texts = uia.all_texts(win) or []
    joined = ' '.join(texts)
    if '共查询到' in joined:
        return False
    if not ('国内案件信息详情' in joined or _detail_ready(texts)):
        return False
    try:
        from run_cnipa_fetch_decision_file_uia import (  # noqa: WPS433
            _pdf_load_failed, _pdf_viewer_visible_light,
        )
        if _pdf_load_failed(win):
            return False
        if _pdf_viewer_visible_light(win):
            return True
    except Exception:
        if any(m in joined for m in DOC_MARKERS):
            return True
    # 右栏未出：仅左树已见决定分支+本案号才可下钻；纯转圈壳（仅网核案件信息）不算
    nc = _norm_case_no(case_no)
    has_branch = any(k in joined for k in ('复审审查决定', '无效审查决定'))
    if has_branch and (not nc or nc in _norm_case_no(joined)):
        return True
    return False


def _open_detail_from_card_click(win, step, r, case_no: str = '') -> bool:
    """结果页点卡片；未开新 Tab 且仍停结果页时 Ctrl 开标签，再切详情 Tab。

    同页 SPA 已渲染可用详情 → True；同页空壳 → Ctrl 开新标签，仍失败则 False。
    """
    x, y = _link_point(r)
    tabs_before = _tab_count(win)
    uia.human_click(win, x, y)
    time.sleep(1.2)
    t1 = _tab_count(win)
    time.sleep(0.8)
    tabs_after = max(t1, _tab_count(win))
    new_tab = tabs_after > tabs_before

    def _front_ok() -> bool:
        if _on_results(win):
            return _activate_front_detail_tab(win, step, case_no=case_no)
        # 已不在结果页：须有可用内容，空壳不算成功（交给调用方重试）
        if _detail_usable(win, case_no):
            return True
        texts = uia.all_texts(win) or []
        return bool(
            _detail_ready(texts)
            or _classify_state(texts).startswith('详情页')
            or any(m in ' '.join(texts) for m in DOC_MARKERS))

    if new_tab or _on_results(win):
        if _on_results(win) and not new_tab:
            step('click_link_retry', why='reopen_ctrl')
            uia.human_ctrl_click(win, x, y)
            time.sleep(1.5)
            t1 = _tab_count(win)
            time.sleep(0.8)
            tabs_after = max(t1, _tab_count(win))
            step('click_link_ctrl', reopen=True, tabs_before=tabs_before,
                 tabs_after=tabs_after, new_tab=tabs_after > tabs_before)
        return _front_ok()

    # Tab 数未增且已离开结果页：同页 SPA
    if _front_ok():
        step('click_link_same_tab', ok=True, why='spa_detail_ok')
        return True
    step('click_link_retry', why='spa_shell_ctrl')
    uia.human_ctrl_click(win, x, y)
    time.sleep(1.5)
    t1 = _tab_count(win)
    time.sleep(0.8)
    tabs_after = max(t1, _tab_count(win))
    step('click_link_ctrl', reopen=True, spa_shell=True,
         tabs_before=tabs_before, tabs_after=tabs_after,
         new_tab=tabs_after > tabs_before)
    return _front_ok()


def _wait_detail_open(win, step, case_no: str = '',
                      reopen=None, max_recover: int = 1) -> dict:
    """等详情可用：单轮 20s 未进入 → F5；仍失败则关 Tab 并 reopen() 重点。

    停在结果页时不重置 20s、不 F5；无 reopen 时直接失败（避免无出口空转）。
    """
    refreshed = reopened = 0
    total_wait = 0.0
    while True:
        if _on_results(win):
            _activate_front_detail_tab(win, step, case_no=case_no)
        t0 = time.time()
        while time.time() - t0 < DETAIL_OPEN_TIMEOUT_S:
            if _on_results(win):
                _activate_front_detail_tab(win, step, case_no=case_no)
                time.sleep(0.8)
                continue
            if _detail_usable(win, case_no):
                w = round(time.time() - t0, 1)
                return {
                    'ready': True,
                    'waited_s': round(total_wait + w, 1),
                    'refreshed': refreshed,
                    'reopened': reopened,
                }
            time.sleep(1.0)
        total_wait += DETAIL_OPEN_TIMEOUT_S
        on_results = _on_results(win)
        step('wait_detail', phase='stuck',
             waited_s=DETAIL_OPEN_TIMEOUT_S,
             refreshed=refreshed, reopened=reopened, on_results=on_results)
        if refreshed == 0 and not on_results:
            _browser_refresh_detail(win, step, '详情20s仍转圈或未出内容')
            refreshed += 1
            continue
        if reopened < max_recover and reopen is not None:
            step('detail_stuck', action='close_tab_reclick')
            _cleanup_tab(win, step)
            reopened += 1
            time.sleep(0.8)
            if reopen():
                if _on_results(win):
                    _activate_front_detail_tab(win, step, case_no=case_no)
                continue
            step('detail_stuck', warn='关 Tab 后未能从结果页重点卡片')
            break
        if reopen is None:
            step('detail_stuck', warn='无 reopen 回调，停止自愈')
        break
    return {
        'ready': False,
        'waited_s': round(total_wait, 1),
        'refreshed': refreshed,
        'reopened': reopened,
    }


def _browser_refresh_detail(win, step, why: str = '') -> None:
    uia.dismiss_chrome_translate_offer(win, step)
    step('detail_refresh', why=why or '详情未完整加载')
    try:
        win.set_focus()
    except Exception:
        pass
    uia.safe_send_keys('^r', win=win, pause=0.05, log=step)
    time.sleep(4.5)


def _close_gate(state: str, closed: int, limit: int = 3) -> bool:
    """继续关标签：详情页或空白加载页；结果页/其它站点停手。"""
    return (state.startswith('详情页') or state.startswith('空白页')) and closed < limit


def _cleanup_tab(win, step) -> bool:
    """还原现场：Alt+← 回结果页；否则 Ctrl+W 关详情/空白页（≤3）。"""
    if _on_results(win):
        step('cleanup', ok=True, state='already_results')
        return True
    st0 = _classify_state(uia.all_texts(win))
    # 空白详情页无 history：Alt+← 无效，直接关标签
    if st0.startswith('空白页'):
        closed = 0
        while _close_gate(_classify_state(uia.all_texts(win)), closed):
            win.set_focus()
            uia.safe_send_keys('^w', win=win, pause=0.8, log=step)
            time.sleep(1.2)
            closed += 1
            if _on_results(win):
                step('cleanup', ok=True, via='ctrl_w_blank', closes=closed)
                return True
        step('cleanup', ok=False, closed=closed, tabs=_tab_count(win),
             warn='空白详情标签未能关回结果页')
        return False
    for i in range(2):                          # 同标签 SPA 跳转：后退回结果页
        win.set_focus()
        uia.safe_send_keys('%{LEFT}', win=win, pause=0.6, log=step)
        time.sleep(1.2)
        if _on_results(win):
            step('cleanup', ok=True, via='alt_left', backs=i + 1)
            return True
    closed = 0                                  # 新标签形态：逐枚关详情标签
    while _close_gate(_classify_state(uia.all_texts(win)), closed):
        win.set_focus()
        uia.safe_send_keys('^w', win=win, pause=0.8, log=step)
        time.sleep(1.2)
        closed += 1
        if _on_results(win):
            step('cleanup', ok=True, via='ctrl_w', closes=closed)
            return True
    step('cleanup', ok=False, closed=closed, tabs=_tab_count(win),
         warn='前台未还原为结果页（结果页标签可能已不在；请先跑 03 重建）')
    return False


def _stop_harvest(i: int, need: list, no_grow: int, panel_ok: bool) -> bool:
    """停屏规则（2026-09-01 19:40 用户对拍修正）：三要素齐备**且正文连续
    ≥2 屏不再增长**（合议组名单齐用 2、不齐用 3，防末页竖排名单漏收）才停。
    旧规则 panel_ok 可单独触发停机——19:34 现场签名栏在 shot3 尾部出现而
    决定书第 2 页正文还有一屏未读；叠加 4 屏预算被壳区空转烧掉 2 屏，
    造成第 2 页底部 4 行正文截断（"…而否定涉案专利的创造性"未收到）。"""
    return i >= 1 and not need and no_grow >= (2 if panel_ok else 3)


def _pdf_ocr_harvest_kwargs(card_fields: dict, texts: list) -> dict | None:
    """卡片+a11y 已凑齐 OCR 四要素则不必扫 PDF；否则限页/限时读 PDF。"""
    a11y = map_detail(parse_detail_texts(texts))
    pre = merge_detail(card_fields or {}, a11y)
    need = [k for k in ('request_date', 'legal_basis', 'decision_summary', 'panel_members')
            if not (pre.get(k) or '').strip()]
    pm = (pre.get('panel_members') or '').strip()
    if pm and not co.panel_members_trustworthy(pm):
        if 'panel_members' not in need:
            need.append('panel_members')
    if not need:
        return None
    heavy = any(k in need for k in ('decision_summary', 'panel_members'))
    return {'max_pages': 8 if heavy else 4, 'budget_s': 55.0}


def _harvest_pdf_page_idxs(n_all: int, *, tail_only: bool = False,
                           head_and_tail: bool = False,
                           max_pages: int = 16) -> list:
    """要 OCR 的 0-based 页号。

    head_and_tail：只要第1、2页 + 末页（字段表+合议组）；勿扫中间页。
    """
    n = max(0, int(n_all or 0))
    if n <= 0:
        return []
    if tail_only:
        return list(range(max(0, n - 2), n))
    if head_and_tail:
        idxs = [0]
        if n >= 2:
            idxs.append(1)
        if n >= 3:
            idxs.append(n - 1)
        return idxs
    return list(range(min(n, max_pages)))


def _harvest_pdf(pdf_path: str, out_dir: str, step, dpi: int = 150,
                 max_pages: int = 16, budget_s: float = 150.0,
                 tail_only: bool = False,
                 head_and_tail: bool = False) -> tuple:
    """已存决定书 PDF → 渲染 + OCR（不截浏览器）。

    tail_only：只扫末 1～2 页合议组。
    head_and_tail：只扫第1、2页 + 末页（老决定字段够用，少打 MinerU）。
    """
    import fitz
    shots, pages = [], []
    scanned = set()
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    prev_sum_len, no_grow = -1, 0
    t0 = time.time()
    ocr_fail = 0
    try:
        n_all = int(doc.page_count or 0)
        idxs = _harvest_pdf_page_idxs(
            n_all, tail_only=tail_only, head_and_tail=head_and_tail,
            max_pages=max_pages)
        step('harvest_pdf', plan=idxs, n_all=n_all,
             mode=('tail' if tail_only else
                   ('head_tail' if head_and_tail else 'prefix')))
        for i in idxs:
            if time.time() - t0 >= budget_s:
                step('harvest_pdf', warn='超时停止', pages=len(pages),
                     budget_s=budget_s)
                break
            path = os.path.join(out_dir, f'pdf_page{i + 1}.png')
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            pix.save(path)
            shots.append(path)
            scanned.add(i)
            try:
                items = sorted(co.ocr_image(path) + co.hline_items(path),
                               key=lambda it: (it[1][1], it[1][0]))
                pages.append(items)
                ocr_fail = 0
                with open(os.path.join(out_dir, f'pdf_page{i + 1}_ocr.txt'), 'w',
                          encoding='utf-8') as f:
                    for tx, bx in items:
                        f.write(f'L|{bx[0]},{bx[1]},{bx[2]},{bx[3]}|{tx}\n')
            except Exception as e:
                ocr_fail += 1
                step('pdf_page', n=i + 1, warn=f'OCR 失败: {e}')
                pages.append([])
                if ocr_fail >= 3:
                    step('harvest_pdf', warn='连续 OCR 失败，停止', fail=ocr_fail)
                    break
                continue
            parsed = co.parse_detail_pages(pages)
            need = [k for k in ('request_date', 'legal_basis', 'decision_summary')
                    if not parsed.get(k)]
            sum_len = len(parsed.get('decision_summary') or '')
            grown = sum_len > prev_sum_len
            no_grow = 0 if grown else no_grow + 1
            prev_sum_len = sum_len
            pm = parsed.get('panel_members') or ''
            panel_ok = co.panel_members_trustworthy(pm)
            if not panel_ok:
                need = list(need) + ['panel_members']
            step('pdf_page', n=i + 1, need=need or '无', summary_len=sum_len,
                 panel=pm[:24] or False, no_grow=no_grow)
            # head_and_tail 固定扫选定页，不中途停（末页还要合议组）
            if ((not tail_only) and (not head_and_tail)
                    and _stop_harvest(i, need, no_grow, panel_ok)):
                break
        # 非 head_and_tail：合议组未齐时再补扫末 1～2 页
        pm_now = (co.parse_detail_pages(pages).get('panel_members') or '')
        if ((not tail_only) and (not head_and_tail)
                and not co.panel_members_trustworthy(pm_now)
                and time.time() - t0 < budget_s):
            n_all = int(doc.page_count or 0)
            for ti in range(max(0, n_all - 2), n_all):
                if ti in scanned or time.time() - t0 >= budget_s:
                    continue
                path = os.path.join(out_dir, f'pdf_page{ti + 1}.png')
                pix = doc.load_page(ti).get_pixmap(matrix=mat, alpha=False)
                pix.save(path)
                shots.append(path)
                scanned.add(ti)
                try:
                    items = sorted(co.ocr_image(path) + co.hline_items(path),
                                   key=lambda it: (it[1][1], it[1][0]))
                    pages.append(items)
                    with open(os.path.join(out_dir, f'pdf_page{ti + 1}_ocr.txt'),
                              'w', encoding='utf-8') as f:
                        for tx, bx in items:
                            f.write(f'L|{bx[0]},{bx[1]},{bx[2]},{bx[3]}|{tx}\n')
                except Exception as e:
                    step('pdf_page', n=ti + 1, warn=f'尾页 OCR 失败: {e}')
                    pages.append([])
                    continue
                pm_t = (co.parse_detail_pages(pages).get('panel_members') or '')
                step('pdf_page', n=ti + 1, tail=True, panel=pm_t[:24] or False)
                if co.panel_members_trustworthy(pm_t):
                    break
    finally:
        doc.close()
    step('harvest_pdf', pages=len(pages), file=os.path.basename(pdf_path),
         elapsed_s=round(time.time() - t0, 1))
    return shots, pages


def detail_from_saved_pdf(pdf_path: str, list_item: dict, application_no: str,
                          case_no: str, library_type: str = 'invalid',
                          out_dir: str = '', step=None) -> dict:
    """盘上已有决定书 PDF：不打开详情页，渲染 OCR 取字段（与线上 harvest_pdf 同路）。

    返回 detail dict（含 decision_file_url）；失败时仍尽量回填列表字段 + PDF URL。
    """
    def _step(name, **kw):
        if step:
            step(name, **kw)

    app = cnipa_contract.dotted_appno(application_no) or str(application_no or '').strip()
    case = str(case_no or '').strip()
    item = list_item if isinstance(list_item, dict) else {}
    # 列表卡片键 → merge_detail 可用的 card 形态
    card = {
        'case_no': item.get('case_no') or case,
        'decision_no': item.get('decision_no') or '',
        'title': item.get('invention_title') or item.get('title') or '',
        'invalid_requester': item.get('petitioner') or item.get('invalid_requester') or '',
        'patentee': item.get('patentee') or '',
        'app_no': app,
        'issue_date': item.get('issue_date') or item.get('decision_date') or '',
        'conclusion': item.get('decision_result') or item.get('conclusion') or '',
    }
    ocr_detail = {}
    if pdf_path and os.path.isfile(pdf_path):
        work = out_dir or tempfile.mkdtemp(prefix='cnipa_pdf_ocr_')
        try:
            os.makedirs(work, exist_ok=True)
            _shots, pages = _harvest_pdf(
                pdf_path, work, _step, dpi=200, budget_s=120.0,
                head_and_tail=True)
            if pages:
                ocr_detail = co.parse_detail_pages(pages) or {}
        except Exception as e:
            _step('detail_from_pdf', warn=str(e)[:120])
    detail = merge_detail(card, ocr_detail)
    detail['application_no'] = (
        cnipa_contract.dotted_appno(detail.get('application_no') or app) or app)
    if case and not (detail.get('case_no') or '').strip():
        detail['case_no'] = case
    detail['decision_file_url'] = cnipa_contract.build_decision_file_url(app, case)
    # 写持久缓存，下次直接命中
    if app and case:
        try:
            payload = {'ok': True, 'detail': detail, 'library_radio': library_type}
            cnipa_contract.write_detail_cache(library_type, app, case, payload)
        except Exception as e:
            _step('detail_from_pdf_cache', warn=str(e)[:80])
    return detail


def _harvest(win, out_dir: str, step, max_shots: int = 8,
             scroll_xy: tuple | None = None, until_page: int | None = None,
             fixed_shots: bool = False):
    """滚动收割 N 屏 OCR。max_shots=8：视口起点可能在详情壳区（前 1-2 屏
    无文档内容，2026-09-01 19:34 实测 shot1/2 空转），预算须覆盖壳区+整文。
    scroll_xy：新决定 PDF 阅读器内滚轮中心（默认 _pane_xy）。"""
    shots, pages = [], []
    prev_sum_len, no_grow = -1, 0
    page_mode = until_page is not None
    scroll_notches = (
        NEW_DECISION_VIEWER_SCROLL_NOTCHES
        if (page_mode or fixed_shots) else 6)
    for i in range(max_shots):
        if (page_mode or fixed_shots) and i == 0:
            try:
                from run_cnipa_fetch_decision_file_uia import (
                    _ensure_pdf_viewer_at_top, _pdf_viewer_scroll_at_top,
                )
                _ensure_pdf_viewer_at_top(win, out_dir, step)
                if not _pdf_viewer_scroll_at_top(win, out_dir, step):
                    step('harvest', warn='not_at_top_before_shot1')
            except Exception as e:
                step('harvest', warn=f'pre_harvest_top: {e}')
        uia.dismiss_chrome_restore_bubble(win)
        win.set_focus()                       # 防串窗：必须先聚焦再截
        time.sleep(0.2)
        path = os.path.join(out_dir, f'shot{i + 1}.png')
        uia.screenshot_window(win, path)
        shots.append(path)
        try:
            # 横线伪项随页入册；按 (y,x) 归并成阅读序——续读循环依序判定，
            # 若横线缀在末尾，线下方的段题会先被遍历（结构规则沦为摆设）
            items = sorted(co.ocr_image(path) + co.hline_items(path),
                           key=lambda it: (it[1][1], it[1][0]))
            pages.append(items)
            with open(os.path.join(out_dir, f'shot{i + 1}_ocr.txt'), 'w',
                      encoding='utf-8') as f:
                for tx, bx in items:
                    f.write(f'L|{bx[0]},{bx[1]},{bx[2]},{bx[3]}|{tx}\n')
        except Exception as e:
            # 229：首屏 OCR 异常曾直接 break → 只翻一页就停，字段全空
            step('shot', n=i + 1, warn=f'OCR 失败（续滚）: {e}')
            pages.append([])
            need = ['request_date', 'legal_basis', 'decision_summary']
            sum_len = prev_sum_len if prev_sum_len >= 0 else 0
            no_grow = no_grow + 1
            step('shot', n=i + 1, need=need, summary_len=sum_len,
                 panel=False, no_grow=no_grow)
            if i < max_shots - 1:
                sx, sy = scroll_xy or _pane_xy(win)
                uia.human_click(win, sx, sy, settle=0.15)
                uia.scroll_down(win, sx, sy, notches=scroll_notches)
                time.sleep(0.9)
            continue
        parsed = co.parse_detail_pages(pages)
        need = [k for k in ('request_date', 'legal_basis', 'decision_summary')
                if not parsed.get(k)]
        sum_len = len(parsed.get('decision_summary') or '')
        grown = sum_len > prev_sum_len
        no_grow = 0 if grown else no_grow + 1
        prev_sum_len = sum_len
        pm = parsed.get('panel_members') or ''
        panel_ok = co.panel_complete(pm)         # 首页签名栏可能被裁/误读 →
        step('shot', n=i + 1, need=need or '无', summary_len=sum_len,  # 续滚找末页
             panel=pm[:24] or False, no_grow=no_grow,
             fixed=fixed_shots)
        if page_mode:
            try:
                from run_cnipa_fetch_decision_file_uia import _read_pdf_page_combined
                pg_toolbar = _read_pdf_page_combined(
                    win, out_dir, step=step, shot_png=path)
                step('shot', n=i + 1, toolbar_page=pg_toolbar)
                if pg_toolbar and pg_toolbar[0] >= until_page:
                    step('shot', stop=f'page>={until_page}', page=pg_toolbar[0],
                         total=pg_toolbar[1])
                    break
            except Exception:
                pass
        elif not fixed_shots and _stop_harvest(i, need, no_grow, panel_ok):
            break
        elif (not fixed_shots and not page_mode
              and co.shot_past_summary_table(items)
              and (parsed.get('decision_summary') or parsed.get('request_date')
                   or panel_ok)):
            # 认出表格底边横线/「案由」段题 → 停滚（原先横线只截要点不控鼠标）
            step('shot', stop='hline_or_section', n=i + 1,
                 summary_len=sum_len, panel=bool(panel_ok))
            break
        if i < max_shots - 1:
            sx, sy = scroll_xy or _pane_xy(win)
            uia.human_click(win, sx, sy, settle=0.15)
            uia.scroll_down(win, sx, sy, notches=scroll_notches)
            time.sleep(0.9)
    return shots, pages


def _sig_fp(items: list) -> str:
    """屏指纹：浏览器壳(y<140 标签页/地址栏/默认浏览器条)之下**首个**内容行。
    判触底用——壳区行跨屏恒定，混入会把"滚不动"误判成屏没变。"""
    for tx, bx in items:
        n = co.norm(tx)
        if n and bx[1] >= 140 and not any(h in n.lower() for h in co._JUNK_HINTS):
            return n[:40]
    return ''


def _tail_stop(panel_ok: bool, fp: str, prev_fp: str) -> bool:
    """跳底停机（纯函数）：名单三角色齐→立即停；屏指纹与上一跳相同=已触底
    （名单未见也停，截图已留档供离线分析）。"""
    if panel_ok:
        return True
    return bool(prev_fp) and fp == prev_fp


def _tail_pass(win, out_dir: str, step, pages: list, shots: list,
               max_jumps: int = 12) -> bool:
    """末页落款补收（2026-09-01 20:01 用户指出缺口）：正常停屏多发生在正文
    中段（no_grow 规则管要点收齐，不管读没读到底），而【合议组成员】在末页
    落款处有**第二次独立渲染**（19:34 首页签名行 跷/晓 一字误识的对照源）。
    解析端 panels 择优"同分取后见"，前提是末页候选先进 pages → 大步跳底
    （24 格≈1.2 屏/跳）触底即收。"""
    prev_fp = ''
    for j in range(max_jumps):
        sx, sy = _pane_xy(win)
        if j == 0:
            uia.human_click(win, sx, sy, settle=0.15)
        uia.scroll_down(win, sx, sy, notches=24)
        time.sleep(0.9)
        win.set_focus()
        time.sleep(0.2)
        path = os.path.join(out_dir, f'tail{j + 1}.png')
        uia.screenshot_window(win, path)
        shots.append(path)
        try:
            items = co.ocr_image(path)
        except Exception as e:
            step('tail', n=j + 1, warn=f'OCR 失败（截图已留档）: {e}')
            return False
        # 指纹必须用纯文本行先算：横线伪项文本恒为 __HL__，两屏同线会让
        # 指纹相等而误判触底（20:57）
        fp = _sig_fp(items)
        items = sorted(items + co.hline_items(path),   # (y,x) 阅读序归并（同上）
                       key=lambda it: (it[1][1], it[1][0]))
        pages.append(items)
        with open(os.path.join(out_dir, f'tail{j + 1}_ocr.txt'), 'w',
                  encoding='utf-8') as f:
            for tx, bx in items:
                f.write(f'L|{bx[0]},{bx[1]},{bx[2]},{bx[3]}|{tx}\n')
        pm = co.parse_detail_pages([items]).get('panel_members') or ''
        panel_ok = co.panel_complete(pm)
        step('tail', n=j + 1, fp=fp[:24] or False, panel=pm[:24] or False)
        if _tail_stop(panel_ok, fp, prev_fp):
            if panel_ok:
                return True
            # 触底未见名单：可能名单恰在底缘上一屏 → 回滚 4 格补一屏再试
            uia.scroll_up(win, *_pane_xy(win), notches=4)
            time.sleep(0.8)
            win.set_focus()
            time.sleep(0.2)
            path2 = os.path.join(out_dir, f'tail{j + 1}b.png')
            uia.screenshot_window(win, path2)
            shots.append(path2)
            try:
                items2 = co.ocr_image(path2)
                pages.append(items2)
                with open(os.path.join(out_dir, f'tail{j + 1}b_ocr.txt'), 'w',
                          encoding='utf-8') as f:
                    for tx, bx in items2:
                        f.write(f'L|{bx[0]},{bx[1]},{bx[2]},{bx[3]}|{tx}\n')
                pm2 = co.parse_detail_pages([items2]).get('panel_members') or ''
                step('tail', n=f'{j + 1}b', panel=pm2[:24] or False)
                return co.panel_complete(pm2)
            except Exception as e:
                step('tail', n=f'{j + 1}b', warn=f'OCR 失败: {e}')
                return False
        prev_fp = fp
    step('tail', warn=f'{max_jumps} 跳未触底（超长文档？），末页名单未收')
    return False


def translate_inputs(args: dict) -> dict:
    """技能侧契约 §3.1 入参转译（纯函数，--selftest 覆盖）。

    兼容旧键 anjianbh/zhuanlisqh/card_fields；新键 library_type /
    application_no / case_no / detail_url / list_item（search item 原样）。
    list_item 卡片精确值只在 card_fields 同名旧键为空时补位（§3.1：
    卡片值优先于 OCR）。返回 {lib, detail_url_arg, anjianbh, zhuanlisqh,
    card_fields}。
    """
    anjianbh = str(args.get('anjianbh') or '').strip()
    zhuanlisqh = str(args.get('zhuanlisqh') or '').strip()
    card_fields = dict(args.get('card_fields') or {})
    lib = str(args.get('library_type') or '').strip().lower()
    detail_url_arg = str(args.get('detail_url') or '').strip()
    list_item = args.get('list_item') if isinstance(args.get('list_item'), dict) else {}
    if list_item:
        rt = list_item.get('review_type')
        if not lib and rt in (1, 2):
            lib = 'reexam' if int(rt) == 1 else 'invalid'
        _NEWS = {'case_no': 'case_no', 'decision_no': 'decision_no',
                 'invention_title': 'title', 'patentee': 'patentee',
                 'application_no': 'app_no', 'app_no': 'app_no'}
        for newk, oldk in _NEWS.items():
            v = str(list_item.get(newk) or '').strip()
            if v and not str(card_fields.get(oldk) or '').strip():
                card_fields[oldk] = cnipa_contract.undotted_appno(v) \
                    if oldk == 'app_no' else v
        pet = str(list_item.get('petitioner') or '').strip()
        if pet:
            oldk = 'reexam_requester' if lib == 'reexam' else 'invalid_requester'
            if not str(card_fields.get(oldk) or '').strip():
                card_fields[oldk] = pet
        res = str(list_item.get('decision_result') or '').strip()
        if res and not str(card_fields.get('conclusion') or '').strip():
            card_fields['conclusion'] = res
        iss = str(list_item.get('issue_date') or '').strip()
        if iss and not str(card_fields.get('issue_date') or '').strip():
            card_fields['issue_date'] = iss
    if not anjianbh:
        anjianbh = str(args.get('case_no') or '').strip() \
            or str(list_item.get('case_no') or '').strip() \
            or str(card_fields.get('case_no') or '').strip()
    if not zhuanlisqh:
        zhuanlisqh = str(args.get('application_no') or '').strip() \
            or str(list_item.get('application_no') or '').strip() \
            or str(card_fields.get('app_no') or '').strip()
        if zhuanlisqh:
            zhuanlisqh = _norm_cpquery_appno(zhuanlisqh)   # 去 CN/ZL/空格，留点
    if detail_url_arg:   # 仅作对拍/取证；离站直入必失效（docstring §进页方式）
        m = re.search(r'anjianbh=([^&]+)', detail_url_arg)
        if m and not anjianbh:
            anjianbh = m.group(1).strip()
        m = re.search(r'zhuanlisqh=([^&]+)', detail_url_arg)
        if m and not zhuanlisqh:
            zhuanlisqh = m.group(1).strip()
    return {'lib': lib or 'invalid', 'detail_url_arg': detail_url_arg,
            'anjianbh': anjianbh, 'zhuanlisqh': zhuanlisqh,
            'card_fields': card_fields}


def main(argv: list) -> int:
    t0 = time.time()
    if len(argv) >= 2 and argv[1] == '--selftest':
        return selftest()
    if len(argv) < 2:
        print(__doc__)
        return 2
    with open(argv[1], 'r', encoding='utf-8-sig') as f:
        args = json.load(f)

    ts = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(ROOT, 'output', 'uia_runs', f'detail_{ts}')
    os.makedirs(out_dir, exist_ok=True)
    payload: dict = {'mode': 'uia_click_ocr', 'run_dir': out_dir,
                     'input_file': argv[1],
                     'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'steps': []}
    win_holder: list = [None]
    run_finished = [False]

    def step(name: str, **kw) -> None:
        kw['step'] = name
        payload['steps'].append(kw)
        print(f"[detail] {name}: {kw}")
        if name in _DEBUG_SNAPSHOT_STEPS or len(payload['steps']) % 20 == 0:
            try:
                _write_debug_snapshot(payload, out_dir)
            except OSError:
                pass

    import atexit

    def _emergency_teardown() -> None:
        if run_finished[0]:
            return
        if os.path.isfile(os.path.join(out_dir, 'payload.json')):
            return
        payload.setdefault('ok', False)
        payload.setdefault('error', '详情 runner 未正常结束（超时或进程被终止）')
        payload['elapsed_s'] = round(time.time() - t0, 1)
        try:
            _write_debug_snapshot(payload, out_dir)
        except OSError:
            pass
        w = win_holder[0]
        if w and not args.get('keep_tab'):
            try:
                _cleanup_tab(w, step)
            except Exception:
                pass

    atexit.register(_emergency_teardown)

    # —— 技能侧契约 §3.1 新入参转译（纯函数 translate_inputs）——
    tin = translate_inputs(args)
    anjianbh, zhuanlisqh = tin['anjianbh'], tin['zhuanlisqh']
    card_fields = tin['card_fields']
    lib, detail_url_arg = tin['lib'], tin['detail_url_arg']
    payload['library_radio'] = lib
    payload['detail_url_arg'] = detail_url_arg

    # —— 本地缓存优先：详情 JSON **且** PDF 齐 → 直接返回（含 force_refresh；
    #    要重采请先删 JSON/PDF）。缺任一才进页。
    if anjianbh and zhuanlisqh \
            and not cnipa_dev.should_skip('detail', args.get('dev_skip_stages')):
        app_for_path = cnipa_contract.dotted_appno(zhuanlisqh)
        cnipa_cache.ensure_decision_pdf_on_file_root(app_for_path, anjianbh)
    if anjianbh and zhuanlisqh \
            and not cnipa_dev.should_skip('detail', args.get('dev_skip_stages')) \
            and cnipa_cache.detail_bundle_ready(lib, zhuanlisqh, anjianbh):
        cached = cnipa_cache.load_detail(lib, zhuanlisqh, anjianbh)
        if cached is not None:
            out = dict(cached)
            det = out.get('detail') if isinstance(out.get('detail'), dict) else None
            if det is not None:
                app_for_path = cnipa_contract.dotted_appno(
                    det.get('application_no') or zhuanlisqh)
                dp = cnipa_contract.decision_file_disk_path(app_for_path, anjianbh)
                pdf_ok = bool(dp and os.path.isfile(dp) and os.path.getsize(dp) > 0)
                if pdf_ok:
                    if not str(det.get('decision_file_url') or '').strip():
                        det['decision_file_url'] = cnipa_contract.build_decision_file_url(
                            app_for_path, anjianbh)
                    out['detail'] = det
                else:
                    cached = None
            if cached is not None:
                out['from_cache'] = True
                out['cache_file'] = cnipa_cache.detail_path(
                    lib, zhuanlisqh, anjianbh)
                print(f"[detail] cache hit => {out['cache_file']}", flush=True)
                cnipa_contract.emit_dict(out)
                try:
                    os.rmdir(out_dir)      # 命中即返：清掉刚建的空产物目录
                except OSError:
                    pass
                return 0

    # 1) 结果页窗口（错 tab 时 require_window 会切回 cpquery）
    win = uia.require_window()
    win_holder[0] = win
    step('window', title=win.window_text())
    if uia.dismiss_chrome_translate_offer(win, step):
        step('dismiss_translate', ok=True)
    if uia.dismiss_chrome_restore_bubble(win):
        step('dismiss_restore', ok=True)
    tabs_before = _tab_count(win)

    # 1b) 起跑自愈：上次未还原 → 详情页或空白详情标签。先关/后退再定位。
    if not args.get('keep_tab'):
        st_pre = _classify_state(uia.all_texts(win))
        if st_pre.startswith('详情页') or st_pre.startswith('空白页'):
            step('pre_heal', why=st_pre[:40])
            _cleanup_tab(win, step)

    # 1c) 仍非结果页：按 MCP 规则切 cpquery（多 Tab 逐个试到「共查询到」）
    st0 = _classify_state(uia.all_texts(win))
    if '共查询到' not in ' '.join(uia.all_texts(win)):
        ok_res, res_detail = uia.ensure_cpquery_results_front(win, step=step)
        step('ensure_results_tab', ok=ok_res, detail=res_detail, was=st0[:40])
        time.sleep(0.3)

    # 2) 按 list_index / 案号定位卡片（与 search items 同序；可见则不滚）
    list_index = args.get('list_index')
    items = ordered_texts(win)
    loc = locate_card_for_list_item(
        items, case_no=anjianbh, application_no=zhuanlisqh, index=list_index)
    if loc is None:
        loc = locate_card_link(items)
    if loc is None:
        texts0 = uia.all_texts(win)
        with open(os.path.join(out_dir, 'all_texts.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(texts0, f, ensure_ascii=False, indent=1)
        try:
            payload['tree_flat'] = flat_tree_dump(win, os.path.join(out_dir, 'tree_flat.txt'))
        except Exception as e:
            step('tree_dump', warn=str(e))
        step('locate_link', warn=f'无检索结果卡片；前台疑似[{_classify_state(texts0)}]')
        payload['ok'] = False
        payload['error'] = f'结果页没有查询到结果卡片（前台态：{_classify_state(texts0)}）'
        _finish(payload, out_dir)
        return 1
    kind, text, r, real = loc
    scrolls = 0
    while not real and scrolls < 2:
        lx, ly = _pane_xy(win, 0.48, 0.72)
        uia.scroll_down(win, lx, ly, notches=6)
        scrolls += 1
        items = ordered_texts(win)
        loc = locate_card_for_list_item(
            items, case_no=anjianbh, application_no=zhuanlisqh, index=list_index)
        if loc is None:
            loc = locate_card_link(items)
        if loc is None:
            break
        kind, text, r, real = loc
    step('locate_link', kind=kind, text=text[:40], rect=r, real=real,
         scrolls=scrolls, list_index=list_index, case_no=anjianbh or None)
    if loc is None or not real:
        step('locate_link', warn='滚屏后仍拿不到实矩形，停止（不再死滚）')
        payload['ok'] = False
        payload['error'] = '页面结构解析失败：滚屏后仍拿不到卡片链接实矩形'
        with open(os.path.join(out_dir, 'all_texts.json'), 'w', encoding='utf-8') as f:
            json.dump(uia.all_texts(win), f, ensure_ascii=False, indent=1)
        payload['tree_flat'] = flat_tree_dump(win, os.path.join(out_dir, 'tree_flat.txt'))
        _finish(payload, out_dir)
        return 1

    # 3) 拟人点击标题**左端**（19:02 瞬移点击被判非人 → 站点弃 window.open
    #    退化同页跳转；human_click：轨迹移入+悬停+90ms 按压，对齐人手行为）
    x, y = _link_point(r)
    uia.human_click(win, x, y)
    time.sleep(1.2)
    t1 = _tab_count(win)                     # 瞬变期树枚举会漏 → 双计数取大
    time.sleep(0.8)
    t2 = _tab_count(win)
    tabs_after = max(t1, t2)
    new_tab = tabs_after > tabs_before
    step('click_link', ok=True, at=(x, y), tabs_before=tabs_before,
         tabs_after=tabs_after, new_tab=new_tab,
         tabs_saw=(t1, t2))

    # 3b) 未开新标签：先看同页是否已是可用详情（2026-09-07 用户截图实证：
    #     同页 SPA 已渲染决定书正文，旧逻辑一律当空壳 Ctrl+W → 退回结果页
    #     再无谓滚动/重点）。仅空白壳才 cleanup + Ctrl 强制新标签。
    if not new_tab:
        time.sleep(1.0)
        texts_now = uia.all_texts(win)
        joined_now = ' '.join(texts_now)
        same_ok = (
            (not _on_results(win))
            and (_detail_ready(texts_now)
                 or _classify_state(texts_now).startswith('详情页')
                 or any(m in joined_now for m in DOC_MARKERS)))
        if same_ok:
            step('click_link_same_tab', ok=True, why='detail_already_rendered')
        else:
            step('click_link_retry', why='no_new_tab_spa_shell_risk')
            _cleanup_tab(win, step)
            time.sleep(0.8)
            items = ordered_texts(win)
            loc = locate_card_link(items)
            if loc is None or not loc[3]:
                payload['ok'] = False
                payload['error'] = '标题点击未开新标签，还原结果页后仍找不到卡片'
                _finish(payload, out_dir)
                return 1
            kind, text, r, real = loc
            x, y = _link_point(r)
            tabs_before = _tab_count(win)
            uia.human_ctrl_click(win, x, y)
            time.sleep(1.5)
            t1 = _tab_count(win)
            time.sleep(0.8)
            t2 = _tab_count(win)
            tabs_after = max(t1, t2)
            new_tab = tabs_after > tabs_before
            step('click_link_ctrl', ok=True, at=(x, y), tabs_before=tabs_before,
                 tabs_after=tabs_after, new_tab=new_tab, tabs_saw=(t1, t2))
            if not new_tab:
                payload['ok'] = False
                payload['error'] = (
                    '标题链接点击后未打开新标签（瑞数退化同页空壳）。'
                    '请勿在空壳页手工点导航；先回到结果列表再试。'
                )
                try:
                    _cleanup_tab(win, step)
                except Exception:
                    pass
                _finish(payload, out_dir)
                return 1

    # 4) 新标签常在后台加载：前台仍是结果列表时先切到最新详情 Tab，禁止立刻点下一条
    if _on_results(win):
        if not _activate_front_detail_tab(win, step, case_no=anjianbh):
            step('abort', why='点击后仍在结果页且切不到详情 Tab')
            payload['ok'] = False
            payload['error'] = '未能进入详情页（点击后仍在结果列表）'
            _finish(payload, out_dir)
            return 1

    def _reopen_from_results() -> bool:
        if not _on_results(win):
            uia.ensure_cpquery_results_front(win, step=step)
            time.sleep(0.35)
        items = ordered_texts(win)
        loc2 = locate_card_for_list_item(
            items, case_no=anjianbh, application_no=zhuanlisqh,
            index=list_index)
        if loc2 is None:
            loc2 = locate_card_link(items)
        if loc2 is None or not loc2[3]:
            return False
        return _open_detail_from_card_click(win, step, loc2[2], anjianbh)

    rdy = _wait_detail_open(
        win, step, case_no=anjianbh, reopen=_reopen_from_results)
    step('wait_detail', **rdy)
    if not rdy.get('ready'):
        rdy2 = _wait_detail_ready(win, timeout_s=12.0)
        step('wait_detail', phase='shell_fallback', **rdy2)
        if rdy2.get('ready') and _detail_usable(win, anjianbh):
            rdy = rdy2
    if not rdy.get('ready'):
        step('abort', why='详情转圈/空壳超时（已刷新与重开）',
             on_results=_on_results(win))
        payload['ok'] = False
        payload['error'] = (
            '详情页加载超时' if _on_results(win)
            else '详情页转圈超时（已 F5 与关 Tab 重进）')
        _finish(payload, out_dir)
        return 1

    nav_case = (anjianbh or str(card_fields.get('case_no') or '')).strip()
    if lib == 'reexam':
        prefer = '复审审查决定'
    elif lib == 'invalid' or str(card_fields.get('invalid_requester') or '').strip():
        prefer = '无效审查决定'
    else:
        prefer = '复审审查决定'
    doc = _wait_doc_ready(win, out_dir, step, budget_s=12.0)
    step('wait_doc', **doc, probe_first=True)
    drilled = False
    if not doc.get('ready') and not _on_results(win):
        if nav_case and rdy.get('ready'):
            # PDF 红条/空壳：先刷新，禁止先滚再刷（092548 实况滚了 18s）
            if (doc.get('need_refresh') or doc.get('last_err') in (
                    'pdf_load_error', 'shell_empty', 'pdf_load_error_or_empty')
                    or _detail_incomplete_shell(win, nav_case)):
                why = 'pdf_load_error' if 'pdf' in str(doc.get('last_err') or '') \
                    else '左树无案件号或右栏空白（下钻前先刷新）'
                _browser_refresh_detail(win, step, why)
                rdy = _wait_detail_ready(win, timeout_s=35.0)
                step('wait_detail', phase='after_refresh', **rdy)
                # 刷新后右栏可能已好 → 跳过无意义下钻点击
                doc_r = _wait_doc_ready(win, out_dir, step, budget_s=8.0)
                step('wait_doc', **doc_r, probe_first=False, after='refresh')
                if doc_r.get('ready'):
                    doc = doc_r
            if not doc.get('ready'):
                drilled = _nav_drill(
                    win, nav_case, prefer, step, budget_s=25.0, out_dir=out_dir)
            else:
                drilled = True
                step('nav_drill', skipped=True, why='doc_ready_after_refresh')
            # 仅「下钻失败」或「仍半壳」才二次刷新；已成功下钻且右栏有锚词则不再刷
            if (not drilled or _detail_incomplete_shell(win, nav_case)) and \
                    not _on_results(win):
                _browser_refresh_detail(
                    win, step, '下钻无进展或仍无案件号/右栏内容')
                _wait_detail_ready(win, timeout_s=20.0)
                drilled = _nav_drill(
                    win, nav_case, prefer, step, budget_s=25.0,
                    out_dir=out_dir) or drilled
            if drilled:
                app_pre = (cnipa_contract.dotted_appno(zhuanlisqh)
                           if zhuanlisqh else '')
                disk_pre = (cnipa_contract.decision_file_disk_path(
                    app_pre, nav_case)
                            if app_pre and nav_case else '')
                pdf_on_disk = bool(
                    disk_pre and os.path.isfile(disk_pre)
                    and os.path.getsize(disk_pre) > 0)
                try:
                    from run_cnipa_fetch_decision_file_uia import (  # noqa: E402
                        _wait_pdf_viewer_ready, _pdf_viewer_visible_light,
                    )
                    viewer_live = _pdf_viewer_visible_light(win)
                    if pdf_on_disk or doc.get('ready') or viewer_live:
                        step('wait_pdf_viewer', skipped=True,
                             why='pdf_on_disk_or_doc_ready',
                             pdf_on_disk=pdf_on_disk,
                             doc_ready=bool(doc.get('ready')))
                    else:
                        _wait_pdf_viewer_ready(
                            win, out_dir, step, timeout_s=35.0)
                except Exception as e:
                    step('wait_pdf_viewer', warn=str(e)[:80])
                # 盘上已有 PDF：勿再 wait_doc 滚探（download 走 disk_reuse）
                if not pdf_on_disk:
                    doc2 = _wait_doc_ready(win, out_dir, step, budget_s=8.0)
                    step('wait_doc', **doc2, probe_first=False, after='nav')
                    doc = doc2
        else:
            step('nav_drill', warn='跳过长下钻：详情壳未就绪或无案件编号')
    payload['nav_drilled'] = drilled
    # 右栏未就绪时勿滚轮：对「载入 PDF 错误」/空壳无效，只会拖鼠标空转

    decision_file_url = ''
    pdf_disk = ''
    decision_type = ''  # old|new|''（页内判定；列表无法区分）
    skip_pdf = cnipa_dev.should_skip('decision_pdf', args.get('dev_skip_stages'))
    if (doc.get('ready') or rdy.get('ready')) and anjianbh and zhuanlisqh:
        app_for_path = cnipa_contract.dotted_appno(zhuanlisqh)
        disk_path = cnipa_contract.decision_file_disk_path(app_for_path, anjianbh)
        # 缓存侧 PDF 先拷到对外路径，便于 download 走 disk_reuse
        if disk_path and not (
                os.path.isfile(disk_path) and os.path.getsize(disk_path) > 0):
            cached = cnipa_cache.load_decision_file(app_for_path, anjianbh)
            if cached is not None and cached.get('kind') == 'pdf':
                src = cached.get('path') or ''
                if src and os.path.isfile(src):
                    os.makedirs(os.path.dirname(disk_path), exist_ok=True)
                    shutil.copyfile(src, disk_path)
        dtype_via = ''
        page_kind = ''
        if skip_pdf:
            pdf_on = bool(disk_path and os.path.isfile(disk_path)
                          and os.path.getsize(disk_path) > 0)
            step('decision_file', skipped=True, why='dev_skip_decision_pdf',
                 pdf_on_disk=pdf_on)
            # 仍需判型（页内 + 列表发文日），供后续采数分流
            try:
                from run_cnipa_fetch_decision_file_uia import (  # noqa: E402
                    _detect_decision_type, _wait_pdf_viewer_ready,
                    _pdf_viewer_visible_light,
                )
                if pdf_on and not _pdf_viewer_visible_light(win):
                    step('wait_pdf_viewer', skipped=True,
                         why='skip_pdf_dev_pdf_on_disk')
                    decision_type, dtype_via = resolve_decision_type_kind(
                        '', card_fields)
                    page_kind = 'unknown'
                elif _wait_pdf_viewer_ready(win, out_dir, step, timeout_s=45.0):
                    page_kind = _detect_decision_type(win, out_dir, step) or ''
                    decision_type, dtype_via = resolve_decision_type_kind(
                        page_kind, card_fields)
                else:
                    decision_type, dtype_via = resolve_decision_type_kind(
                        '', card_fields)
                    page_kind = 'unknown'
                step('decision_type', kind=decision_type or 'unknown',
                     page=page_kind or 'unknown', via=dtype_via or 'none',
                     issue_date=str(card_fields.get('issue_date') or '')[:32])
            except Exception as e:
                step('decision_type', warn=f'skip_pdf_detect: {e}')
        elif disk_path:
            # 始终进 download（带入列表发文日）：判型→有 PDF 则 reuse；无则另存/打印
            try:
                from run_cnipa_fetch_decision_file_uia import (  # noqa: E402
                    download_decision_pdf_on_detail,
                )
                pdf_res = download_decision_pdf_on_detail(
                    win, out_dir, disk_path, step, card_fields=card_fields)
                step('decision_file', **{k: pdf_res.get(k) for k in
                                         ('ok', 'method', 'error', 'pages',
                                          'decision_type', 'decision_type_page',
                                          'decision_type_via')
                                         if k in pdf_res})
                decision_type = str(
                    pdf_res.get('decision_type') or '').strip().lower()
                page_kind = str(
                    pdf_res.get('decision_type_page') or '').strip().lower()
                dtype_via = str(pdf_res.get('decision_type_via') or '')
                if decision_type not in ('old', 'new'):
                    # download 未判出时再兜一次（含仅 page 字段）
                    decision_type, dtype_via = resolve_decision_type_kind(
                        page_kind or decision_type, card_fields)
                if pdf_res.get('ok') and pdf_res.get('method') != 'print_move_only':
                    if (disk_path and os.path.isfile(disk_path)
                            and os.path.getsize(disk_path) > 0):
                        cnipa_cache.write_decision_file(
                            app_for_path, anjianbh, disk_path)
                if decision_type in ('new', 'old') and pdf_res.get('ok'):
                    cnipa_cache.write_decision_meta(
                        app_for_path, anjianbh, decision_type,
                        method=str(pdf_res.get('method') or ''))
            except Exception as e:
                step('decision_file', warn=f'下载异常（不挡详情）: {e}')
                decision_type, dtype_via = resolve_decision_type_kind(
                    decision_type, card_fields)
        if disk_path and os.path.isfile(disk_path) and os.path.getsize(disk_path) > 0:
            decision_file_url = cnipa_contract.build_decision_file_url(
                app_for_path, anjianbh)
            pdf_disk = disk_path
        payload['decision_type'] = decision_type or ''
        payload['decision_type_page'] = page_kind or 'unknown'
        payload['decision_type_via'] = dtype_via or ''
        step('decision_type_resolved', kind=decision_type or 'unknown',
             page=page_kind or 'unknown', via=dtype_via or 'none',
             issue_date=str(card_fields.get('issue_date') or '')[:32])
        if decision_type not in ('old', 'new'):
            payload['ok'] = False
            payload['error'] = 'DECISION_TYPE_UNKNOWN'
            payload['message'] = (
                '无法判定新旧决定（页内 unknown，且列表无可用发文日作补充）')
            payload['elapsed_s'] = round(time.time() - t0, 1)
            if not args.get('keep_tab'):
                try:
                    payload['cleanup_ok'] = _cleanup_tab(win, step)
                except Exception as e:
                    step('cleanup', warn=str(e))
            run_finished[0] = True
            atexit.unregister(_emergency_teardown)
            _finish(payload, out_dir)
            return 1
        # 字段采自 PDF/a11y，不必再整页上滚
        step('post_pdf_scroll', skipped=True, why=f'{decision_type}_pdf_fields')

    # 5) 采证：old=OCR 读 PDF；new=a11y 为主 + PDF OCR 补缺
    texts = uia.all_texts(win)
    with open(os.path.join(out_dir, 'all_texts.json'), 'w', encoding='utf-8') as f:
        json.dump(texts, f, ensure_ascii=False, indent=1)
    try:
        payload['tree_flat'] = flat_tree_dump(win, os.path.join(out_dir, 'tree_flat.txt'))
    except Exception as e:
        step('tree_dump', warn=str(e))
    skip_harvest = cnipa_dev.should_skip('detail_harvest', args.get('dev_skip_stages'))
    shots, pages = [], []
    is_old = decision_type == 'old'
    is_new = decision_type == 'new'
    if skip_harvest:
        step('harvest', skipped=True, why='dev_skip_detail_harvest')
    elif _on_results(win):
        step('harvest', skipped=True, why='仍在结果页，禁止滚屏')
    elif is_old and pdf_disk:
        # 老决定：PDF OCR 只要第1、2页（字段表）+ 末页（合议组）
        try:
            shots, pages = _harvest_pdf(
                pdf_disk, out_dir, step, dpi=200, budget_s=120.0,
                head_and_tail=True)
            payload['harvest_from'] = 'pdf_head_tail'
            step('harvest', mode='old_pdf_head_tail', shots=len(shots))
        except Exception as e:
            step('harvest_pdf', warn=f'old_pdf_ocr 失败: {e}')
            pages = []
    elif is_new:
        # 新决定：字段靠 a11y；合议组只 OCR 盘上 PDF 末 1～2 页（不截浏览器、不翻页）
        hk = _pdf_ocr_harvest_kwargs(card_fields, texts)
        if hk is None:
            step('harvest_pdf', skipped=True, why='card_a11y_fields_ok')
        elif not pdf_disk:
            step('harvest_pdf', skipped=True, why='no_pdf')
        else:
            try:
                shots, pages = _harvest_pdf(
                    pdf_disk, out_dir, step, dpi=220, max_pages=2,
                    budget_s=25.0, tail_only=True)
                payload['harvest_from'] = 'pdf_tail'
                step('harvest', mode='new_pdf_tail_panel', shots=len(shots))
            except Exception as e:
                step('harvest_pdf', warn=f'new_pdf_ocr 失败: {e}')
                pages = []
    # 老决定正道：页间线另存为→合并 PDF→OCR；已取消页内滚屏 OCR 兜底
    if is_old and not pages and not skip_harvest and not _on_results(win):
        step('harvest', skipped=True,
             why=('old_no_pdf_no_scroll_fallback' if not pdf_disk
                  else 'old_pdf_ocr_empty'))
    payload['shots'] = [os.path.basename(s) for s in shots]
    payload['ocr_pages'] = len(pages)

    # 6) 合并：old=卡片>OCR；new=卡片>a11y>OCR
    a11y_detail = map_detail(parse_detail_texts(texts))
    ocr_detail = co.parse_detail_pages(pages) if pages else {}
    # 通道对照落盘：最终字段不对时便于区分 a11y vs PDF OCR
    try:
        for name, blob in (('a11y_fields.json', a11y_detail),
                           ('ocr_fields.json', ocr_detail),
                           ('card_fields.json', card_fields or {})):
            with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
                json.dump(blob, f, ensure_ascii=False, indent=2)
        if anjianbh and zhuanlisqh and not cnipa_cache.cache_disabled():
            for path, blob in (
                    (cnipa_cache.detail_a11y_path(lib, zhuanlisqh, anjianbh),
                     a11y_detail),
                    (cnipa_cache.detail_ocr_path(lib, zhuanlisqh, anjianbh),
                     ocr_detail)):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(blob, f, ensure_ascii=False, indent=2)
            step('channels_dump',
                 a11y=os.path.basename(cnipa_cache.detail_a11y_path(
                     lib, zhuanlisqh, anjianbh)),
                 ocr=os.path.basename(cnipa_cache.detail_ocr_path(
                     lib, zhuanlisqh, anjianbh)))
        else:
            step('channels_dump', run_dir=True)
    except Exception as e:
        step('channels_dump', warn=str(e)[:160])
    if is_old:
        detail = merge_channels(card_fields, {}, ocr_detail)
        step('merge', mode='old_card_ocr',
             ocr_n=sum(1 for k in DETAIL_REPORT_KEYS if (ocr_detail.get(k) or '').strip()),
             filled=sum(1 for k in DETAIL_REPORT_KEYS if (detail.get(k) or '').strip()))
    else:
        detail = merge_channels(card_fields, a11y_detail, ocr_detail)
        detail['panel_members'] = pick_panel_members(a11y_detail, ocr_detail)
        detail['decision_summary'] = co.strip_browser_ui_tail(
            detail.get('decision_summary') or '')
        step('merge', mode='new_card_a11y_ocr',
             a11y_n=sum(1 for k in DETAIL_REPORT_KEYS if (a11y_detail.get(k) or '').strip()),
             ocr_n=sum(1 for k in DETAIL_REPORT_KEYS if (ocr_detail.get(k) or '').strip()),
             filled=sum(1 for k in DETAIL_REPORT_KEYS if (detail.get(k) or '').strip()))
    # 申请号纠偏（21:5x 用户指出三个不同值）：归一后与入参相等的通道值优先，
    # 须在 _channel_view 之前做——sources/diffs 反映的必须是最终值
    app_final, app_note = reconcile_application_no(
        detail, card_fields, a11y_detail, ocr_detail, zhuanlisqh)
    if app_final != (detail.get('application_no') or ''):
        step('reconcile', field='application_no',
             was=detail.get('application_no'), now=app_final, by=app_note)
    detail['application_no'] = cnipa_contract.dotted_appno(app_final) or app_final
    # 技能侧契约 §3.1：调用方传入的 detail_url 原样回显进 detail
    # （契约层 clean_detail 优先取 detail.detail_url，其次才用模板构造）
    if detail_url_arg:
        detail['detail_url'] = detail_url_arg
    detail['decision_file_url'] = decision_file_url
    if not detail['decision_file_url'] and anjianbh and zhuanlisqh:
        app_for_path = cnipa_contract.dotted_appno(app_final)
        dp = cnipa_contract.decision_file_disk_path(app_for_path, anjianbh)
        if dp and os.path.isfile(dp) and os.path.getsize(dp) > 0:
            detail['decision_file_url'] = cnipa_contract.build_decision_file_url(
                app_for_path, anjianbh)
    missing = [k for k in DETAIL_REPORT_KEYS if not (detail.get(k) or '').strip()]
    sources, diffs = _channel_view(detail, card_fields, ocr_detail, a11y_detail)
    # payload 只留交付字段（21:5x 用户反馈：字段分散、掺运行元数据）；
    # 两通道比对过程(channel_diffs)是排障遥测 → _finish 移交 debug.json
    payload.update({'detail': detail,
                    'field_sources': sources,
                    'channel_diffs': diffs,
                    'missing_fields': missing,
                    'field_count': len(DETAIL_REPORT_KEYS) - len(missing)})
    match = {}
    if anjianbh:
        match['case_no'] = re.sub(r'[^0-9A-Za-z]', '', detail.get('case_no', '')).upper() \
            == re.sub(r'[^0-9A-Za-z]', '', anjianbh).upper()
    if zhuanlisqh:
        match['application_no'] = re.sub(r'[^0-9A-Za-z]', '', detail.get('application_no', '')).upper() \
            == re.sub(r'[^0-9A-Za-z]', '', zhuanlisqh).upper()
    if match:
        payload['match'] = match
        payload['match_note'] = {'application_no': app_note}
    step('parse', fields=payload['field_count'], missing=missing or '无')

    # 7) 还原现场（PDF 路径已在 §5 前还原，避免 OCR 期间占前台抢鼠标）
    if not args.get('keep_tab'):
        try:
            payload['cleanup_ok'] = _cleanup_tab(win, step)
        except Exception as e:
            step('cleanup', warn=str(e))

    payload['elapsed_s'] = round(time.time() - t0, 1)
    payload['ocr_hit'] = any((detail.get(k) or '').strip()
                             for k in ('request_date', 'legal_basis',
                                       'decision_summary', 'panel_members'))
    payload['ok'] = _run_ok(payload['field_count'], detail)
    if not payload['ok']:
        payload['error'] = (f'详情字段不足（{payload["field_count"]}/12 列，'
                            f'缺 {missing or "?"}）——两通道均未取全')
    elapsed = payload['elapsed_s']              # _finish 会把耗时移去 debug
    # 成功且双号齐全 → 写持久缓存（§4.2；write_detail_cache 内部做契约投影，
    # 缓存文件本身不含 from_cache/cache_file），响应回填 from_cache=false
    if payload['ok'] and anjianbh and zhuanlisqh:
        cf = cnipa_contract.write_detail_cache(lib, zhuanlisqh, anjianbh, payload)
        if cf:
            payload['cache_file'] = cf
            payload['from_cache'] = False
    run_finished[0] = True
    atexit.unregister(_emergency_teardown)
    _finish(payload, out_dir)
    print(f"[detail] 完成：{payload['field_count']}/12 列，缺 {missing or '无'}，"
          f"耗时 {elapsed}s，产物目录：{out_dir}")
    return 0 if payload['ok'] else 1


# 消费者不需要的键（运行元数据/过程遥测/通道比对）——_finish 移交 debug.json
PAYLOAD_TELEMETRY_KEYS = ('mode', 'run_dir', 'input_file', 'started_at',
                          'nav_drilled', 'ocr_pages', 'tail_ok', 'shots',
                          'channel_diffs', 'cleanup_ok', 'ocr_hit',
                          'elapsed_s', 'steps', 'tree_flat')

_DEBUG_SNAPSHOT_STEPS = frozenset({
    'old_page', 'decision_file', 'old_abort', 'decision_type', 'old_fetch',
})


def _write_debug_snapshot(payload: dict, out_dir: str) -> None:
    """长跑/被 kill 前落盘 steps，便于排障（不 pop payload）。"""
    snap = {k: payload[k] for k in PAYLOAD_TELEMETRY_KEYS if k in payload}
    snap.setdefault('steps', list(payload.get('steps') or []))
    snap['note'] = '进行中快照（run 尚未 _finish）'
    path = os.path.join(out_dir, 'debug.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _finish(payload: dict, out_dir: str) -> None:
    """落盘两份（20:50 拆分惯例，21:5x 再收紧：payload 里只留消费者要的）：
    payload.json = ok/detail(12 列终值)/field_sources/missing_fields/
    field_count/match——**值只此一份**，不散落其它键；
    debug.json = 运行元数据 + steps/shots/tree_flat + channel_diffs
    （两通道比对是排障过程，不是结果）等全部遥测。"""
    debug = {k: payload.pop(k) for k in list(payload)
             if k in PAYLOAD_TELEMETRY_KEYS}
    debug['note'] = '遥测（运行元数据/每步动作/通道比对/树 dump）；结果看 payload.json'
    with open(os.path.join(out_dir, 'payload.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, 'debug.json'), 'w', encoding='utf-8') as f:
        json.dump(debug, f, ensure_ascii=False, indent=2)
    cnipa_contract.maybe_emit('detail', payload)


# ---------------------------------------------------------------------------
# 离线自检：定位器 + 解析器 + 合并，不碰网站/窗口/OCR
# ---------------------------------------------------------------------------

def _load_replay_items(path: str) -> list:
    items = []
    rx = re.compile(r'^(Text|Edit) \| name=(.*?) \| rect=\(([^)]*)\)')
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = rx.match(line.strip())
            if not m:
                continue
            name = m.group(2)
            if name.startswith(("'", '"')) and name.endswith(("'", '"')):
                name = name[1:-1]
            rect = tuple(int(v.strip()) for v in m.group(3).split(','))
            if name:
                items.append((name, rect))
    return items


# 03 实跑（run_20260901_171031）首卡字段——卡片覆盖通道的冻结样本
_CARD_FIXTURE = {
    'patent_type': '发明专利',
    'title': '(±)2-(二甲基氨基)-1-{[O-(m-甲氧基苯乙基)苯氧基]甲基}乙基氢琥珀酸酯盐酸盐的晶体',
    'case_no': '4W107577', 'decision_no': '38430', 'issue_date': '20181228',
    'app_no': '2006100041736', 'patentee': '远大医药（天津）有限公司',
    'app_date': '2006-02-22', 'class_no': 'C07C229/24',
    'conclusion': '维持有效', 'invalid_requester': '李瑶',
}


def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    # ── locate_card_link ──
    real_dump = os.path.join(ROOT, 'output', 'uia_runs', 'run_20260901_171031', 'tree_flat.txt')
    if os.path.isfile(real_dump):
        items = _load_replay_items(real_dump)
        loc = locate_card_link(items)
        check('A) 03真实树回放：定位 title（0矩形→real=False）',
              loc is not None and loc[0] == 'title' and loc[3] is False
              and '晶体' in loc[1])

    scrolled = [('共查询到 ', (236, 911, 296, 928)), ('1', (295, 911, 305, 928)),
                ('条结果', (304, 911, 350, 928)),
                ('发明专利 ', (256, 1033, 320, 1052)),
                ('案件编号： ', (340, 1035, 420, 1052)),
                ('4W107577', (425, 1035, 500, 1052)),
                ('决定号：38430', (340, 1080, 460, 1097))]
    lb = locate_card_link(scrolled)
    check('B) 实矩形·无标题→备选 case_no 值（拆分形态）',
          lb is not None and lb[0] == 'case_no_value' and lb[1] == '4W107577'
          and lb[2] == (425, 1035, 500, 1052) and lb[3] is True)

    titled = [('共查询到 1 条结果', (236, 911, 350, 928)),
              ('发明专利 ', (256, 1033, 320, 1052)),
              ('一种晶体制备方法', (330, 1033, 700, 1052)),
              ('案件编号：4W107577', (340, 1080, 520, 1097))]
    lc = locate_card_link(titled)
    check('C) 标题实矩形命中（kind=title）',
          lc is not None and lc[0] == 'title' and lc[1] == '一种晶体制备方法' and lc[3] is True)

    check('D) 无结果页返回 None', locate_card_link([('共查询到 0 条', (236, 911, 400, 928))]) is None)

    # ── parse_detail_texts / map_detail ──
    joined = ['案件编号：4W107577', '决定号：38430', '决定日：20181228', '发明名称：一种晶体',
              '无效宣告请求人：李瑶', '专利权人：远大医药（天津）有限公司',
              '申请号/专利号：2006100041736', '无效宣告请求日：2017-05-10',
              '法律依据：专利法第22条第3款', '决定要点：全文……', '审查结论：维持有效',
              '合议组成员：组长 张三；主审员 李四；参审员 王五']
    d1 = map_detail(parse_detail_texts(joined))
    check('E) 详情解析 12 列全对', all(d1.get(k) for k in DETAIL_REPORT_KEYS)
          and d1['case_no'] == '4W107577' and d1['legal_basis'] == '专利法第22条第3款')
    separate = ['案件编号：', '4W107577', '决定号：', '38430', '法律依据：', '专利法第22条第3款',
                '发明名称', '一种晶体', '案件编号：', '4W107577', '共1条']
    d2 = map_detail(parse_detail_texts(separate))
    check('F) 拆分+裸标签+双枚举', d2['case_no'] == '4W107577' and d2['decision_no'] == '38430'
          and d2['legal_basis'] == '专利法第22条第3款' and d2['invention_title'] == '一种晶体')
    multi = ['法律依据：', '专利法第22条第3款', '专利法实施细则第65条',
             '决定要点：', '本专利涉及百度网盘。', '现有技术公开了软件。',
             '合议组组长：熊婷', '主审员：吴士芬', '参审员：杜宇',
             '（第 583248 号）']
    dm = map_detail(parse_detail_texts(multi))
    check('F2) a11y多行要点+法律依据+合议组+决定号',
          '百度网盘' in (dm.get('decision_summary') or '')
          and '现有技术' in (dm.get('decision_summary') or '')
          and '实施细则' in (dm.get('legal_basis') or '')
          and '熊婷' in (dm.get('panel_members') or '')
          and dm.get('decision_no') == '583248')
    m_ch = merge_channels(
        {},
        {'decision_summary': '百度网盘，软件。', 'petitioner': '甲'},
        {'decision_summary': '百匿网盘软件', 'petitioner': '乙', 'request_date': '2018-01-01'})
    check('F3) 能a11y就用a11y（OCR不盖）',
          m_ch['decision_summary'] == '百度网盘，软件。'
          and m_ch['petitioner'] == '甲'
          and m_ch['request_date'] == '2018-01-01')
    k1, v1 = resolve_decision_type_kind('old', {})
    k2, v2 = resolve_decision_type_kind('unknown', {'issue_date': '20250315'})
    k3, v3 = resolve_decision_type_kind('unknown', {'issue_date': '20181228'})
    k4, _ = resolve_decision_type_kind('', {})
    k5, v5 = resolve_decision_type_kind('new', {'issue_date': '20181228'})
    check('F3b) 判型：页内优先；unknown+发文补 new/old；无日期空',
          k1 == 'old' and v1 == 'page'
          and k2 == 'new' and v2 == 'issue_date_2025'
          and k3 == 'old' and v3 == 'issue_date_2018'
          and k4 == ''
          and k5 == 'new' and v5 == 'page')
    m_res = merge_channels(
        {'conclusion': '维持专利权有效'},
        {'decision_result': '维持专利权有效全部无效部分无效'},
        {'decision_result': '维持专利权有效'})
    check('F4) 审查结论多选项→改OCR/卡片',
          m_res['decision_result'] == '维持专利权有效')
    # F4b) 合议组：脏 OCR 不得盖可信 a11y（090427 实况：专利局章…708136）
    junk_pm = '专利局章参审员杜宇708136'
    good_pm = '合议组组长：熊婷；主审员：刘茜；参审员：杜宇'
    check('F4b) 脏OCR panel 不可信',
          not co.panel_members_trustworthy(junk_pm)
          and co.panel_members_trustworthy(good_pm))
    check('F4c) 合议组：可信a11y胜脏OCR',
          pick_panel_members({'panel_members': good_pm},
                             {'panel_members': junk_pm}) == good_pm)
    check('F4d) 合议组：可信OCR胜a11y',
          pick_panel_members({'panel_members': '参审杜宇'},
                             {'panel_members': good_pm}) == good_pm)
    shell = ['地址和搜索栏', '国家知识产权局', '中国及多国专利审查信息查询',
             '国内案件信息详情', '申请号/专利号：', '', '京ICP备05069085号-14']
    check('G) 壳页（空申请号）列数<就绪阈值', _field_count(parse_detail_texts(shell)) < READY_MIN_FIELDS
          and not _detail_ready(shell))
    results_like = ['中国及多国专利审查信息查询', '共查询到 1 条结果', '发明专利 ',
                    '案件编号：4W107577']
    detail_like = ['国内案件信息详情', '申请号/专利号：2006100041736']
    home_like = ['地址和搜索栏', '国家知识产权局', '中国及多国专利审查信息查询',
                 '申请号', '发明名称', '查询']
    check('K) 页态分类：结果/详情/入口三态',
          _classify_state(results_like).startswith('结果页')
          and _classify_state(detail_like).startswith('详情页')
          and _classify_state(shell).startswith('详情页')      # 壳页也是详情标签
          and _classify_state(home_like).startswith('非结果页'))
    check('K2) 几乎无内容→空白页',
          _classify_state(['地址和搜索栏', 'Chrome Legacy Window']).startswith('空白页'))

    # ── ok 判定收紧（19:02 假阳性回归：卡片 8/12 + 右栏全空 ≠ 成功） ──
    card_only = merge_detail(_CARD_FIXTURE, {})          # 右栏 OCR 全空（19:02 现场）
    card_n = len(DETAIL_REPORT_KEYS) - len(
        [k for k in DETAIL_REPORT_KEYS if not (card_only.get(k) or '').strip()])
    full = {**card_only, 'request_date': '20180921',
            'legal_basis': '专利法第22条第3款', 'decision_summary': '维持有效',
            'panel_members': '张三；李四；王五'}
    check('R) ok收紧：卡片8列无右栏字段→False', not _run_ok(card_n, card_only))
    check('R2) ok收紧：右栏OCR命中→True', _run_ok(card_n, full))

    # ── 收割停机规则（19:34 第2页截断现场回归） ──
    # shot4 现场还原：三要素刚齐、正文仍在增长（第2页还有4行没读）→ 必须续滚
    check('S) 三要素齐但正文在长→不停（续滚第2页）',
          not _stop_harvest(3, [], no_grow=0, panel_ok=True))
    # 壳区空转屏（无正文增长）但三要素未齐 → 不停
    check('S2) 三要素未齐→不停（壳区空转屏）',
          not _stop_harvest(1, ['request_date'], no_grow=2, panel_ok=False))
    # 底部停机：三要素齐 + panel 齐 + 连续2屏不增长
    check('S3) 齐备+panel齐+连续2屏不增长→停',
          _stop_harvest(5, [], no_grow=2, panel_ok=True))
    # panel 未齐需更保守（连续3屏）
    check('S4) panel不齐→需连续3屏才停',
          not _stop_harvest(5, [], no_grow=2, panel_ok=False)
          and _stop_harvest(5, [], no_grow=3, panel_ok=False))
    # 首屏（i=0）永不单独停机
    check('S5) 首屏不停', not _stop_harvest(0, [], no_grow=9, panel_ok=True))

    # ── 末页跳底补收（_tail_stop/_sig_fp 纯函数，20:01 用户指出缺口） ──
    check('T) 名单齐→立即停', _tail_stop(True, '某行', '另行'))
    check('T2) 屏指纹两跳相同=触底→停', _tail_stop(False, '底部行', '底部行'))
    check('T3) 指纹在变/首跳无前值→不停',
          not _tail_stop(False, '甲行', '乙行') and not _tail_stop(False, '甲行', ''))
    fp_items = [('的 中 苤 及 多 苤 专 利 0 信 息', (56, 20, 1019, 36)),
                ('在 无 效 程 序 中 如 果 现 有 技 术', (744, 784, 1460, 801))]
    check('T4) 屏指纹跳过恒定壳区(y<140)取首个内容行',
          _sig_fp(fp_items).startswith('在无效程序中'))
    check('T5) 全壳行→空指纹', _sig_fp(fp_items[:1]) == '')

    # ── 关标签闸门（20:14 实况：叠≥2枚详情标签，单发Ctrl+W不够） ──
    check('U) 前台详情页+未超限→继续关', _close_gate('详情页（不是检索结果列表）', 0))
    check('U2) 叠多枚：第2枚仍是详情页→继续关',
          _close_gate('详情页（不是检索结果列表）', 1))
    check('U3) 超3枚→停 / 非详情页→停（防误关用户标签）',
          not _close_gate('详情页（不是检索结果列表）', 3)
          and not _close_gate('结果页', 0)
          and not _close_gate('入口页（未检索）', 0))
    check('U4) 空白详情页可关',
          _close_gate('空白页（详情加载失败或空标签）', 0)
          and _classify_state(['地址和搜索栏', '']).startswith('空白页'))

    # ── V) 通道去重视图（20:35 payload 重复反馈）：同值不重复落盘 ──
    v_det = {'case_no': '4W107577', 'patentee': '远大医药（天津）有限公司',
             'decision_summary': '要' * 100 + '”'}
    v_card = {'case_no': '4W107577', 'patentee': '远大医药（天津）有限公司',
              'title': '某晶体'}
    v_ocr = {'case_no': '4W107577', 'patentee': '天津田边制药有限公司',
             'decision_summary': '要' * 100 + '”'}
    src, df = _channel_view(v_det, v_card, v_ocr, {'case_no': ''})
    check('V) sources 标注同值通道 / diffs 只留异值（80字截断）',
          src['case_no'] == 'card+ocr' and src['decision_summary'] == 'ocr'
          and 'decision_summary' not in df['ocr']                # 同值全文不再重复
          and df['card'] == {} and df['ocr']['patentee'] == '天津田边制药有限公司'
          and len(df['ocr'].get('patentee', '')) <= 81 and df['a11y'] == {}
          and src['patentee'] == 'card'
          and _channel_view(v_det, v_card, v_ocr,
                            {'patentee': '远大医药（天津）有限公司'})[0]['patentee']
          == 'card+a11y')                                       # a11y 同值也并入来源

    # ── 页内导航下钻（18:56 展开态 / 19:02 收起态 树 dump 冻结） ──
    nav_expanded = [                                   # run_20260901_185633 实 rect
        (' 复审无效审查决定', (306, 562, 422, 579)),
        (' 复审审查决定', (324, 588, 412, 605)),
        (' 无效审查决定', (324, 614, 412, 631)),
        (' 4W107577', (342, 640, 414, 657)),
        (' 4W107577', (654, 324, 738, 344)),           # 右栏顶栏摘要同名干扰
    ] * 2                                              # 幻影双枚举副本
    na = next_nav_action(nav_expanded, '4W107577', '无效审查决定')
    check('L1) 展开态直取案件项（右栏 x=654 干扰已排除）',
          na == ('case', '4W107577', (342, 640, 414, 657)))
    nb = next_nav_action(nav_expanded, '', '无效审查决定')
    check('L2) 无案件号→择路 prefer=无效审查决定',
          nb is not None and nb[0] == 'branch' and nb[1] == '无效审查决定')
    nc = next_nav_action(nav_expanded, '', '')
    check('L3) 无 prefer→树序第一分支（复审审查决定）',
          nc is not None and nc[0] == 'branch' and nc[1] == '复审审查决定')
    nav_collapsed = [                                  # 19:02 all_texts 实文（rect 同位估计）
        (' 申请信息', (332, 390, 374, 402)),
        (' 复审无效审查决定', (306, 562, 422, 579)),
    ]
    ng = next_nav_action(nav_collapsed, '4W107577', '无效审查决定')
    check('M1) 收起态先点分组展开', ng is not None and ng[0] == 'group'
          and ng[1] == '复审无效审查决定')
    seq = [next_nav_action(nav_collapsed, '4W107577', '无效审查决定')[0],
           next_nav_action(nav_expanded, '4W107577', '无效审查决定')[0]]
    check('M2) 下钻三步序 group→case（收起→展开即案件）', seq == ['group', 'case'])
    check('M3) 导航全屏外（0矩形）返回 None',
          next_nav_action([(' 复审无效审查决定', (0, 0, 0, 0))], '4W107577', '') is None)

    # ── 真实两页 OCR（page1/page2 冻结夹具）× 卡片覆盖合并 ──
    try:
        import ocr_fixtures_detail as fx
        # N) doc-ready 锚词：真实决定书 OCR 右栏(x≈708+)命中；
        #    左导航/顶条小字（x<620，19:02 实拍误识形态）被划界排除
        dm = doc_marker(fx.PAGE1)
        check('N1) doc_marker 右栏决定书锚词命中', dm != '')
        check('N2) 左导航/顶条小字不误报',
              doc_marker([('法 律 依 据', (306, 562, 422, 579)),
                          ('中 苤 及 多 苤 专 利', (56, 20, 781, 36))]) == '')
        ocr = co.parse_detail_pages([fx.PAGE1, fx.PAGE2])
        detail = merge_detail(_CARD_FIXTURE, ocr)
        missing = [k for k in DETAIL_REPORT_KEYS if not detail.get(k)]
        check('H) 真实两页+卡片合并 11/12 列（仅缺合议组）',
              len(missing) == 1 and missing[0] == 'panel_members'
              and detail['case_no'] == '4W107577'
              and detail['decision_no'] == '38430'
              and detail['decision_date'] == '20181221'      # OCR 决定日 优先于卡片发文日
              and detail['request_date'] == '20180724'
              and detail['legal_basis'] == '专利法第22条第2款，第22条第3款'
              and detail['application_no'] == '2006100041736'  # 卡片覆盖 OCR 残缺值
              and detail['patentee'] == '远大医药（天津）有限公司'
              and detail['invention_title'] == _CARD_FIXTURE['title']
              and detail['decision_result'] == '维持专利权有效'
              and detail['decision_summary'].startswith('在无效程序中'))

        # I) 要点溢出到第 3 屏：续读 + 合议组收尾 + 重叠行去重
        page3 = [
            ('效 果 应 当 是 权 利 要 求 的 技 术 方 案 相 对 于 最 接 近 现 有 技 术 具 有 的 技 术 效 果 。 但 是 ， 在 无 效 程 序 中 ，',
             (709, 984, 1452, 1002)),   # page2 末行重叠回显 → 应被去重
            ('由 此 重 新 确 定 权 利 要 求 实 际 解 决 的 技 术 问 题 。', (712, 120, 1450, 138)),
            ('合 议 组 组 长 ： 王 一 主 审 员 ： 赵 二 参 审 员 ： 钱 三', (712, 160, 1300, 178)),
            ('京 ICP 备 05069085 号 -14', (700, 1000, 900, 1015)),
        ]
        ocr3 = co.parse_detail_pages([fx.PAGE1, fx.PAGE2, page3])
        check('I) 溢出第3屏：要点续读+合议组收尾+重叠去重',
              ocr3.get('panel_members', '').startswith('合议组组长')
              and '重新确定' in ocr3.get('decision_summary', '')
              and ocr3['decision_summary'].count('技术效果应当是') == 1
              and ocr3['decision_summary'].endswith('由此重新确定权利要求实际解决的技术问题。'))

        # J) 首页签名栏夹具（page1-info.png）补齐第 12 列：12/12 全通
        ocrj = co.parse_detail_pages([fx.PAGE1, fx.PAGE_SIG, fx.PAGE2])
        dj = merge_detail(_CARD_FIXTURE, ocrj)
        missj = [k for k in DETAIL_REPORT_KEYS if not dj.get(k)]
        check('J) 首页签名栏×卡片合并 12/12 列',
              not missj
              and dj['panel_members'].startswith('合议组组长')
              and '主审' in dj['panel_members']
              and '委员会' not in dj['panel_members'])
    except FileNotFoundError as e:
        check('H/I) 夹具缺失', False)
        print(f'    {e}')

    # K) 申请号多通道纠偏：无卡片跑、OCR 残缺、a11y 正确 → 取 a11y 值
    v, note = reconcile_application_no(
        {'application_no': '2010004173、6'},          # OCR 残缺（202133 实况）
        {}, {'application_no': '2006100041736'},
        {'application_no': '2010004173、6'}, '200610004173.6')
    check('K) 纠偏：无卡片+OCR残缺 → a11y 与入参相符者回填',
          v == '2006100041736' and 'a11y' in note)
    v2, note2 = reconcile_application_no(
        {'application_no': '2006100041736'},
        {'app_no': '2006100041736'}, {}, {}, '')
    check('K2) 纠偏：已一致/无入参 → 原样不动',
          v2 == '2006100041736' and note2 == '未对拍(无入参)')
    v3, note3 = reconcile_application_no(
        {'application_no': '2010004173、6'}, {}, {}, {}, '9999999999999')
    check('K3) 纠偏：无通道相符 → 保留原值并标记不一致',
          v3 == '2010004173、6' and note3.startswith('不一致'))

    # N) 契约 §3.1 入参转译 translate_inputs：list_item / library_type /
    #    detail_url 抽参 / 新旧键回退 / card_fields 优先
    li = {'source': 'cnipa', 'review_type': 2, 'case_no': '4W107577',
          'decision_no': '38430', 'issue_date': '2018.12.21',
          'invention_title': '一种晶体制备方法', 'petitioner': '李瑶',
          'patentee': '远大医药（天津）有限公司',
          'application_no': '200610004173.6',
          'decision_result': '维持专利权有效'}
    t1 = translate_inputs({'list_item': li})
    check('N1) list_item→card_fields 转译（invalid 库）',
          t1['lib'] == 'invalid'
          and t1['card_fields'].get('invalid_requester') == '李瑶'
          and t1['card_fields'].get('app_no') == '2006100041736'
          and t1['card_fields'].get('title') == '一种晶体制备方法'
          and t1['card_fields'].get('conclusion') == '维持专利权有效'
          and t1['card_fields'].get('issue_date') == '2018.12.21'
          and t1['anjianbh'] == '4W107577'
          and t1['zhuanlisqh'] == '200610004173.6')
    t2 = translate_inputs({'list_item': {**li, 'review_type': 1,
                                         'petitioner': '某公司'}})
    check('N2) review_type=1 → reexam 库，petitioner→reexam_requester',
          t2['lib'] == 'reexam'
          and t2['card_fields'].get('reexam_requester') == '某公司'
          and not t2['card_fields'].get('invalid_requester'))
    t3 = translate_inputs({
        'library_type': 'reexam',
        'detail_url': 'https://cpquery.cponline.cnipa.gov.cn/detail/index'
                      '?zhuanlisqh=200610004173.6&anjianbh=4W107577&searchType=8'})
    check('N3) detail_url 抽 anjianbh/zhuanlisqh + library_type 显式优先',
          t3['lib'] == 'reexam' and t3['anjianbh'] == '4W107577'
          and t3['zhuanlisqh'] == '200610004173.6'
          and t3['detail_url_arg'].startswith('https://cpquery'))
    t4 = translate_inputs({'application_no': 'CN200610004173.6',
                           'case_no': '4W107577'})
    check('N4) 顶层新键回退（CN 前缀剥除、默认 invalid 库）',
          t4['zhuanlisqh'] == '200610004173.6' and t4['anjianbh'] == '4W107577'
          and t4['lib'] == 'invalid' and t4['detail_url_arg'] == '')
    t5 = translate_inputs({'anjianbh': '4W107577', 'zhuanlisqh': '2006100041736',
                           'card_fields': {'invalid_requester': '李瑶'}})
    check('N5) 旧键入参不受影响',
          t5['anjianbh'] == '4W107577' and t5['zhuanlisqh'] == '2006100041736'
          and t5['card_fields']['invalid_requester'] == '李瑶')
    t6 = translate_inputs({'list_item': li,
                           'card_fields': {'title': '卡片已有标题'}})
    check('N6) list_item 不覆盖 card_fields 已有值（卡片精确值优先）',
          t6['card_fields']['title'] == '卡片已有标题'
          and t6['card_fields'].get('app_no') == '2006100041736')

    # L) _finish 拆分：payload 只剩交付字段，遥测/通道比对全数移交 debug.json
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        demo = {'ok': True, 'detail': {'case_no': 'X'},
                'field_sources': {'case_no': 'ocr'}, 'missing_fields': [],
                'field_count': 12, 'match': {'case_no': True},
                'mode': 'uia', 'run_dir': td, 'elapsed_s': 1.0,
                'shots': ['a.png'], 'channel_diffs': {'card': {}},
                'steps': [{'step': 'x'}], 'tree_flat': 'tree.txt'}
        _finish(demo, td)
        pj = json.load(open(os.path.join(td, 'payload.json'), encoding='utf-8'))
        dj_ = json.load(open(os.path.join(td, 'debug.json'), encoding='utf-8'))
        check('L) payload=交付物：仅 ok/detail/sources/missing/count/match',
              set(pj) == {'ok', 'detail', 'field_sources', 'missing_fields',
                          'field_count', 'match'})
        check('L2) 遥测移交：channel_diffs/mode/shots/steps 在 debug 而非 payload',
              'channel_diffs' in dj_ and 'mode' in dj_ and 'shots' in dj_
              and 'steps' in dj_ and 'channel_diffs' not in pj)

    print('[selftest] OK' if fails == 0 else f'[selftest] {fails} FAIL')
    return 0 if fails == 0 else 1


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        crash_dir = os.path.join(ROOT, 'output', 'uia_runs')
        os.makedirs(crash_dir, exist_ok=True)
        tb = traceback.format_exc()
        with open(os.path.join(crash_dir, 'last_crash.txt'), 'w', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S\n') + tb)
        print('[detail] 未捕获异常（已写 python/output/uia_runs/last_crash.txt）:\n' + tb)
        sys.exit(9)
