# -*- coding: utf-8 -*-
"""CNIPA 复审无效审查决定查询 · UIA 版 runner（免 CDP 架构，2026-09-01 定案）。

等价场景：02_search_by_application_no.json（申请号检索）与
03_search_by_case_no.json（案件编号检索）。2026-09-03 起支持技能侧契约 §1.2
全 12 项 filters（申请号/发明名称/专利类型/分类号/决定号/案件编号/专利权人/
请求人/合议组组长/主审员/参审员/代理机构），多条件=站点 AND；任一非收起态
字段自动点「展开更多搜索项」；结果支持 max_items（默认 50/上限 500）与
防御式翻页 + truncated 标记。案件编号字段只在「展开更多搜索项」
后的表单里（展开态实测：output/uia_probe/cpquery_tree_20260901_165804.txt，
367 节点）→ case_no 输入会自动点展开（幂等：已展开则跳过）。
与 PW 版的本质区别：不经 CDP/Playwright（瑞数全灭），由 OS 层 pywinauto(UIA)
定位控件 + SendInput 真实键鼠驱动用户日常 Chrome，请求由浏览器正常发出。

运行（由用户手工执行；前置：Chrome 以 --force-renderer-accessibility 启动、
cpquery 为前台标签、已登录。运行期间请勿动键鼠，脚本会自动聚焦窗口）：
    cd C:\\git\\Mcps\\cnipa-mcp
    python -X utf8 python/run_cnipa_search_decisions_uia.py python/test_inputs/02_search_by_application_no.json

产出：python/output/uia_runs/run_<ts>/  下 payload.json + result.png + tree_flat.txt
自检（离线，不碰网站、不碰窗口）：python -X utf8 python/run_cnipa_search_decisions_uia.py --selftest

结果区树结构依据（output/uia_probe/cpquery_tree_20260901_163521.txt，242 节点）：
  总数    Text '共查询到 ' + Edit(含子 Text 'N') + Text ' 条结果'；分页 Text '共N条'
  卡片头  Text '发明专利 '/'实用新型 '/'外观设计 '（含尾空格），同行右侧 Text = 发明名称
  字段    纯 Text 键值：'决定号：38430' 等；'案件编号： ' 值在下一个 Text（如 '4W107577'）
  库切换  RadioButton '复审决定库' / '无效决定库'（对应输入 library_type）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cnipa_contract
import cnipa_cache
import cnipa_uia as uia

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TAB_NAME = '复审无效审查决定查询'
EXPAND_ITEM = '展开更多搜索项'      # 收起态按钮；展开后变为 COLLAPSE_ITEM
COLLAPSE_ITEM = '收起更多搜索项'    # 出现它 = 表单已是展开态

LIB_RADIO = {
    'invalid': '无效决定库', '无效': '无效决定库', '无效决定库': '无效决定库',
    'reexam': '复审决定库', '复审': '复审决定库', '复审决定库': '复审决定库',
}

# 结果卡片字段白名单（键 → 输出字段）。表单区/页脚同名冒号文本一律不在此表 → 天然排除。
KEY_MAP = {
    '案件编号': 'case_no',
    '决定号': 'decision_no',
    '发文日': 'issue_date',
    '申请号/专利号': 'app_no',
    '专利权人': 'patentee',
    '申请日': 'app_date',
    '分类号': 'class_no',
    '审查结论': 'conclusion',
    '无效宣告请求人': 'invalid_requester',
    '复审请求人': 'reexam_requester',
}

TYPE_RE = re.compile(r'^(发明专利|实用新型|外观设计)')
PAIR_RE = re.compile(r'^([^：:]{2,12})[：:]\s*(.*)$')


def _digits(s: str) -> str:
    return re.sub(r'\D', '', str(s))


def _alnum(s: str) -> str:
    """回读比对用：去点/空格等噪声后比对（申请号 '2006…173.6' ↔ 站点 '2006…1736'；
    案件号 '4W107577' 含字母，_digits 会把字母抹掉导致比对退化）。"""
    return re.sub(r'[^0-9A-Za-z]', '', str(s)).upper()


# ---------------------------------------------------------------------------
# 结果解析（离线可测）
# ---------------------------------------------------------------------------

def ordered_texts(win) -> list:
    """按树序返回 [(text, rect)]，不去重（多卡片会出现重复键，必须保序）。"""
    out: list = []
    try:
        for el in win.descendants():
            if uia._ctype(el) in ('Text', 'Edit'):
                n, r = uia._name(el), uia._rect(el)
                if n and r:
                    out.append((n, r))
    except Exception:
        pass
    return out


def _dedup_items(items: list) -> list:
    """按 (text, rect) 去重，只留首份。

    2026-09-01 实跑实测（run_20260901_164253/tree_flat.txt）：win.descendants()
    会把整棵页面内容重复枚举两遍（两份文本+坐标完全一致）。不去重时第二份
    副本里的表单标签会灌进已开卡片、并凭空多出一张幻影卡片。
    """
    seen = set()
    out = []
    for n, r in items:
        key = (n, tuple(r))
        if key in seen:
            continue
        seen.add(key)
        out.append((n, r))
    return out


def _card_rect_real(r) -> bool:
    return r[2] > r[0] and r[3] > r[1]


def _card_type_row_indices(items: list) -> list:
    items = _dedup_items(items)
    gate_idx = 0
    for idx, (n, _r) in enumerate(items):
        if (n or '').startswith('共查询到'):
            gate_idx = idx
            break
    return [i for i in range(gate_idx + 1, len(items))
            if TYPE_RE.match(items[i][0] or '')]


def _locate_from_type_row(items: list, type_i: int):
    text, r = items[type_i]
    if type_i + 1 < len(items):
        t2, r2 = items[type_i + 1]
        same_row = abs(((r2[1] + r2[3]) // 2) - ((r[1] + r[3]) // 2)) <= 25
        if (same_row and not TYPE_RE.match(t2 or '')
                and PAIR_RE.match(t2) is None and len(t2.strip()) >= 4):
            return ('title', t2, r2, _card_rect_real(r2))
    return ('type_text', text, r, _card_rect_real(r))


def locate_card_at_index(items: list, index: int):
    """第 index 张结果卡片（0 起，与 parse_cards / worker items 同序）。"""
    items = _dedup_items(items)
    rows = _card_type_row_indices(items)
    if index < 0 or index >= len(rows):
        return None
    return _locate_from_type_row(items, rows[index])


def _card_slice_text(items: list, type_i: int, next_type_i) -> str:
    end = next_type_i if next_type_i is not None else len(items)
    return '\n'.join((items[j][0] or '') for j in range(type_i, end))


def locate_card_for_list_item(items: list, *, case_no: str = '',
                              application_no: str = '', index=None):
    """按序/案号定位卡片；批量优先 index，不做二次条件筛选。"""
    if index is not None:
        try:
            return locate_card_at_index(items, int(index))
        except (TypeError, ValueError):
            pass
    items = _dedup_items(items)
    rows = _card_type_row_indices(items)
    if not rows:
        return None
    cn_key = (case_no or '').strip().lower()
    app_digits = _digits(application_no)
    if cn_key:
        for ri, type_i in enumerate(rows):
            nxt = rows[ri + 1] if ri + 1 < len(rows) else None
            blob = _card_slice_text(items, type_i, nxt).lower()
            if cn_key not in blob and _alnum(case_no) not in _alnum(blob):
                continue
            if app_digits and app_digits not in _digits(blob):
                continue
            return _locate_from_type_row(items, type_i)
        return None
    if app_digits:
        hits = []
        for ri, type_i in enumerate(rows):
            nxt = rows[ri + 1] if ri + 1 < len(rows) else None
            if app_digits in _digits(_card_slice_text(items, type_i, nxt)):
                hits.append(type_i)
        if len(hits) == 1:
            return _locate_from_type_row(items, hits[0])
        return None
    return locate_card_at_index(items, 0)


def parse_cards(items: list) -> list:
    """把 ordered_texts 解析成卡片列表。

    双门槛规则（163521 收起态 / 164253 收起态实跑 / 165804 展开态 三棵实测树归纳）：
      - 先按 (text, rect) 去重（消除 UIA 双份枚举的幻影副本，164253 实测）；
      - 门槛一（主）：以 '共查询到' 总数条在枚举序列中的**位置**为界，只解析其后的
        文本——UIA 枚举顺序跟 DOM 一致（表单 → 总数条 → 结果区），表单标签
        （含展开态与卡片同名的 '案件编号：'）永远在界前，天然排除；
      - 门槛二（辅，仅实矩形）：rect 在视口内（宽高>0）且 top <= 总数条 top 的跳过。
        展开态表单把卡片推出视口时，Chrome a11y 对屏外文本回 0 矩形 @(0,0,0,0)
        （165804 实测），此时只用门槛一——0 矩形不参与 y 过滤；
      - 卡片起点 = TYPE_RE 命中的 Text（'发明专利 ' 等）；
      - 标题 = 起点之后同行（垂直中心差<=25px；双方都 0 矩形时中心相等，同样成立）
        的第一个非键值 Text；
      - 字段 = PAIR_RE 且键在 KEY_MAP；值为空时取下一个 Text（案件编号模式）；
      - 白名单外文本（页脚 '主办单位：' 等）忽略。
    """
    items = _dedup_items(items)
    gate_idx = None
    gate_y = None
    for idx, (n, r) in enumerate(items):
        if n.startswith('共查询到'):
            gate_idx, gate_y = idx, r[1]
            break
    if gate_idx is None:
        gate_idx, gate_y = 0, 0  # 空结果页也带总数条；万一没有则不设门（此时也解析不出卡片）

    def _real(r) -> bool:
        return r[2] > r[0] and r[3] > r[1]

    cards: list = []
    cur = None
    i = gate_idx + 1
    while i < len(items):
        text, r = items[i]
        if _real(r) and r[1] <= gate_y:
            i += 1
            continue
        m = TYPE_RE.match(text)
        if m:
            cur = {'patent_type': m.group(1), 'title': ''}
            cards.append(cur)
            i += 1
            if i < len(items):
                t2, r2 = items[i]
                same_row = abs(((r2[1] + r2[3]) // 2) - ((r[1] + r[3]) // 2)) <= 25
                if same_row and not TYPE_RE.match(t2) and PAIR_RE.match(t2) is None:
                    cur['title'] = t2
                    i += 1
            continue
        if cur is not None:
            pm = PAIR_RE.match(text)
            if pm:
                key, val = pm.group(1).strip(), pm.group(2).strip()
                if key in KEY_MAP:
                    if not val and i + 1 < len(items):
                        val = items[i + 1][0].strip()
                    cur[KEY_MAP[key]] = val
        i += 1
    return cards


def flat_tree_dump(win, path: str) -> str:
    """平铺 dump（ctype|name|rect），供离线排错/后续 04 详情页选型用。"""
    lines: list = []
    try:
        for el in win.descendants():
            ct, n, r = uia._ctype(el), uia._name(el), uia._rect(el)
            if r is None:
                continue
            if not n and ct not in ('Edit', 'DataItem', 'Custom', 'Hyperlink'):
                continue
            lines.append(f'{ct} | name={n!r} | rect={r}')
            if len(lines) >= 2000:
                lines.append('<capped 2000>')
                break
    except Exception as e:
        lines.append(f'<dump err: {e}>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _radio_selected(el):
    try:
        return bool(el.is_selected())
    except Exception:
        return None


def _norm_cpquery_appno(v: str) -> str:
    """cpquery 检索框要裸申请号（无 CN/ZL 前缀、无空格横线）——
    自然语言调用方常给 CN200610004173.6，这里归一成站点期望形态。"""
    v = re.sub(r'[\s\-]', '', str(v or ''))
    return re.sub(r'^(CN|ZL|cn|zl)', '', v)


# ---------------------------------------------------------------------------
# 12 项检索条件（技能侧契约 §1.2）：filter 键 → 站点表单标签
# ---------------------------------------------------------------------------

# 展开态表单实测（output/uia_runs/run_20260901_190040/tree_flat.txt）：
# 申请号/专利号：、发明名称：、专利类型：+Text'请选择'、分类号：、决定号：、案件编号：、
# 专利权人：、无效宣告请求人：、合议组组长：、主审员：、参审员：、代理机构名称：
# 复审库下请求人字段名为「复审请求人」（与结果卡片一致，KEY_MAP 两键都收）。
PATENT_TYPE_MAP = {
    'invention': '发明专利', 'utility_model': '实用新型', 'design': '外观设计',
    '发明': '发明专利', '发明型': '发明专利', '实用': '实用新型', '外观': '外观设计',
    '发明专利': '发明专利', '实用新型': '实用新型', '外观设计': '外观设计',
}

# 仅收起态就有的字段（其余一律自动展开表单）
COLLAPSED_OK = {'application_no', 'decision_no'}

FORM_LABELS = {
    'application_no': ('申请号/专利号',),
    'invention_title': ('发明名称',),
    'ipc_class': ('分类号',),
    'case_no': ('案件编号',),
    'patentee': ('专利权人',),
    'panel_leader': ('合议组组长',),
    'panel_examiner': ('主审员',),
    'panel_member': ('参审员',),
    'agency': ('代理机构名称',),
    # petitioner 标签随库变化：plan_fields 里按 library_type 给候选
}


def plan_fields(filters: dict, lib: str) -> list:
    """filters（12 键）→ [(filter_key, 站点标签或 None, 值)]，按固定顺序、空值剔除。

    patent_type 是下拉（标签位 None，值映射为站点中文选项），由 _select_option 专用路径处理。
    petitioner：无效库用「无效宣告请求人」，复审库优先「复审请求人」、找不到再回退。
    """
    out = []
    for key in ('application_no', 'invention_title', 'patent_type', 'ipc_class',
                'decision_no', 'case_no', 'patentee', 'petitioner',
                'panel_leader', 'panel_examiner', 'panel_member', 'agency'):
        val = str(filters.get(key) or '').strip()
        if not val:
            continue
        if key == 'application_no':
            val = _norm_cpquery_appno(val)
        if key == 'patent_type':
            val = PATENT_TYPE_MAP.get(val, val)     # 未知值原样透传（站点中文枚举直给也行）
            out.append((key, None, val))
            continue
        if key == 'petitioner':
            labels = ('复审请求人', '无效宣告请求人') if lib.startswith('reexam') \
                else ('无效宣告请求人', '复审请求人')
        else:
            labels = FORM_LABELS.get(key) or (key,)
        out.append((key, labels, val))
    return out


def need_expand_for(plan: list) -> bool:
    """任一条件不在收起态字段集 → 需要展开表单。"""
    return any(k not in COLLAPSED_OK for k, _, _ in plan)


def _match_typed(typed: str, expect: str) -> bool:
    """回读比对：数字/字母为主的字段用 _alnum（去点去空格噪声）；
    纯中文值 _alnum 会退化成空串，此时按去空白直比。"""
    a, b = _alnum(typed), _alnum(expect)
    if a and b:
        return a == b
    return str(typed or '').strip() == str(expect or '').strip()


def _select_option(win, label_text: str, value: str, step) -> bool:
    """下拉选择（专利类型）：点标签右侧「请选择」占位 → 点弹层选项 → 验证。

    树证据（190040）：'专利类型：' 同行右侧是 Text '请选择'（antd 下拉占位）；
    展开后选项是弹层 Text/ListItem，与表单静态文本同名冲突时按「点击后新出现」优先。
    验证：标签右侧同行出现 == value 的 Text（选中态会替换占位文本）。
    """
    label = None
    for t in uia.descendants(win, control_type='Text'):
        n = uia._name(t)
        if n and n.startswith(label_text):
            label = t
            break
    if label is None:
        return False
    lr = uia._rect(label)
    if lr is None:
        return False
    before = {(uia._name(e), uia._rect(e))
              for e in uia.descendants(win) if uia._name(e)}

    def _same_row(el):
        r = uia._rect(el)
        return r is not None and abs(((r[1] + r[3]) // 2) - ((lr[1] + lr[3]) // 2)) <= 25

    # ① 点占位/当前值（已选过则右侧文本就是值，同样可点）
    holder = None
    for t in uia.descendants(win, control_type='Text'):
        n = uia._name(t)
        if n and _same_row(t) and uia._rect(t)[0] > lr[2]:
            holder = t
            break
    if holder is None:
        return False
    uia.click(win, holder)
    time.sleep(0.8)
    # ② 点弹层选项：优先「新出现」的 == value 文本，其次任意 == value 可点元素
    cands = []
    for ct in ('ListItem', 'Text'):
        for el in uia.descendants(win, control_type=ct):
            n, r = uia._name(el), uia._rect(el)
            if n == value and r is not None:
                fresh = (n, r) not in before
                cands.append((0 if fresh else 1, r[1], el))
    if not cands:
        step('select_option', warn=f'下拉弹层未见选项 {value!r}')
        return False
    cands.sort(key=lambda x: (x[0], x[1]))
    cx, cy = uia._center(uia._rect(cands[0][2]))
    uia.human_click(win, cx, cy)
    time.sleep(0.6)
    # ③ 验证：标签同行出现 == value 的 Text
    for t in uia.descendants(win, control_type='Text'):
        n, r = uia._name(t), uia._rect(t)
        if n == value and r is not None and _same_row(t) and r[0] > lr[2]:
            return True
    return False


def _finish(payload: dict, out_dir: str) -> None:
    payload['finished_at'] = datetime.now().isoformat()
    p = os.path.join(out_dir, 'payload.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print('[uia] payload => ' + p)
    cnipa_contract.maybe_emit('search', payload)


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == '--selftest':
        return selftest()
    if len(sys.argv) >= 3 and sys.argv[1] == '--replay':
        return replay(sys.argv[2])

    inp = {}
    if len(sys.argv) >= 2 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], 'r', encoding='utf-8-sig') as f:
            inp = json.load(f)
    filters = dict(inp.get('filters') or {})
    # 顶层 application_no / case_no 兼容旧入参形态
    if inp.get('application_no') and not filters.get('application_no'):
        filters['application_no'] = inp['application_no']
    if inp.get('case_no') and not filters.get('case_no'):
        filters['case_no'] = inp['case_no']
    lib = str(inp.get('library_type') or 'invalid')
    radio_name = LIB_RADIO.get(lib, '无效决定库')

    # max_items / page 解析前移（入口缓存查询需要 max_items 入键）
    # 宽泛条件钳制为 10；服务层还会再硬切前 10
    max_items, broad_search = cnipa_contract.effective_search_max_items(
        filters, inp.get('max_items'))
    try:
        start_page = max(1, int(inp.get('page') or 1))
    except (TypeError, ValueError):
        start_page = 1

    # 12 项检索条件（技能侧契约 §1.2）→ 填表计划
    plan = plan_fields(filters, lib)
    if not plan:
        err = {'success': False, 'error': 'FILTER_EMPTY',
               'message': 'filters 至少需要一个非空条件（12 项任选其一，可组合）',
               'hint': cnipa_contract.HINTS['FILTER_EMPTY']}
        print(json.dumps(err, ensure_ascii=False))
        return 4

    # 本地缓存优先（docs/cnipa-mcp-本地缓存要求.md §4.1）：命中且非 force_refresh →
    # 直接返回契约 JSON（from_cache=true / cache_file），不打开任何窗口。
    # 仅 page=1 语义读写缓存：page>1 时 runner 丢弃起始页之前的卡片，语义不同。
    import cnipa_dev  # noqa: E402
    force = bool(inp.get('force_refresh'))
    if (not force or cnipa_dev.reuse_search_cache()) and start_page == 1 \
            and not cnipa_dev.should_skip('search', inp.get('dev_skip_stages')):
        cached = cnipa_cache.load_search(lib, filters, max_items)
        if cached is not None:
            out = dict(cached)
            out['from_cache'] = True
            out['cache_file'] = cnipa_cache.search_path(lib, filters, max_items)
            print('[uia] search cache hit => ' + out['cache_file'], flush=True)
            cnipa_contract.emit_dict(out)
            return 0
    need_expand = need_expand_for(plan)
    app_no = next((v for k, _, v in plan if k == 'application_no'), '')
    case_no = next((v for k, _, v in plan if k == 'case_no'), '')

    t0 = time.time()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(REPO, 'python', 'output', 'uia_runs', 'run_' + ts)
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        'mode': 'uia', 'run_dir': out_dir, 'input_file': sys.argv[1] if len(sys.argv) >= 2 else '',
        'application_no': app_no, 'case_no': case_no,
        'filters_filled': [k for k, _, _ in plan], 'library_radio': radio_name,
        'started_at': datetime.now().isoformat(), 'steps': [],
    }

    def step(name, **kv):
        payload['steps'].append(dict(step=name, **kv))
        print('[uia] ' + name + ' ' + json.dumps(kv, ensure_ascii=False)[:300], flush=True)

    def fail(code, err) -> int:
        step('fail', code=code, err=err)
        payload['error'] = str(err)  # 契约发射用（cnipa_contract 按此分类错误码）
        payload['elapsed_s'] = round(time.time() - t0, 1)
        _finish(payload, out_dir)
        return code

    # 1)+2) 窗口与表单定位——同题 Chrome 窗口可能多个，逐个试：
    #    a) set_focus 先行（18:56 实证：窗口最小化时 chrome 按钮 rect=-31992、
    #       树中 0 个 TabItem，标签条根本不枚举 → 必须先恢复）；
    #    b) 本窗口内自愈链：① 直查「决定库」表单（须见 决定号/案件编号——
    #       不能用「申请号」判定：登录后默认落在「案件查询」，也有申请号，
    #       否则会误判已在目标模块、跳过左侧「复审无效审查决定查询」）；
    #       ② 页内模块导航 TabItem '复审无效审查决定查询'
    #       （171031 树实证 @x237-478，SPA 竖排导航；首页/错模块页在场，点击即
    #       本标签内切到目标模块）；③ 浏览器标签条 TabItem（rect top<60，名含
    #       '专利审查信息查询'——模块/首页/详情页文档标题同为站名；18:48 前台=
    #       详情页时导航不在场，172905 树实证，须先切回模块标签再回 ②）；
    #    c) 全部窗口试完仍无 → 探针落盘（截图+全文本+树）后失败
    #       （18:48 教训：此路径曾零落盘）。
    def _decision_form_edit(w):
        """仅当已在「复审无效审查决定查询」模块时返回表单 Edit。"""
        if uia.label_to_edit(w, '决定号') is None and uia.label_to_edit(w, '案件编号') is None:
            return None
        return (uia.label_to_edit(w, '申请号')
                or uia.label_to_edit(w, '决定号')
                or uia.label_to_edit(w, '案件编号'))

    def _module_nav_targets(w):
        """左侧「复审无效审查决定查询」：TabItem 优先，其次 Text/ListItem/Hyperlink。"""
        navs = list(uia.descendants(w, control_type='TabItem', name=TAB_NAME) or [])
        if navs:
            return navs, 'TabItem'
        for ct in ('Text', 'ListItem', 'Hyperlink', 'Button', 'TreeItem'):
            for el in uia.descendants(w, control_type=ct) or []:
                n = (uia._name(el) or '').strip()
                if TAB_NAME in n or n in ('复审无效审查决定', '审查决定查询'):
                    r = uia._rect(el)
                    if r and r[0] < 620 and r[1] > 80:
                        return [el], ct
        return [], ''

    def _click_module_nav(w, tag=''):
        navs, kind = _module_nav_targets(w)
        if not navs:
            step('switch_module_nav', miss=True, after=tag or None)
            return False
        step('switch_module_nav', to=TAB_NAME, kind=kind, after=tag or None)
        r = uia._rect(navs[0])
        try:
            if r:
                uia.human_click(
                    w, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2, settle=0.4)
            else:
                uia.click(w, navs[0])
        except Exception:
            uia.click(w, navs[0])
        time.sleep(2.0)
        uia.ensure_page_a11y(w, retries=2)
        return True

    def _locate_form(w):
        if not uia.page_a11y_ok(uia.all_texts(w)):
            if not uia.ensure_page_a11y(w, wait_s=2.0, retries=4):
                step('form_abort', reason='a11y_not_ready',
                     n_texts=len(uia.all_texts(w)),
                     hint=uia.a11y_fail_hint(w))
                return None
        edit = _decision_form_edit(w)
        if edit is not None:
            return edit
        strip, seen = [], set()
        for t in uia.descendants(w, control_type='TabItem'):
            n, r = uia._name(t), uia._rect(t)
            if not n or not r or r[1] >= 60 or '专利审查信息查询' not in n:
                continue
            key = (n, tuple(r))
            if key in seen:
                continue
            seen.add(key)
            strip.append((n, t))
        navs, kind = _module_nav_targets(w)
        step('tabs_seen', nav=bool(navs), nav_kind=kind or None,
             strip=[n[:36] for n, _ in strip])
        if navs:
            _click_module_nav(w)
            edit = _decision_form_edit(w)
            if edit is not None:
                return edit
        for i, (n, t) in enumerate(strip[:3]):
            step('switch_strip_tab', to=n[:36], attempt=i + 1)
            uia.click(w, t)
            time.sleep(1.5)
            if not uia.ensure_page_a11y(w, retries=2):
                continue
            edit = _decision_form_edit(w)
            if edit is None and _click_module_nav(w, tag=f'strip{i+1}'):
                edit = _decision_form_edit(w)
            if edit is not None:
                return edit
        return None

    wins = uia.find_chrome_windows()
    if not wins:
        # 前台常停在 tysf 登录/其它标签 → 标题不含关键字；切回 cpquery 再找
        step('window', note='title_miss_try_switch_cpquery')
        try:
            w0 = uia.require_window()
            wins = [w0]
        except Exception as e:
            return fail(2, f'未找到标题含 {uia.WINDOW_TITLE_KEYWORD!r} 的目标窗口：{e}')
    win, edit = None, None
    for wi, w in enumerate(wins[:3]):
        try:
            w.set_focus()          # 最小化 → 恢复并前置
        except Exception as e:
            step('window', idx=wi + 1, focus_warn=str(e)[:80])
        # 前台若被点到登录/其它 tab，先切回审查信息查询
        try:
            title0 = w.window_text() or ''
        except Exception:
            title0 = ''
        ok_tab, tab_detail = uia.ensure_cpquery_front_tab(w, step=step)
        step('ensure_cpquery_tab', ok=ok_tab, detail=tab_detail, was=title0[:40])
        time.sleep(0.8)
        payload['window_title'] = w.window_text()
        payload['logged_in'] = uia.looks_logged_in(w)
        if uia.dismiss_chrome_restore_bubble(w):
            step('dismiss_restore', ok=True, idx=wi + 1)
        a11y_ok = uia.ensure_page_a11y(w, wait_s=2.0, retries=4)
        n_txt = len(uia.all_texts(w))
        step('window', idx=wi + 1, title=payload['window_title'],
             logged_in=payload['logged_in'], n_windows=len(wins),
             page_a11y=a11y_ok, n_texts=n_txt)
        if not a11y_ok:
            step('a11y_fail', hint=uia.a11y_fail_hint(w))
            continue
        # 先关「使用说明」（可能首屏就出；关完才能点左侧菜单）
        uia.dismiss_usage_dialog(w, step, timeout_s=8.0, absent_giveup_s=2.5)
        edit = _locate_form(w)
        if edit is not None:
            win = w
            break
    if win is None:
        w = wins[0]
        try:
            uia.screenshot_window(w, os.path.join(out_dir, 'fail_form.png'))
        except Exception:
            pass
        texts0 = uia.all_texts(w)
        try:
            with open(os.path.join(out_dir, 'all_texts.json'), 'w', encoding='utf-8') as f:
                json.dump(texts0, f, ensure_ascii=False, indent=1)
            flat_tree_dump(w, os.path.join(out_dir, 'tree_flat.txt'))
        except Exception:
            pass
        hint = '详情页' if any('国内案件信息详情' in t for t in texts0) else (
            '结果页' if any('共查询到' in t for t in texts0) else '未知页态')
        a11y = ''
        if not texts0 or not uia.page_a11y_ok(texts0):
            a11y = '（%s）' % uia.a11y_fail_hint(w)
        return fail(2, f'已试 {len(wins[:3])} 个窗口均未定位到检索表单，'
                       f'最后窗口疑似[{hint}]{a11y}')

    def _still_on_decision_form() -> bool:
        return _decision_form_edit(win) is not None

    def _prepare_decision_form() -> bool:
        """关使用说明 → 确认在复审无效模块（否则点左侧菜单）→ 再关一次弹窗。"""
        uia.dismiss_usage_dialog(win, step, timeout_s=6.0, absent_giveup_s=1.5)
        if _still_on_decision_form():
            return True
        if not _click_module_nav(win, tag='prepare'):
            # 再等弹窗：挡住时菜单点不到
            uia.dismiss_usage_dialog(win, step, timeout_s=8.0, absent_giveup_s=1.0)
            if not _click_module_nav(win, tag='prepare2'):
                return _still_on_decision_form()
        time.sleep(1.0)
        uia.dismiss_usage_dialog(win, step, timeout_s=6.0, absent_giveup_s=1.5)
        return _still_on_decision_form()

    def _ensure_expanded_form() -> tuple:
        if not need_expand:
            return True, None
        try:
            if uia.find_button(win, COLLAPSE_ITEM) is not None:
                step('ensure_expanded', already=True)
                return True, None
            exp = uia.find_button(win, EXPAND_ITEM)
            if exp is None:
                return False, f'未找到「{EXPAND_ITEM}」/「{COLLAPSE_ITEM}」'
            uia.click(win, exp)
            time.sleep(1.2)
            step('ensure_expanded', clicked=True)
            return True, None
        except Exception as e:
            step('ensure_expanded', warn=str(e))
            return True, None

    def _select_library_radio() -> tuple:
        """必须点到 library_type 对应决定库（重查路径此前漏选 → 默认复审库 0 条）。"""
        try:
            radios = uia.descendants(win, control_type='RadioButton', name=radio_name)
            if not radios:
                step('radio', miss=True, want=radio_name)
                return False, f'未找到 RadioButton {radio_name!r}（当前可能仍在案件查询页）'
            sel = _radio_selected(radios[0])
            if sel is True:
                step('radio', to=radio_name, already_selected=True)
            else:
                uia.click(win, radios[0])
                time.sleep(1.0)
                step('radio', to=radio_name, was_selected=sel)
            return True, None
        except Exception as e:
            step('radio', warn=str(e))
            return False, str(e)

    def _fill_plan_fields() -> tuple:
        for fkey, labels, fval in plan:
            if fkey == 'patent_type':
                ok_sel = _select_option(win, '专利类型', fval, step)
                step('select_patent_type', value=fval, match=ok_sel)
                if not ok_sel:
                    return False, f'专利类型下拉选择未生效 value={fval!r}'
                continue
            edit, used = None, None
            for lab in labels:
                edit = uia.label_to_edit(win, lab)
                if edit is not None:
                    used = lab
                    break
            if edit is None:
                return False, f'未定位到「{labels[0]}」输入框'
            typed = uia.clear_and_type(win, edit, fval)
            okv = _match_typed(typed, fval)
            if not okv and typed == '':
                time.sleep(0.5)
                typed = uia.edit_value(edit)
                okv = _match_typed(typed, fval)
            step('type_field', field=used, value=fval, match=okv)
            if not okv and typed:
                return False, (
                    f'{used} 输入框回读不一致 typed={typed!r} expect={fval!r}')
        return True, None

    def _submit_query(total_before, retry: bool = False) -> tuple:
        qbtn = uia.find_button(win, '查询')
        if qbtn is None:
            return False, None, '未找到「查询」按钮'
        payload['total_before'] = total_before
        uia.click(win, qbtn)
        if retry:
            step('click_query', ok=True, retry=True)
        else:
            step('click_query', ok=True)
        return _wait_query_result(total_before, allow_retry=not retry)

    def _fill_and_query() -> tuple:
        """填表+点查询。禁止先点「重置」——易掉回案件查询；字段用 clear_and_type 即可。"""
        if not _still_on_decision_form():
            if not _prepare_decision_form():
                return False, None, '不在复审无效检索模块'
        ok_e, err_e = _ensure_expanded_form()
        if not ok_e:
            return False, None, err_e
        ok_r, err_r = _select_library_radio()
        if not ok_r:
            return False, None, err_r
        if not _still_on_decision_form():
            if not _prepare_decision_form():
                return False, None, '选库后页面离开检索模块'
            ok_r, err_r = _select_library_radio()
            if not ok_r:
                return False, None, err_r
        total_before = uia.read_total(win)
        ok_f, err_f = _fill_plan_fields()
        if not ok_f:
            return False, None, err_f
        return _submit_query(total_before)

    def _wait_query_result(total_before, allow_retry=True,
                           allow_empty_requery=True) -> tuple:
        """等总数变化；查询后「使用说明」常晚出，轮询中随时关。关后若掉回首页则重进重查。

        站点偶发：转圈后假 0 条，条件不变再点一次「查询」即有结果。
        """
        reset = {'hit': False}

        def _poll_dismiss():
            if not uia.usage_dialog_present(win):
                return
            if not uia.dismiss_usage_dialog(
                    win, step, timeout_s=12.0, absent_giveup_s=0.5):
                return
            if not _still_on_decision_form():
                reset['hit'] = True

        total_after = uia.wait_total_change(
            win, total_before, timeout_s=28.0, poll_s=0.7, on_poll=_poll_dismiss)
        if reset['hit']:
            step('usage_reset_page', note='关使用说明后掉回案件查询，重进模块重查')
            if not allow_retry:
                return False, None, '关使用说明后页面被重置且不可再重试'
            if not _prepare_decision_form():
                return False, None, '关使用说明后无法回到复审无效检索页'
            return _fill_and_query_once()
        if uia.usage_dialog_present(win):
            uia.dismiss_usage_dialog(win, step, timeout_s=10.0, absent_giveup_s=0.5)
            if not _still_on_decision_form():
                step('usage_reset_page', note='结果后关弹窗掉回首页')
                if allow_retry and _prepare_decision_form():
                    return _fill_and_query_once()
                return False, None, '关使用说明后无法回到复审无效检索页'

        if allow_empty_requery and (total_after is None or total_after == 0):
            time.sleep(1.5)
            again = uia.read_total(win)
            if again is not None and again > 0:
                step('query_empty_skip', note='过渡假0后已有结果，不重点查询',
                     total=again)
                return True, again, None
            step('query_empty_requery',
                 note='首次为 0，条件不变再点查询一次')
            time.sleep(1.2)
            qbtn = uia.find_button(win, '查询')
            if qbtn is None:
                return True, total_after, None
            uia.click(win, qbtn)
            step('click_query', ok=True, empty_requery=True)
            total_after = uia.wait_total_change(
                win, 0, timeout_s=22.0, poll_s=0.7, on_poll=_poll_dismiss)
            if reset['hit']:
                step('usage_reset_page', note='空结果重查时掉回首页')
                if allow_retry and _prepare_decision_form():
                    return _fill_and_query_once()
                return False, None, '空结果重查时页面被重置'
        time.sleep(0.8)
        return True, total_after, None

    def _fill_and_query_once() -> tuple:
        """关使用说明掉回首页后的重查：须重新进模块 + 选库 + 填表。"""
        if not _prepare_decision_form():
            return False, None, '重查时无法回到复审无效检索页'
        ok_e, err_e = _ensure_expanded_form()
        if not ok_e:
            return False, None, err_e
        ok_r, err_r = _select_library_radio()
        if not ok_r:
            return False, None, err_r
        total_before = uia.read_total(win)
        ok_f, err_f = _fill_plan_fields()
        if not ok_f:
            return False, None, err_f
        return _submit_query(total_before, retry=True)

    if not _prepare_decision_form():
        uia.screenshot_window(win, os.path.join(out_dir, 'fail_module.png'))
        return fail(2, '未能进入「复审无效审查决定查询」模块（左侧菜单未点到或被使用说明挡住）')

    ok_q, total_after, err_q = _fill_and_query()
    if not ok_q:
        uia.screenshot_window(win, os.path.join(out_dir, 'fail_fill.png'))
        return fail(3 if err_q and '回读' in err_q else 2, err_q or '填表查询失败')
    payload['total_after'] = total_after
    step('total', before=payload.get('total_before'), after=total_after)

    # 7) 解析结果 + 采证落盘
    #    展开态卡片可能在视口外（0 矩形），顺序门槛可直接解析；万一解析不出
    #    （总数>0 而卡片 0）→ 真实滚轮下滚带回视口重采一次
    items = ordered_texts(win)
    cards = parse_cards(items)
    if not cards and (total_after or 0) > 0:
        try:
            uia.scroll_down(win, 960, 950, notches=5)
            items = ordered_texts(win)
            cards = parse_cards(items)
            step('scroll_retry', n=len(cards))
        except Exception as e:
            step('scroll_retry', warn=str(e))
    payload['result_count'] = len(cards)
    payload['results'] = cards
    if cards and app_no:
        payload['app_no_match'] = _alnum(cards[0].get('app_no', '')) == _alnum(app_no)
    if cards and case_no:
        payload['case_no_match'] = _alnum(cards[0].get('case_no', '')) == _alnum(case_no)
    step('parse_cards', n=len(cards), first=cards[0] if cards else None)

    # 7.5) 翻页采集（防御式，技能侧契约 §1.1 max_items 默认 50 / 上限 500）
    #      实测树（190040）分页区只见 Button '1'，未见「下一页」命名按钮——
    #      能找到就翻、找不到就停在当前页并把 truncated 标真，绝不死循环。
    #      （max_items / start_page 已在 main() 入口解析，缓存键在那里就要用）
    seen_keys = {(c.get('case_no'), c.get('app_no')) for c in cards}

    def _collect_more() -> int:
        grew = 0
        for c in parse_cards(ordered_texts(win)):
            k = (c.get('case_no'), c.get('app_no'))
            if k not in seen_keys:
                seen_keys.add(k)
                cards.append(c)
                grew += 1
        return grew

    def _next_page_btn():
        for ct in ('Button', 'MenuItem'):
            for el in uia.descendants(win, control_type=ct):
                if '下一页' in uia._name(el) and uia._rect(el) is not None:
                    return el
        return None

    cur_page, guard = 1, 0
    while cur_page < start_page and guard < 50:        # page>1：先翻到起始页
        nxt = _next_page_btn()
        if nxt is None:
            step('paginate', warn=f'未找到「下一页」，停在当前页 {cur_page}（请求 page={start_page}）')
            break
        uia.click(win, nxt)
        time.sleep(2.0)
        cur_page += 1
        guard += 1
    if start_page > 1 and cur_page == start_page:
        cards.clear()                                  # 丢弃起始页之前页面的卡片
        seen_keys.clear()
        _collect_more()
        payload['results'] = cards
        payload['result_count'] = len(cards)
        step('paginate', page=cur_page, restarted=True, collected=len(cards))
    while len(cards) < max_items and (total_after or 0) > len(seen_keys) and guard < 50:
        nxt = _next_page_btn()
        if nxt is None:
            break
        uia.click(win, nxt)
        time.sleep(2.0)
        cur_page += 1
        guard += 1
        grew = _collect_more()
        step('paginate', page=cur_page, new=grew, collected=len(cards))
        if grew == 0:
            break                                       # 无新增 → 防御性止损
    payload['page'] = cur_page
    payload['max_items'] = max_items
    if len(cards) > max_items:
        cards = cards[:max_items]
        payload['results'] = cards
        payload['result_count'] = len(cards)
    payload['truncated'] = bool((total_after or 0) > len(cards))
    if broad_search and (total_after or 0) > len(cards):
        step('broad_cap', total=total_after, kept=len(cards),
             max_items=max_items, note='宽泛检索只取前 N 条')
    step('collect_done', pages=cur_page, collected=len(cards),
         total=total_after, truncated=payload['truncated'], broad=broad_search)

    try:
        shot = uia.screenshot_window(win, os.path.join(out_dir, 'result.png'))
        payload['screenshot'] = shot
    except Exception as e:
        step('screenshot', warn=str(e))
    try:
        payload['tree_flat'] = flat_tree_dump(win, os.path.join(out_dir, 'tree_flat.txt'))
    except Exception as e:
        step('tree_dump', warn=str(e))

    payload['elapsed_s'] = round(time.time() - t0, 1)
    payload['ok'] = total_after is not None and not (total_after or 0) > 0 or len(cards) > 0 or total_after == 0
    # 成功 → 写持久缓存（契约投影；缓存文件本身不含 from_cache/cache_file），
    # 并给本次响应回填 from_cache=false + cache_file（§4.1）。page=1 语义才写。
    if payload['ok'] and start_page == 1:
        cf = cnipa_contract.write_search_cache(lib, filters, max_items, payload)
        if cf:
            payload['cache_file'] = cf
            payload['from_cache'] = False
    _finish(payload, out_dir)
    print(f'[uia] 完成：total={total_after}，解析 {len(cards)} 条，耗时 {payload["elapsed_s"]}s，产物目录：{out_dir}')
    return 0


# ---------------------------------------------------------------------------
# 离线自检：用 163521 实测树的文本序列喂解析器，不碰网站/窗口
# ---------------------------------------------------------------------------

def _assert_one_card(cards: list, ctx: str) -> None:
    assert len(cards) == 1, f'{ctx}: 应解析出 1 张卡片（去重失败会得 2），实际 {len(cards)}'
    c = cards[0]
    assert c['patent_type'] == '发明专利', (ctx, c)
    assert c['case_no'] == '4W107577', (ctx, c)
    assert c['decision_no'] == '38430', (ctx, c)
    assert c['issue_date'] == '20181228', (ctx, c)
    assert c['app_no'] == '2006100041736', (ctx, c)
    assert c['patentee'] == '远大医药（天津）有限公司', (ctx, c)
    assert c['app_date'] == '2006-02-22', (ctx, c)
    assert c['class_no'] == 'C07C229/24', (ctx, c)
    assert c['conclusion'] == '维持有效', (ctx, c)
    assert c['invalid_requester'] == '李瑶', (ctx, c)
    assert c['title'].startswith('(±)2-(二甲基氨基)'), (ctx, c)
    blob = json.dumps(cards, ensure_ascii=False)
    assert '主办单位' not in blob and '网站标识码' not in blob, f'{ctx}: 页脚文本不得混入'
    assert '关键词' not in blob and 'cancel' not in blob, f'{ctx}: 表单标签/图标名不得混入'


def selftest() -> int:
    TITLE = '(±)2-(二甲基氨基)-1-{[O-(m-甲氧基苯乙基)苯氧基]甲基}乙基氢琥珀酸酯盐酸盐的晶体'

    # 夹具一：run_20260901_164253 实跑形态（收起态）——表单标签 + Edit 'cancel'
    # 在总数条上方（真实矩形）；卡片在下方；页脚在最后；结尾拼完整副本（UIA 双份枚举）。
    base = [
        ('申请号/专利号：', (528, 284, 632, 302)),          # 表单标签（门槛上方）
        ('cancel', (647, 280, 992, 308)),                    # 内层 input 的 name，非值
        ('决定号：', (575, 328, 632, 346)),
        ('关键词：', (1171, 328, 1228, 346)),
        ('共查询到 ', (236, 581, 296, 598)),                 # 总数条 = 门槛
        ('1', (295, 581, 305, 598)),
        (' 条结果', (304, 581, 350, 598)),
        ('申请日', (256, 649, 299, 666)),                    # 排序表头：无冒号 → 忽略
        ('发文日', (329, 649, 372, 666)),
        ('发明专利 ', (265, 711, 330, 729)),
        (TITLE, (344, 711, 912, 729)),
        ('案件编号： ', (260, 744, 334, 761)),
        ('4W107577', (333, 744, 402, 761)),
        ('决定号：38430', (442, 744, 538, 761)),
        ('发文日：20181228', (578, 744, 698, 761)),
        ('申请号/专利号：2006100041736', (738, 744, 945, 761)),
        ('专利权人：远大医药（天津）有限公司', (985, 744, 1224, 761)),
        ('申请日：2006-02-22', (1264, 744, 1392, 761)),
        ('分类号：C07C229/24', (1432, 744, 1568, 761)),
        ('审查结论：维持有效', (260, 744, 1651, 791)),
        ('无效宣告请求人：李瑶', (385, 774, 526, 791)),
        ('共1条', (788, 826, 825, 844)),
        ('主办单位：国家知识产权局', (884, 990, 1052, 1007)),   # 页脚：白名单外 → 忽略
        ('版权所有：国家知识产权局', (1072, 990, 1240, 1007)),
        ('网站标识码：bm38000007', (1180, 1021, 1348, 1038)),
    ]
    cards = parse_cards(base + base)   # 拼第二份完整副本 = UIA 幻影副本（164253 实测形态）
    print(json.dumps(cards, ensure_ascii=False, indent=2))
    _assert_one_card(cards, '收起态(164253形态)')

    # 夹具二：cpquery_tree_20260901_165804 展开态形态——表单 22 字段全在门槛上方
    # （含与卡片同名的表单版 '案件编号：'，空值+真实矩形）；卡片被推出视口 →
    # 全部 0 矩形 @(0,0,0,0)；同样拼一份幻影副本。
    zero = (0, 0, 0, 0)
    base2 = [
        ('申请号/专利号：', (528, 284, 632, 302)),
        ('决定号：', (575, 516, 632, 534)),
        ('案件编号：', (1157, 516, 1228, 534)),      # 展开表单里的同名字段：顺序门槛必须挡住
        ('关键词：', (1171, 796, 1228, 814)),
        ('查询', (955, 845, 984, 862)),
        ('收起更多搜索项', (1111, 845, 1210, 862)),
        ('共查询到 ', (236, 911, 296, 928)),
        ('1', (295, 911, 305, 928)),
        (' 条结果', (304, 911, 350, 928)),
        ('申请日', (256, 979, 299, 996)),            # 排序表头
        ('发文日', (329, 979, 372, 996)),
        ('发明专利 ', zero),                          # ↓ 卡片全部屏外（0 矩形）
        (TITLE, zero),
        ('案件编号： ', zero),
        ('4W107577', zero),
        ('决定号：38430', zero),
        ('发文日：20181228', zero),
        ('申请号/专利号：2006100041736', zero),
        ('专利权人：远大医药（天津）有限公司', zero),
        ('申请日：2006-02-22', zero),
        ('分类号：C07C229/24', zero),
        ('审查结论：维持有效', zero),
        ('无效宣告请求人：李瑶', zero),
        ('共1条', zero),
        ('主办单位：国家知识产权局', (884, 990, 1052, 1007)),  # 实矩形但在门槛下方 → 只能靠白名单挡
    ]
    cards2 = parse_cards(base2 + base2)
    print(json.dumps(cards2, ensure_ascii=False, indent=2))
    _assert_one_card(cards2, '展开态0矩形(165804形态)')

    # —— 12 项 filters（技能侧契约 §1.2）——
    # 全量计划：键序固定、申请号规范化、专利类型枚举映射、petitioner 按库取标签
    plan = plan_fields({
        'application_no': 'CN200610004173.6', 'invention_title': '晶体',
        'patent_type': 'invention', 'ipc_class': 'A61K31/137',
        'decision_no': '38430', 'case_no': '4W107577',
        'patentee': '远大医药', 'petitioner': '李瑶',
        'panel_leader': '任晓兰', 'panel_examiner': '刘婷婷',
        'panel_member': '刘静', 'agency': '某代理所',
    }, 'invalid')
    assert [k for k, _, _ in plan] == ['application_no', 'invention_title',
                                       'patent_type', 'ipc_class', 'decision_no',
                                       'case_no', 'patentee', 'petitioner',
                                       'panel_leader', 'panel_examiner',
                                       'panel_member', 'agency'], plan
    fd = dict((k, (lb, v)) for k, lb, v in plan)
    assert fd['application_no'][1] == '200610004173.6', fd['application_no']
    assert fd['patent_type'] == (None, '发明专利'), fd['patent_type']
    assert fd['petitioner'][0][0] == '无效宣告请求人', fd['petitioner']
    assert need_expand_for(plan) is True

    # 复审库：petitioner 优先「复审请求人」
    plan_r = plan_fields({'petitioner': '王某'}, 'reexam')
    assert plan_r == [('petitioner', ('复审请求人', '无效宣告请求人'), '王某')], plan_r
    # 收起态两字段不触发展开
    assert need_expand_for(plan_fields({'application_no': '200610004173.6'}, 'invalid')) is False
    assert need_expand_for(plan_fields({'decision_no': '38430'}, 'invalid')) is False
    # 空值剔除 / 空 filters → 空计划（→ FILTER_EMPTY）
    assert plan_fields({'case_no': '  ', 'patentee': None}, 'invalid') == []
    # patent_type 中文值直通
    assert plan_fields({'patent_type': '实用新型'}, 'invalid') == [
        ('patent_type', None, '实用新型')]

    # 回读比对：字母数字走 _alnum 去噪；纯中文去空白直比
    assert _match_typed('200610004173.6', '2006100041736') is True
    assert _match_typed('4W107577', '4W107577') is True
    assert _match_typed('李瑶 ', '李瑶') is True
    assert _match_typed('王五', '李四') is False

    print('[selftest] OK — 卡片三形态 + 12 项 filters 计划 + 回读比对 全部通过')
    return 0


def replay(path: str) -> int:
    """把 flat_tree_dump 的落盘文件回放进解析器（离线验证，不碰网站/窗口）。"""
    import ast
    items = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^(Text|Edit) \| name=(.*?) \| rect=\(([^)]*)\)', line)
            if not m:
                continue
            name = ast.literal_eval(m.group(2))
            if not name:
                continue
            rect = tuple(int(x.strip()) for x in m.group(3).split(','))
            items.append((name, rect))
    cards = parse_cards(items)
    print(json.dumps(cards, ensure_ascii=False, indent=2))
    print(f'[replay] {path}: {len(items)} 条文本 → {len(cards)} 张卡片')
    return 0 if cards else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # 未捕获异常也要留现场（run_20260901_164022 教训：只留下空目录无任何证据）
        import traceback
        tb = traceback.format_exc()
        try:
            crash_dir = os.path.join(REPO, 'python', 'output', 'uia_runs')
            os.makedirs(crash_dir, exist_ok=True)
            with open(os.path.join(crash_dir, 'last_crash.txt'), 'w', encoding='utf-8') as f:
                f.write(datetime.now().isoformat() + '\n' + tb)
        except Exception:
            pass
        print('[uia] 未捕获异常（已写 python/output/uia_runs/last_crash.txt）:\n' + tb)
        sys.exit(9)
