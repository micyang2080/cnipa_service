# -*- coding: utf-8 -*-
"""MCP 接口契约层（v2 · 技能侧契约要求对齐）：白名单投影 + 规范化 + 错误分类 + stdout 末行发射。

对照 docs/cnipa-mcp-技能侧契约要求.md（2026-09-03 定稿）：
- 键名与技能 schema 完全一致（petitioner 统一 / application_no 含点 / invention_title…），
  技能 ingest 零映射。
- 日期统一 YYYY.MM.DD；申请号统一 13 位含校验位点。
- items[] / detail 固定键集（缺值给 ""，键必须存在）；每条 case 带 detail_url。
- 错误码新增 FILTER_EMPTY / TOO_MANY_RESULTS / DETAIL_URL_MISSING。

用法（UIA runner 内，_finish 末尾一行）：
    import cnipa_contract
    cnipa_contract.maybe_emit('figure', payload)

规则（见 docs/接口契约.md §4）：
- 环境变量 CNIPA_EMIT_JSON=1 才发射（本地实跑不受影响）。
- 发射的是「契约投影后的干净 JSON」，打印为 stdout 末行；
  Node 层沿用「末行 JSON」协议原样透传。
- 内部遥测（steps/tree_flat/run_dir/img_candidates/…）一律不出边界。

自测（离线）：
    python cnipa_contract.py --selftest
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------- 开关

EMIT_ENV = 'CNIPA_EMIT_JSON'


def emit_enabled() -> bool:
    return os.environ.get(EMIT_ENV, '').strip() == '1'


# ---------------------------------------------------------------- 规范化

BROAD_SEARCH_MAX_ITEMS = 10
SEARCH_NARROW_FILTER_KEYS = (
    'application_no', 'invention_title', 'ipc_class', 'decision_no', 'case_no',
    'patentee', 'petitioner', 'panel_leader', 'panel_examiner', 'panel_member',
    'agency',
)
SEARCH_BROAD_FILTER_KEYS = (
    'patent_type', 'application_date_from', 'application_date_to',
    'issue_date_from', 'issue_date_to',
)


def _nonempty_filter_keys(filters) -> set:
    raw = filters if isinstance(filters, dict) else {}
    keys = set(SEARCH_NARROW_FILTER_KEYS) | set(SEARCH_BROAD_FILTER_KEYS)
    out = set()
    for k in keys:
        if str(raw.get(k) or '').strip():
            out.add(k)
    return out


def is_broad_search_filters(filters) -> bool:
    active = _nonempty_filter_keys(filters)
    if not active:
        return False
    return not any(k in SEARCH_NARROW_FILTER_KEYS for k in active)


def effective_search_max_items(filters, max_items):
    try:
        n = int(max_items) if max_items is not None else 50
    except (TypeError, ValueError):
        n = 50
    n = min(max(1, n), 500)
    if is_broad_search_filters(filters):
        return min(n, BROAD_SEARCH_MAX_ITEMS), True
    return n, False


def dotted_appno(value):
    """申请号规范化为 13 位含校验位点（如 200610004173.6）。

    接受：CN/ZL 前缀、含点/不含点、两侧空白；无法识别时原样去空白返回。
    """
    s = str(value or '').strip().upper().replace('，', '').replace(',', '')
    s = re.sub(r'^(CN|ZL)', '', s).strip()
    m = re.fullmatch(r'(\d{12})\.?(\d)', s)          # 13 位（点可有可无）
    if m:
        return '%s.%s' % (m.group(1), m.group(2))
    m = re.fullmatch(r'(\d{8})\.?(\d)', s)           # 老式 9 位
    if m:
        return '%s.%s' % (m.group(1), m.group(2))
    if re.fullmatch(r'\d{13}', s):                   # cpquery 列表/详情顶栏无点：12 流水+校验位
        return '%s.%s' % (s[:12], s[12])
    return s


def undotted_appno(value):
    """去掉点号（站点输入框/卡片常用无点形态）。"""
    return str(value or '').strip().replace('.', '')


def fmt_date(value):
    """日期统一 YYYY.MM.DD。

    接受：20181228 / 2018-12-28 / 2018.12.28 / 2018/12/28 /
    「2024 年 07 月 03 日」「2025年1月20日」。
    """
    s = str(value or '').strip()
    if not s:
        return ''
    s_ns = re.sub(r'\s+', '', s)
    m = re.fullmatch(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?', s_ns)
    if m:
        return '%s.%02d.%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r'(\d{4})(\d{2})(\d{2})', s_ns)
    if m:
        return '%s.%s.%s' % (m.group(1), m.group(2), m.group(3))
    return s


def fmt_panel_members(value):
    """合议组统一：合议组组长：甲；主审员：乙；参审员：丙。

    兼容 OCR 堆叠「合议组组长任晓兰主审员刘婷婷参审员刘静」、
    a11y「合议组组长：熊婷；主审员：吴士芬；参审员：杜宇」、
    「任晓兰（组长）刘婷婷（主审）刘静（参审）」。
    """
    s = re.sub(r'\s+', '', str(value or '').strip())
    if not s:
        return ''
    for a, b in (('王审员', '主审员'), ('三审员', '主审员'), ('叁审员', '主审员'),
                 ('厶议组', '合议组'), ('合议纟且', '合议组')):
        s = s.replace(a, b)
    s = s.replace(':', '：').replace(';', '；')

    def _name(x: str) -> str:
        return re.sub(r'[^\u4e00-\u9fff]', '', x or '')

    roles = {}
    m = re.search(
        r'合议组组长[：:]?(?P<a>.+?)主审员?[：:]?(?P<b>.+?)参审员?[：:]?(?P<c>.+)$', s)
    if m:
        roles = {'议组组长': _name(m.group('a')), '主审': _name(m.group('b')),
                 '参审': _name(m.group('c'))}
    if not roles.get('议组组长'):
        m2 = re.search(
            r'(?P<a>[^（(]{1,4})[（(]组长[）)]'
            r'(?P<b>[^（(]{1,4})[（(]主审[）)]'
            r'(?P<c>[^（(]{1,4})[（(]参审[）)]', s)
        if m2:
            roles = {'议组组长': _name(m2.group('a')), '主审': _name(m2.group('b')),
                     '参审': _name(m2.group('c'))}
    if sum(1 for k in ('议组组长', '主审', '参审') if roles.get(k)) < 2:
        # 已带冒号的分行/分号串：逐段拆
        for part in re.split(r'[；;、]', s):
            part = part.strip()
            if '组长' in part:
                roles['议组组长'] = _name(re.sub(r'^.*组长[：:]?', '', part))
            elif '主审' in part:
                roles['主审'] = _name(re.sub(r'^.*主审员?[：:]?', '', part))
            elif '参审' in part:
                roles['参审'] = _name(re.sub(r'^.*参审员?[：:]?', '', part))
    if sum(1 for k in ('议组组长', '主审', '参审') if roles.get(k)) < 2:
        return str(value or '').strip()
    bits = []
    if roles.get('议组组长'):
        bits.append('合议组组长：' + roles['议组组长'])
    if roles.get('主审'):
        bits.append('主审员：' + roles['主审'])
    if roles.get('参审'):
        bits.append('参审员：' + roles['参审'])
    return '；'.join(bits)


# ---------------------------------------------------------------- 白名单

# search 卡片「输入键」＝runner 内部旧键（与 run_cnipa_search_decisions_uia.py
# KEY_MAP 对拍，见 _selftest）。投影层负责改名到技能 schema 新键。
CARD_KEYS = (
    'patent_type', 'title',
    'case_no', 'decision_no', 'issue_date', 'app_no', 'patentee',
    'app_date', 'class_no', 'conclusion',
    'invalid_requester', 'reexam_requester',
    'agency',
)

# items[] / detail 固定输出键集（技能 schema 权威键名；缺值给 ""，键必须存在）
# v2.1：新增 decision_file_url → 21 键
CASE_OUT_KEYS = (
    'source', 'review_type',
    'case_no', 'decision_no',
    'issue_date', 'decision_date',
    'invention_title', 'petitioner', 'patentee', 'application_no',
    'request_date', 'legal_basis', 'decision_summary', 'decision_result',
    'panel_members', 'figure_path', 'detail_url',
    'patent_type', 'ipc_class', 'agency',
    'decision_file_url',
)

# detail 12 列（与 run_cnipa_fetch_decision_detail_uia.py DETAIL_REPORT_KEYS 对拍）
DETAIL_KEYS = (
    'case_no', 'decision_no', 'decision_date', 'invention_title',
    'petitioner', 'patentee', 'application_no', 'request_date',
    'legal_basis', 'decision_summary', 'decision_result', 'panel_members',
)

# status 工具字段
STATUS_KEYS = (
    'success', 'chrome_found', 'a11y_ok', 'logged_in', 'ready',
    'cpquery_front', 'cpquery_usable',
    'window_titles', 'message',
)

# login 工具字段（密码绝不入白名单——selftest 有泄漏回归断言）
LOGIN_KEYS = (
    'success', 'logged_in', 'already', 'account_configured',
    'username_masked', 'message',
)

# 绝不外泄的内部遥测键（黑名单兜底；白名单投影本身已排除它们）
# 注：logged_in 在 status 工具是契约字段，故不在黑名单里
TELEMETRY_KEYS = (
    'mode', 'run_dir', 'input_file', 'started_at', 'finished_at',
    'steps', 'img_candidates', 'total_before', 'window_title',
    'screenshot', 'tree_flat', 'ocr_pages', 'cleanup_ok',
    'elapsed_s', 'app_no_match', 'case_no_match', 'note',
    'results', 'total_after', 'library_radio', 'result_count', 'ok',
    'field_sources', 'missing_fields', 'field_count', 'detail_raw',
    'max_items', 'page', 'detail_seen',
)

# 旧键 → 新键（items[] 投影改名表；见技能侧契约 §2.2 废弃清单）
_CARD_RENAME = {
    'title': 'invention_title',
    'app_no': 'application_no',
    'conclusion': 'decision_result',
    'class_no': 'ipc_class',
    'invalid_requester': 'petitioner',
    'reexam_requester': 'petitioner',
    'app_date': '_app_date',   # 列表不输出申请日（不在 12 列），仅内部用
}

_REVIEW_TYPE = {'invalid': 2, 'invalidation': 2, '无效': 2, '无效决定库': 2,
                'reexam': 1, '复审': 1, '复审决定库': 1}


def review_type_of(library):
    v = str(library or '').strip().lower()
    return _REVIEW_TYPE.get(v, 2)


def build_detail_url(application_no, case_no):
    """按技能侧契约模板构造详情链接（无法构造时返回 ''）。"""
    app = dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return ''
    return ('https://cpquery.cponline.cnipa.gov.cn/detail/index'
            '?zhuanlisqh=%s&anjianbh=%s&searchType=8' % (app, case))


FILE_ROOT_ENV = 'CNIPA_FILE_ROOT'
FILE_URL_PREFIX_ENV = 'CNIPA_FILE_URL_PREFIX'
DEFAULT_FILE_ROOT = r'D:\cnipa_file'
DEFAULT_FILE_URL_PREFIX = '/cnipa_file'


def file_root():
    return os.environ.get(FILE_ROOT_ENV, DEFAULT_FILE_ROOT)


def file_url_prefix():
    return os.environ.get(FILE_URL_PREFIX_ENV, DEFAULT_FILE_URL_PREFIX).rstrip('/')


def build_decision_file_rel_path(application_no, case_no):
    app = dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return ''
    return '%s/%s.pdf' % (app, case)


def build_decision_file_url(application_no, case_no):
    rel = build_decision_file_rel_path(application_no, case_no)
    if not rel:
        return ''
    return '%s/%s' % (file_url_prefix(), rel)


def decision_file_disk_path(application_no, case_no):
    rel = build_decision_file_rel_path(application_no, case_no)
    if not rel:
        return ''
    return os.path.join(file_root(), *rel.split('/'))


def project_case(card, library='invalid'):
    """runner 旧卡片键 → 技能 schema 固定 21 键（缺值 ""）。"""
    raw = card if isinstance(card, dict) else {}
    out = {k: '' for k in CASE_OUT_KEYS}
    out['source'] = 'cnipa'
    out['review_type'] = review_type_of(library)
    for k, v in raw.items():
        if v is None:
            continue
        key = _CARD_RENAME.get(k, k)
        if key.startswith('_') or key not in out:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[key] = v
    # 请求人：无效宣告请求人 / 复审请求人 → petitioner（前者优先，按库取）
    if not str(out.get('petitioner') or '').strip():
        alt = raw.get('reexam_requester') if out['review_type'] == 1 else raw.get('invalid_requester')
        out['petitioner'] = str(alt or '').strip()
    # 规范化
    out['application_no'] = dotted_appno(out.get('application_no'))
    out['issue_date'] = fmt_date(out.get('issue_date'))
    out['decision_date'] = ''            # 列表无决定日，详情补
    out['request_date'] = fmt_date(out.get('request_date'))
    if str(out.get('panel_members') or '').strip():
        out['panel_members'] = fmt_panel_members(out.get('panel_members'))
    out['figure_path'] = ''              # 附图工具写入
    if not str(out.get('decision_file_url') or '').strip():
        out['decision_file_url'] = ''
    out['detail_url'] = build_detail_url(out.get('application_no'), out.get('case_no'))
    return out


# ---------------------------------------------------------------- 错误分类

# (code, 匹配子串列表)——按序首个命中者胜；文本取 payload['error']
# WINDOW_NOT_FOUND 先于 A11Y：勿用启动参数文案误伤归类
_ERROR_RULES = (
    ('WINDOW_NOT_FOUND', ('未找到', '找不到', '窗口')),
    ('A11Y_DISABLED', ('辅助功能', '无障碍树未', '内容通道未就绪', 'a11y')),
    # NEED_CREDENTIALS 在 NOT_LOGGED_IN 之前：前者=文件没账密（要用户在会话里给），
    # 后者=浏览器没登录态（要跑自动登录）。
    ('NEED_CREDENTIALS', ('未配置凭证', '缺少账号密码', 'NEED_CREDENTIALS')),
    ('CAPTCHA_FAIL', ('滑块', '拼图', '验证未通过', 'CAPTCHA')),
    ('AUTH_REJECTED', ('密码错误', '账号或密码', '用户名或密码', '账号不存在',
                       '登录被拒', '已被锁定')),
    ('NOT_LOGGED_IN', ('未登录', '登录已过期', '会话过期', '请先登录',
                       '登录墙', '登录表单未出现', '自动登录失败',
                       '仍停在登录页', '仍未进检索页')),
    ('FILTER_EMPTY', ('筛选条件为空', '至少一个筛选', 'FILTER_EMPTY')),
    ('TOO_MANY_RESULTS', ('结果过多', '超出上限', 'TOO_MANY_RESULTS')),
    ('DETAIL_URL_MISSING', ('无法构造详情链接', 'DETAIL_URL_MISSING')),
    ('NO_FIGURE', ('附图',)),
    ('NO_RESULT', ('无结果', '没有查询到', '未查询到', '共查询到 0', 'NO_RESULT')),
    ('DECISION_FILE_FAIL', ('决定文书', 'DECISION_FILE_FAIL', 'print_pdf',
                            'merge_images', 'saveas_images', '打印 PDF',
                            'PDF 未在', 'PDF 文件未')),
    ('INVALID_INPUT', ('输入', '参数', '申请号')),
    ('TIMEOUT', ('超时', 'timeout', 'Timeout')),
    ('PARSE_FAIL', ('解析', '字段', '页面结构')),
)

HINTS = {
    'WINDOW_NOT_FOUND': '服务暂时无法完成查询，请稍后重试。',
    'A11Y_DISABLED': '服务暂时无法完成查询，请稍后重试。',
    'NOT_LOGGED_IN': '请稍后重试；若持续失败可调用 cnipa_login。',
    'FILTER_EMPTY': 'filters 至少需要一个非空条件。',
    'TOO_MANY_RESULTS': '命中过多，结果已截断（truncated=true）。可收紧条件或调大 max_items（上限 10）。',
    'DETAIL_URL_MISSING': '缺少申请号或案件编号，无法定位该条详情。',
    'NO_FIGURE': '该专利可能无摘要附图。可按缺图处理。',
    'NO_RESULT': '未查询到符合条件的决定，请核对申请号/案件编号。',
    'DECISION_FILE_FAIL': '决定文书 PDF 获取失败，可稍后重试或 force_refresh=true。不影响其它字段。',
    'INVALID_INPUT': '请检查入参（申请号如 200610004173.6）。',
    'NEED_CREDENTIALS': '首次使用需提供国知局账号与密码；请在对话中提供后重试，'
                        '或调用 cnipa_login 传入 username/password。',
    'CAPTCHA_FAIL': '登录验证未通过，请稍后重试。',
    'AUTH_REJECTED': '账号或密码不正确，请确认后通过 cnipa_login 更新。',
    'TIMEOUT': '请求超时，请稍后重试。',
    'PARSE_FAIL': '结果解析失败，请稍后重试。',
    'RUNTIME_FAIL': '运行失败，请稍后重试。',
}


def classify_error(text):
    """把 runner 的 error 文本归类为契约错误码。"""
    if not text:
        return 'RUNTIME_FAIL'
    for code, words in _ERROR_RULES:
        for w in words:
            if w in text:
                return code
    return 'RUNTIME_FAIL'


def _error_payload(payload):
    """从失败 payload 提取契约错误结构。"""
    raw = payload.get('error') or payload.get('message') or 'unknown error'
    code = classify_error(str(raw))
    out = {
        'success': False,
        'error': code,
        'message': str(raw)[:500],
        'hint': HINTS[code],
    }
    return out


# ---------------------------------------------------------------- 投影

def _clean_card(item):
    if not isinstance(item, dict):
        return None
    out = {}
    for k in CARD_KEYS:
        v = item.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue  # 「有值才出」
        out[k] = v
    return out or None


def clean_search(payload):
    if not payload.get('ok'):
        return _error_payload(payload)
    library = payload.get('library_radio') or 'invalid'
    items_raw = payload.get('results') or []
    items = [project_case(c, library) for c in items_raw if isinstance(c, dict)]
    total = payload.get('total_after')
    if not isinstance(total, int) or total < 0:
        total = len(items)
    truncated = bool(payload.get('truncated')) or total > len(items)
    out = {
        'success': True,
        # 回显规范化 token（runner 的 library_radio 是中文单选名）
        'library_type': 'reexam' if review_type_of(library) == 1 else 'invalid',
        'total': total,
        'truncated': truncated,
        'items': items,
    }
    if payload.get('cache_file'):
        out['cache_file'] = str(payload['cache_file'])
    if 'from_cache' in payload:
        out['from_cache'] = bool(payload['from_cache'])
    return out


def clean_details_batch(payload):
    if not payload.get('details') and not payload.get('ok'):
        return _error_payload(payload)
    details = []
    for d in payload.get('details') or []:
        if isinstance(d, dict):
            details.append(d)
    out = {
        'success': bool(details),
        'count': len(details),
        'requested': payload.get('requested'),
        'details': details,
    }
    if payload.get('failed'):
        out['failed'] = payload['failed']
    if payload.get('detail_cap') is not None:
        out['detail_cap'] = payload['detail_cap']
    if not details:
        err = payload.get('error') or '批量详情未取到任何一条'
        out.update(_error_payload({'error': err}))
        out['success'] = False
    return out


def clean_detail(payload):
    if not payload.get('ok') or not payload.get('detail'):
        return _error_payload(payload)
    detail = dict(payload['detail'])
    library = payload.get('library_radio') or detail.get('library_type') or 'invalid'
    # 12 列以 project_case 统一规范化（日期/申请号/固定键集），再补 detail 层字段
    out = project_case(detail, library)
    out['decision_date'] = fmt_date(detail.get('decision_date') or detail.get('issue_date'))
    out['request_date'] = fmt_date(
        out.get('request_date') or detail.get('request_date'))
    out['panel_members'] = fmt_panel_members(
        out.get('panel_members') or detail.get('panel_members'))
    out['detail_url'] = str(detail.get('detail_url') or '') or out['detail_url']
    url = str(detail.get('decision_file_url') or '').strip()
    if url:
        out['decision_file_url'] = url
    out2 = {'success': True, 'detail': out}
    if payload.get('cache_file'):
        out2['cache_file'] = str(payload['cache_file'])
    if 'from_cache' in payload:
        out2['from_cache'] = bool(payload['from_cache'])
    return out2


def clean_figure(payload):
    if not payload.get('ok'):
        return _error_payload(payload)
    app = dotted_appno(payload.get('application_no') or payload.get('search_key'))
    out = {
        'success': True,
        'application_no': app,
    }
    if payload.get('figure_path'):
        out['figure_path'] = payload['figure_path']
    if payload.get('method'):
        out['method'] = payload['method']
    if payload.get('cache_file'):
        out['cache_file'] = str(payload['cache_file'])
    if 'from_cache' in payload:
        out['from_cache'] = bool(payload['from_cache'])
    return out


def clean_decision_file(payload):
    if not payload.get('ok'):
        return _error_payload(payload)
    app = dotted_appno(payload.get('application_no'))
    case = str(payload.get('case_no') or '').strip()
    out = {
        'success': True,
        'application_no': app,
        'case_no': case,
    }
    if payload.get('decision_file_url'):
        out['decision_file_url'] = payload['decision_file_url']
    if payload.get('method'):
        out['method'] = payload['method']
    if payload.get('cache_file'):
        out['cache_file'] = str(payload['cache_file'])
    if 'from_cache' in payload:
        out['from_cache'] = bool(payload['from_cache'])
    return out


def clean_status(payload):
    out = {k: payload[k] for k in STATUS_KEYS if k in payload}
    out['success'] = bool(payload.get('ready'))
    if not out['success'] and 'message' not in out:
        out['message'] = '环境未就绪，见 hint 各字段'
    return out


def clean_login(payload):
    """login 工具白名单投影。密码/遥测一律不出边界（runner 侧也不该写）。"""
    if not payload.get('ok'):
        return _error_payload(payload)
    out = {'success': True}
    for k in ('logged_in', 'already', 'account_configured', 'username_masked'):
        if k in payload:
            out[k] = payload[k]
    return out


_CLEANERS = {
    'search': clean_search,
    'detail': clean_detail,
    'details_batch': clean_details_batch,
    'figure': clean_figure,
    'decision_file': clean_decision_file,
    'status': clean_status,
    'login': clean_login,
}


# ---------------------------------------------------------------- 发射

def maybe_emit(tool, payload):
    """runner 终态钩子：若开启 CNIPA_EMIT_JSON，投影后打印为 stdout 末行。

    返回发射的 dict（未发射时返回 None）。异常绝不向上抛——
    发射失败不应影响 runner 落盘主流程。
    """
    if not emit_enabled():
        return None
    try:
        cleaner = _CLEANERS.get(tool)
        out = cleaner(payload) if cleaner else {'success': False, 'error': 'RUNTIME_FAIL', 'message': 'unknown tool: %s' % tool}
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        return out
    except Exception as e:  # pragma: no cover - 防御性
        try:
            fallback = {'success': False, 'error': 'RUNTIME_FAIL', 'message': 'emit failed: %s' % e}
            print(json.dumps(fallback, ensure_ascii=False))
            sys.stdout.flush()
        except Exception:
            pass
        return None


# ------------------------------------------------------- 持久缓存写（§8 落点）
# docs/cnipa-mcp-本地缓存要求.md：search/detail 成功后写 search//detail/ 路径，
# 返回绝对路径（即响应 cache_file 指向的对象）。lazy import 防循环依赖
# （cnipa_cache 顶层 import 本模块的 dotted_appno/review_type_of）。

def write_search_cache(library_type, filters, max_items, payload):
    """检索成功：clean_search 投影 → search/{lib}/{query_key}.json；失败/禁用→None。

    缓存文件内容 = 线上契约 JSON 本体（不含 from_cache/cache_file——
    两者是响应级扩展，命中时由调用方附加）。"""
    try:
        import cnipa_cache
        if cnipa_cache.cache_disabled():
            return None
        out = clean_search(payload)
        if out.get('success') is not True:
            return None
        out.pop('from_cache', None)
        out.pop('cache_file', None)
        return cnipa_cache.write_json(
            cnipa_cache.search_path(library_type, filters, max_items), out)
    except Exception:
        return None


def write_detail_cache(library_type, application_no, case_no, payload):
    """详情成功：clean_detail 投影 → detail/{lib}/{app}__{case}.json。

    门禁：application_no 与 case_no 双号齐全才写（§3.3「仍建议两端都有再写缓存」；
    缺侧文件名虽可 `_` 占位，但缺号对拍无从谈起）。"""
    try:
        import cnipa_cache
        if cnipa_cache.cache_disabled():
            return None
        app = dotted_appno(application_no)
        case = str(case_no or '').strip()
        if not app or not case:
            return None
        out = clean_detail(payload)
        if out.get('success') is not True:
            return None
        out.pop('from_cache', None)
        out.pop('cache_file', None)
        return cnipa_cache.write_json(
            cnipa_cache.detail_path(library_type, app, case), out)
    except Exception:
        return None


def emit_dict(out):
    """缓存命中短路用：直接发射**已投影**的契约 dict（不经 runner _finish）。

    与 maybe_emit 同协议（stdout 末行 + 开关门禁），异常不向上抛。"""
    if not emit_enabled():
        return None
    try:
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- 自测

def _selftest():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 1) 与 runner 源码对拍：CARD_KEYS ⊆ KEY_MAP.values()+两固定键
    here = os.path.dirname(os.path.abspath(__file__))
    src_search = os.path.join(here, 'run_cnipa_search_decisions_uia.py')
    src_detail = os.path.join(here, 'run_cnipa_fetch_decision_detail_uia.py')
    if os.path.exists(src_search):
        m = re.search(r'KEY_MAP\s*=\s*\{(.*?)\}', open(src_search, encoding='utf-8').read(), re.S)
        check(m is not None, 'search 源码未找到 KEY_MAP')
        if m:
            mapped = set(re.findall(r"'([^']+)'", m.group(1)))
            expected = set(CARD_KEYS) - {'patent_type', 'title', 'agency'}
            check(expected <= mapped, 'CARD_KEYS 与 KEY_MAP 不一致: %s' % (expected - mapped))
    if os.path.exists(src_detail):
        m = re.search(r'DETAIL_REPORT_KEYS\s*=\s*[\[(](.*?)[\])]', open(src_detail, encoding='utf-8').read(), re.S)
        check(m is not None, 'detail 源码未找到 DETAIL_REPORT_KEYS')
        if m:
            mapped = set(re.findall(r"'([^']+)'", m.group(1)))
            check(mapped == set(DETAIL_KEYS), 'DETAIL_KEYS 与源码不一致: %s' % (set(DETAIL_KEYS) ^ mapped))

    # 2) 规范化：dotted_appno / fmt_date
    check(dotted_appno('2006100041736') == '200610004173.6', 'dotted_appno 无点→含点失败')
    check(dotted_appno('CN200610004173.6') == '200610004173.6', 'dotted_appno CN 前缀失败')
    check(dotted_appno('ZL200610004173.6') == '200610004173.6', 'dotted_appno ZL 前缀失败')
    check(dotted_appno(' 200610004173.6 ') == '200610004173.6', 'dotted_appno 空白失败')
    check(dotted_appno('8812345.6') == '8812345.6', 'dotted_appno 9位失败')
    check(dotted_appno('2020102008342') == '202010200834.2',
          'dotted_appno cpquery无点13位失败')
    check(dotted_appno('') == '', 'dotted_appno 空值失败')
    check(fmt_date('20181228') == '2018.12.28', 'fmt_date 紧凑失败')
    check(fmt_date('2018-12-28') == '2018.12.28', 'fmt_date 横线失败')
    check(fmt_date('2018.12.28') == '2018.12.28', 'fmt_date 点号失败')
    check(fmt_date('2024 年 07 月 03 日') == '2024.07.03', 'fmt_date 中文带空格失败')
    check(fmt_date('2025年1月20日') == '2025.01.20', 'fmt_date 中文无补零失败')
    check(fmt_date('') == '', 'fmt_date 空值失败')
    check(fmt_panel_members('合议组组长任晓兰主审员刘婷婷参审员刘静')
          == '合议组组长：任晓兰；主审员：刘婷婷；参审员：刘静',
          'fmt_panel_members 堆叠失败')
    check(fmt_panel_members('合议组组长：熊婷；主审员：吴士芬；参审员：杜宇')
          == '合议组组长：熊婷；主审员：吴士芬；参审员：杜宇',
          'fmt_panel_members 已规范串失败')

    # 3) search 投影：固定 21 键 + 改名 + 规范化 + detail_url + truncated
    s = clean_search({
        'ok': True, 'library_radio': 'invalid', 'total_after': 3,
        'results': [
            {'patent_type': '发明专利', 'title': '吡啶酮衍生物', 'case_no': '4W107577',
             'decision_no': '38430', 'issue_date': '20181221',
             'app_no': 'CN2006100041736', 'patentee': '远大医药（天津）有限公司',
             'class_no': 'A61K31/137', 'conclusion': '维持专利权有效',
             'invalid_requester': '李瑶', 'steps': ['x']},
            {'title': '另一件', 'case_no': ''},   # 无申请号/案件编号 → detail_url ''
        ],
        'steps': ['tel'], 'run_dir': '/tmp/x',
    })
    check(set(s.keys()) == {'success', 'library_type', 'total', 'truncated', 'items'},
          'search 顶层键不符: %s' % s.keys())
    check(s['total'] == 3 and s['truncated'] is True, 'search total/truncated 不符: %r/%r'
          % (s['total'], s['truncated']))
    it = s['items'][0]
    check(tuple(it.keys()) == CASE_OUT_KEYS, 'items 键集/顺序不符: %s' % list(it.keys()))
    check(it['source'] == 'cnipa' and it['review_type'] == 2, 'source/review_type 不符')
    check(it['invention_title'] == '吡啶酮衍生物', 'title→invention_title 失败')
    check(it['application_no'] == '200610004173.6', 'application_no 含点失败: %s' % it['application_no'])
    check(it['petitioner'] == '李瑶', 'invalid_requester→petitioner 失败')
    check(it['decision_result'] == '维持专利权有效', 'conclusion→decision_result 失败')
    check(it['ipc_class'] == 'A61K31/137', 'class_no→ipc_class 失败')
    check(it['issue_date'] == '2018.12.21', 'issue_date 日期格式失败: %s' % it['issue_date'])
    check(it['decision_date'] == '' and it['figure_path'] == ''
          and it['decision_file_url'] == '',
          '列表 decision_date/figure_path/decision_file_url 应为空')
    check(it['detail_url'] == ('https://cpquery.cponline.cnipa.gov.cn/detail/index'
                               '?zhuanlisqh=200610004173.6&anjianbh=4W107577&searchType=8'),
          'detail_url 构造不符: %s' % it['detail_url'])
    check('reexam_requester' not in it and 'app_no' not in it and 'title' not in it
          and 'conclusion' not in it, '废弃旧键仍出现在 items: %s' % list(it.keys()))
    check(s['items'][1]['detail_url'] == '', '缺申请号时 detail_url 应为空')
    check('steps' not in s and 'run_dir' not in s, 'search 泄漏遥测')

    # 3b) reexam 库：中文单选名 → review_type=1 + library_type 回显规范 token
    s2 = clean_search({'ok': True, 'library_radio': '复审决定库', 'total_after': 1,
                       'results': [{'title': 'T', 'case_no': '1F123456', 'app_no': '200610004173.6',
                                    'reexam_requester': '王某', 'issue_date': '2020-01-02'}]})
    it2 = s2['items'][0]
    check(it2['review_type'] == 1 and it2['petitioner'] == '王某', 'reexam 库 petitioner 失败: %r' % it2)
    check(it2['issue_date'] == '2020.01.02', 'reexam issue_date 格式失败')
    check(s2['library_type'] == 'reexam', 'library_type 应回显规范 token: %s' % s2['library_type'])

    # 3c) 全命中无截断
    s3 = clean_search({'ok': True, 'library_radio': 'invalid', 'total_after': 1,
                       'results': [{'title': 'T', 'case_no': 'C', 'app_no': '200610004173.6'}]})
    check(s3['truncated'] is False, '全命中不应 truncated')

    # 4) detail 投影：固定键集 + 决定日回退 + detail_url
    d = clean_detail({
        'ok': True, 'library_radio': 'invalid',
        'detail': {'case_no': '4W107577', 'decision_no': '38430',
                   'decision_date': '20181221', 'invention_title': 'T', 'petitioner': '李瑶',
                   'patentee': 'P', 'application_no': 'CN2006100041736',
                   'request_date': '2018-07-24', 'legal_basis': '专利法第22条第2款',
                   'decision_summary': '……决定要点……', 'decision_result': '维持专利权有效',
                   'panel_members': '任晓兰（组长）刘婷婷（主审）刘静（参审）',
                   'field_count': 12},
    })
    dt = d['detail']
    check(d['success'] is True and tuple(dt.keys()) == CASE_OUT_KEYS,
          'detail 键集不符: %s' % list(dt.keys()))
    check(dt['application_no'] == '200610004173.6', 'detail application_no 含点失败')
    check(dt['decision_date'] == '2018.12.21', 'detail decision_date 格式失败')
    check(dt['request_date'] == '2018.07.24', 'detail request_date 格式失败')
    check(dt['panel_members']
          == '合议组组长：任晓兰；主审员：刘婷婷；参审员：刘静',
          'detail panel_members 格式失败')
    check(dt['source'] == 'cnipa' and dt['review_type'] == 2, 'detail source/review_type 失败')
    check(dt['detail_url'].endswith('anjianbh=4W107577&searchType=8'), 'detail detail_url 失败')
    check(dt['figure_path'] == '' and dt['decision_file_url'] == '',
          'detail figure_path/decision_file_url 应为空串')
    # 决定日缺失 → 回退发文日
    d2 = clean_detail({'ok': True, 'detail': {'case_no': 'C', 'application_no': '200610004173.6',
                                              'issue_date': '2018.12.21', 'decision_result': 'R'}})
    check(d2['detail']['decision_date'] == '2018.12.21', 'decision_date 未回退 issue_date')
    d3 = clean_detail({
        'ok': True, 'library_radio': 'invalid',
        'detail': {'case_no': '4W107577', 'application_no': '200610004173.6',
                   'decision_file_url': '/cnipa_file/200610004173.6/4W107577.pdf'},
    })
    check(d3['detail']['decision_file_url'] == '/cnipa_file/200610004173.6/4W107577.pdf',
          'detail decision_file_url 透传失败')
    check(build_decision_file_url('CN2006100041736', '4W107577').endswith(
        '/cnipa_file/200610004173.6/4W107577.pdf'), 'build_decision_file_url 不符')
    check(len(CASE_OUT_KEYS) == 21, 'CASE_OUT_KEYS 应为 21 键')
    check(effective_search_max_items({'patent_type': 'x'}, 500) == (10, True),
          '宽泛检索应钳制 10')
    check(effective_search_max_items({'application_no': '1'}, 50) == (50, False),
          '精确检索不应钳制')

    # 5) figure 投影：application_no 含点回显
    f = clean_figure({'ok': True, 'application_no': 'CN2006100041736',
                      'figure_path': r'C:\x.png', 'method': 'saveas', 'steps': [1]})
    check(f == {'success': True, 'application_no': '200610004173.6',
                'figure_path': 'C:\\x.png', 'method': 'saveas'}, 'figure 投影不符: %r' % f)

    # 6) 错误分类全码覆盖（含三个新码）
    cases = [
        ('未找到目标 Chrome 窗口', 'WINDOW_NOT_FOUND'),
        ('Chrome 辅助功能未启用', 'A11Y_DISABLED'),
        ('未配置凭证：account.ini 缺少账号密码', 'NEED_CREDENTIALS'),
        ('滑块验证未通过（尝试 3 次）', 'CAPTCHA_FAIL'),
        ('登录被拒: 账号或密码错误', 'AUTH_REJECTED'),
        ('检测到未登录', 'NOT_LOGGED_IN'),
        ('筛选条件为空：filters 至少一个非空', 'FILTER_EMPTY'),
        ('结果过多：共 800 条超出上限', 'TOO_MANY_RESULTS'),
        ('无法构造详情链接', 'DETAIL_URL_MISSING'),
        ('详情页未检测到摘要附图区域', 'NO_FIGURE'),
        ('共查询到 0 条', 'NO_RESULT'),
        ('页面解析失败', 'PARSE_FAIL'),
        ('操作超时', 'TIMEOUT'),
        ('随机字符串错误', 'RUNTIME_FAIL'),
    ]
    for text, want in cases:
        got = classify_error(text)
        check(got == want, '错误分类 %r → %s（期望 %s）' % (text, got, want))

    # 7) 失败 payload → 契约错误结构（含 hint）
    e = clean_figure({'ok': False, 'error': '未找到目标 Chrome 窗口'})
    check(e == {'success': False, 'error': 'WINDOW_NOT_FOUND',
                'message': '未找到目标 Chrome 窗口',
                'hint': HINTS['WINDOW_NOT_FOUND']}, 'figure 失败投影不符: %r' % e)
    check('application_no' not in e, 'figure 失败结构不应含 application_no')

    # 8) 发射开关默认关
    os.environ.pop(EMIT_ENV, None)
    check(maybe_emit('figure', {'ok': True}) is None, '未设开关时不应发射')
    check(emit_dict({'success': True}) is None, 'emit_dict 未设开关时不应发射')
    os.environ[EMIT_ENV] = '1'
    r = maybe_emit('figure', {'ok': True, 'application_no': 'X', 'figure_path': 'p', 'method': 'saveas'})
    check(r is not None and r['success'] is True, '开关开启后应发射')
    r2 = emit_dict({'success': True, 'from_cache': True})
    check(r2 is not None and r2['from_cache'] is True, 'emit_dict 开关开启后应发射')
    del os.environ[EMIT_ENV]

    # 9) 遥测黑名单与白名单零交集（双保险）
    leak = set(TELEMETRY_KEYS) & (set(CARD_KEYS) | set(CASE_OUT_KEYS) | set(DETAIL_KEYS)
                                  | set(STATUS_KEYS) | set(LOGIN_KEYS)
                                  | {'figure_path', 'method', 'application_no', 'library_type',
                                     'total', 'truncated', 'items', 'detail', 'error',
                                     'message', 'hint', 'cache_file', 'from_cache',
                                     'source', 'success', 'detail_url', 'review_type'})
    check(not leak, '黑白名单冲突: %s' % leak)

    # 9b) 缓存扩展字段透传（本地缓存要求 §5）：from_cache/cache_file 只在
    #     payload 带了才出现在投影里；缓存写入包装会剥掉它们（见 9c）
    sp = clean_search({'ok': True, 'library_radio': 'invalid', 'total_after': 0,
                       'results': [], 'cache_file': r'C:\c\s.json', 'from_cache': True})
    check(sp.get('cache_file') == r'C:\c\s.json' and sp.get('from_cache') is True,
          'search from_cache/cache_file 透传失败: %r' % sp)
    check('cache_file' not in s and 'from_cache' not in s,
          '未带缓存字段的 search 投影不应凭空出现它们')
    dp3 = clean_detail({'ok': True, 'library_radio': 'invalid',
                        'detail': {'case_no': '4W107577', 'application_no': '200610004173.6'},
                        'cache_file': r'C:\c\d.json', 'from_cache': True})
    check(dp3.get('cache_file') == r'C:\c\d.json' and dp3.get('from_cache') is True,
          'detail from_cache/cache_file 透传失败: %r' % dp3)
    check('cache_file' not in d and 'from_cache' not in d,
          '未带缓存字段的 detail 投影不应凭空出现它们')
    fp3 = clean_figure({'ok': True, 'application_no': '200610004173.6',
                        'figure_path': 'p', 'method': 'cache',
                        'cache_file': 'p2', 'from_cache': True})
    check(fp3.get('cache_file') == 'p2' and fp3.get('from_cache') is True,
          'figure from_cache/cache_file 透传失败: %r' % fp3)
    check('cache_file' not in f and 'from_cache' not in f,
          '未带缓存字段的 figure 投影不应凭空出现它们')

    # 10) login 投影：成功白名单 + 密码泄漏回归
    lg = clean_login({'ok': True, 'logged_in': True, 'already': False,
                      'account_configured': True, 'username_masked': '138****34',
                      'password': 'p@ss', 'steps': ['x'], 'run_dir': '/tmp'})
    check(lg == {'success': True, 'logged_in': True, 'already': False,
                 'account_configured': True, 'username_masked': '138****34'},
          'login 投影不符: %r' % lg)
    lge = clean_login({'ok': False, 'error': '未配置凭证：account.ini 缺少账号密码'})
    check(lge['error'] == 'NEED_CREDENTIALS' and 'hint' in lge,
          'login 失败投影不符: %r' % lge)
    check('password' not in json.dumps(lg), 'login 投影泄漏密码')

    if failures:
        print('SELFTEST FAIL (%d)' % len(failures))
        for x in failures:
            print(' -', x)
        return 1
    print('SELFTEST OK')
    return 0


if __name__ == '__main__':
    sys.exit(_selftest())
