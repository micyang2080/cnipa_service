# -*- coding: utf-8 -*-
"""决定书 OCR：WinRT zh-CN + MinerU HTTP 回退 + 文本容错解析。

为什么 OCR：cpquery 详情页右栏是防爬的**整页图片**渲染——2026-09-01 树 dump
（output/uia_probe/cpquery_tree_20260901_174632.txt）实证内容区
Group@(634,-74,1656,11523) 下约 9 张 Image、零 Text，UIA 文本通道永远拿不到
决定书正文。滚动截图 + OCR 是免 CDP 的文本化通道；无 WinRT 时走 MinerU
`/file_parse`（见 cnipa_mineru.py / config.ini [ocr]）。

OCR 实测三类噪声（ocr_synth_test.png 实测）：
  ① 中文字符间全插半角空格（'专 利 权 人'）；
  ② 冒号常识别成 '．'/'．．' 或丢失；
  ③ 个别字误识（决→'冫央'、件→'亻牛'）。
对策：不做通用模糊配对，改**锚词命中 + 正则取值**——锚词取识别稳定的
尾段（'定号'/'编号'/'请求人'），误识字出现在锚词前后不影响命中。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)   # 自测直接跑本文件时兜底（捆绑运行时安全路径）
PS1 = os.path.join(ROOT, 'ocr_winrt.ps1')

# 锚词表（field → 锚词，长的优先在匹配时全局排序）。
# 锚词刻意取「尾段」：'决定号'→'定号'、'案件编号'→'编号' 规避首字误识。
FIELD_ANCHORS: dict = {
    'petitioner': ('无效宣告请求人', '复审请求人', '请求人'),
    'patentee': ('专利权人',),
    'invention_title': ('发明创造名称', '发明名称'),
    'request_date': ('无效宣告请求日', '复审请求日', '请求日'),
    'legal_basis': ('法律依据',),
    'decision_summary': ('决定要点',),
    'panel_members': ('合议组',),
    'decision_result': ('审查结论', '决定结果'),
    # case_no 双锚：'编号' 兜底吸收 件→亻牛 等中段误识（'案亻牛编号4W…'）
    'case_no': ('案件编号', '编号'),
    'decision_no': ('定号',),
    'decision_date': ('定日',),
    'application_no': ('申请号',),
}
# 一个锚词可能被更长锚词包含（'请求人'⊂'无效宣告请求人'）→ 全局按长度降序
_ANCHOR_SORTED = sorted(((a, f) for f, as_ in FIELD_ANCHORS.items() for a in as_),
                        key=lambda x: -len(x[0]))
# 后续行续读（长文本字段：决定要点/法律依据/合议组名单跨行）
_CONT_FIELDS = {'decision_summary', 'legal_basis', 'panel_members'}
_DATE_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日凵口]?')
_RESULT_WORDS = ('维持', '无效', '撤销', '驳回', '部分')


def norm(s: str) -> str:
    """OCR 行归一：去空格与常见误识分隔符（：:．.、，,；;）。"""
    return re.sub(r'[\s：:．.、，,；;]', '', s or '')


_BOX_RE = re.compile(r'^L\|(\d+),(\d+),(\d+),(\d+)\|(.*)$')


def _decode(b: bytes) -> str:
    """PowerShell stdout 编码自适配（2026-09-01 19:02 实跑教训）。

    ps1 已强制 [Console]::OutputEncoding=UTF8，但 GUI 宿主无控制台句柄时
    setter 失败 → stdout 仍是系统 ANSI（中文系统 GBK）。策略：utf-8 / gbk
    依次试严格解码，取首个无 U+FFFD 的；全失败按 utf-8 replace（原行为）。
    旧版按 utf-8 errors='replace' 硬解 GBK → 中文全损（shot1_ocr.txt 实证）。
    """
    if not b:
        return ''
    for enc in ('utf-8', 'gbk'):
        try:
            s = b.decode(enc)
        except UnicodeDecodeError:
            continue
        if '\ufffd' not in s:
            return s.replace('\ufeff', '')
    return b.decode('utf-8', 'replace').replace('\ufeff', '')


def _ocr_backend() -> str:
    try:
        import cnipa_mineru as mu
        return str(mu.load_ocr_settings().get('backend') or 'mineru').lower()
    except Exception:
        return 'mineru'


def _ocr_image_winrt(img_path: str, lang: str = 'zh-CN', timeout: int = 90) -> list:
    """Windows OCR，返回 [(text, (l,t,r,b)), ...]。"""
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
           '-File', PS1, os.path.abspath(img_path), '-Lang', lang]
    cp = subprocess.run(cmd, capture_output=True, timeout=timeout)   # bytes：编码自适配
    stdout = _decode(cp.stdout or b'')
    out = stdout + '\n' + _decode(cp.stderr or b'')
    if 'OCR_ENGINE_NULL' in out:
        raise RuntimeError(f'Windows OCR 引擎缺失({lang})：需在系统设置安装中文OCR语言包')
    if 'OCR_ERR' in out:
        raise RuntimeError(f'OCR 失败: {out[:300]}')
    items = []
    for ln in stdout.splitlines():
        if not ln or ln.startswith(('LANG=', 'LINES=')):
            continue
        m = _BOX_RE.match(ln)
        if m:
            items.append((m.group(5).rstrip(),
                          (int(m.group(1)), int(m.group(2)),
                           int(m.group(3)), int(m.group(4)))))
        else:
            items.append((ln.rstrip(), (0, 0, 0, 0)))
    return items


def ocr_image(img_path: str, lang: str = 'zh-CN', timeout: int = 90) -> list:
    """OCR 图片 → [(text, (l,t,r,b)), ...]（行级词框并集，物理像素）。

    backend（service/config.ini [ocr] / CNIPA_OCR_BACKEND，默认 mineru）：
      mineru — HTTP file_parse（默认，全环境一致）
      winrt  — 仅本机 WinRT
      auto   — 先 WinRT，失败或空再 MinerU
    box 只用于同一张图内行对齐；无框时 (0,0,0,0)。
    """
    backend = _ocr_backend()
    if backend == 'mineru':
        import cnipa_mineru as mu
        return mu.ocr_image_mineru(img_path, timeout=max(timeout, 180))

    winrt_err = None
    items = []
    try:
        items = _ocr_image_winrt(img_path, lang=lang, timeout=timeout)
    except Exception as e:
        winrt_err = e
        items = []

    if items:
        return items

    if backend == 'winrt':
        if winrt_err:
            raise winrt_err
        return items

    # auto：WinRT 失败/空 → MinerU
    try:
        import cnipa_mineru as mu
        return mu.ocr_image_mineru(img_path, timeout=max(timeout, 180))
    except Exception as mu_err:
        if winrt_err:
            raise RuntimeError(f'WinRT失败且MinerU失败: {winrt_err}; {mu_err}') from mu_err
        raise


def ocr_image_lines(img_path: str, lang: str = 'zh-CN', timeout: int = 90) -> list:
    """纯文本行视图（供 parse_ocr_lines 这类无坐标解析）。"""
    return [t for t, _ in ocr_image(img_path, lang, timeout)]


def _extract(field: str, val: str) -> str:
    """锚词命中后的取值清洗（按字段类型收紧）。"""
    if not val:
        return ''
    if field == 'case_no':
        m = re.search(r'\d{1,3}W\d{5,7}', val)
        return m.group(0) if m else val[:20]
    if field == 'decision_no':
        m = re.search(r'\d{3,7}', val)
        return m.group(0) if m else val[:12]
    if field == 'application_no':
        m = re.search(r'\d{11,14}', val)
        return m.group(0) if m else val[:16]
    if field in ('decision_date', 'request_date'):
        m = _DATE_RE.search(val)
        if m:
            return f'{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}'
        m = re.search(r'\d{8}', val)
        return m.group(0) if m else val[:12]
    if field == 'panel_members':
        return val[:120]
    if field == 'decision_summary':
        return val[:600]
    return val[:80]


def parse_ocr_lines(lines: list) -> dict:
    """OCR 行序列 → 12 列字典（锚词命中 + 跨行续读 + 首个非空优先）。

    decision_result 额外通道：行首为「决定」且含结论词（维持/无效/撤销…）
    ——决定书结论行的标准排布（用户 17:44 实见「决定：维持专利权有效」）。
    """
    out: dict = {}
    cur_field = None

    def _hit(field: str, val: str) -> None:
        v = _extract(field, val)
        if v and field not in out:
            out[field] = v

    for raw in lines:
        n = norm(raw)
        if len(n) < 3:
            cur_field = None
            continue
        matched = None
        for anchor, field in _ANCHOR_SORTED:
            if anchor in n:
                matched = (anchor, field)
                break
        if matched is None:
            # 结论行特判：决定：维持专利权有效（决定要点/决定号 都已是更长锚词）
            if n.startswith('决定') and any(w in n for w in _RESULT_WORDS) \
                    and '决定要点' not in n:
                _hit('decision_result', n[2:])
                cur_field = None   # 结论行不得回灌决定要点续读
                continue
            if cur_field:
                out[cur_field] = (out.get(cur_field, '') + n)[:600 if cur_field
                                                              in ('decision_summary',) else 120]
            continue
        anchor, field = matched
        if field in out and field not in _CONT_FIELDS:
            cur_field = None
            continue
        val = n[n.index(anchor) + len(anchor):]
        _hit(field, val)
        cur_field = field if (field in _CONT_FIELDS and len(val) < 40) else None
    return out


# ---------------------------------------------------------------------------
# 详情页带框 OCR 解析（2026-09-01 page1/page2 实测版式）
#
# 版式实证（ocr_page2_boxed.txt）：
#   · 信息表 = 标签列(x≈708-834) × 值列(x≈951+)，**逐行同 y 对齐**（如
#     无效宣告请求日(y664-681) ↔ 2018年07月24日(y664-681)）→ 同排右值连接；
#   · '决定要点' 是表末行，正文在标签下方**全宽**多行，可溢出到下一屏；
#   · 标签误识 '决定日'→'决定凵'、'申请日'→'申请凵' → 归一时 凵→日 兜底；
#   · 结论 = 勾选框三选一行（page1 y864-966）：未选 '囗'、勾选 '冈'。
# ---------------------------------------------------------------------------

CANON_ROWS: dict = {
    '案件编号': 'case_no',
    '决定号': 'decision_no',
    '决定日': 'decision_date', '发文日': 'decision_date',
    '发明创造名称': 'invention_title', '发明名称': 'invention_title',
    '国际分类号': 'class_no',
    '无效宣告请求人': 'petitioner', '复审请求人': 'petitioner',
    '专利权人': 'patentee',
    '专利号': 'application_no', '申请号': 'application_no',
    '申请日': 'app_date', '优先权日': 'priority_date', '授权公告日': 'grant_date',
    '无效宣告请求日': 'request_date', '复审请求日': 'request_date',
    '法律依据': 'legal_basis',
    '决定要点': 'decision_summary',
}

_OUTCOMES = ('维持专利权有效', '宣告专利权全部无效', '宣告专利权部分无效',
             '维持有效', '驳回再审请求')
# 签名栏实测（page1-info.png 18:31）：'厶议组组长'（合→厶）、'主审'（无员字）
# → 尾段锚 '议组组长' + 短形 '主审'/'参审' 兜底。
_PANEL_HINTS = ('合议组', '议组组长', '主审员', '主审', '参审员', '参审')
_JUNK_HINTS = (
    'cpquery', 'chrome', '浏览器', '中国及多国', 'searchtype', 'icp备',
    'savetmp', 'figure_saveas', '.png', '全部显示', '多国发明',
    '专利审查信息查询', '国内案件信息详', '中国专利审查',
    '地址和搜索栏',
)
_BROWSER_UI_TAIL = ('地址和搜索栏',)
_TYPE_FIELDS = {'case_no', 'decision_no', 'application_no', 'decision_date',
                'request_date', 'app_date', 'priority_date', 'grant_date'}


def despace(s: str) -> str:
    return re.sub(r'\s+', '', s or '')


def _cjk_len(s: str) -> int:
    return sum(1 for ch in (s or '') if '\u4e00' <= ch <= '\u9fff')


def _is_prose(n: str) -> bool:
    """要点正文行：够长中文且非浏览器/地址栏噪声。"""
    return _cjk_len(n) >= 8 and not any(h in n.lower() for h in _JUNK_HINTS)


def _is_sec_head(n: str) -> bool:
    """段题行（2026-09-01 20:57 用户质疑：不能写死"案由"一个词）。泛化为
    复审/无效决定书模板段题词表 + 可选序号；norm 会吃掉 、：等分隔符，OCR
    碎片 '、 案 由'→'案由'、'一、案由'→'一案由' 均可命中。本规则只是
    **文字兜底**，主规则是表格底边框横线（__HL__ 伪项，见 find_hlines）。"""
    n2 = re.sub(r'[【\[\]（）()]', '', n or '')
    if re.fullmatch(r'[一二三四五六七八九十1-9]?'
                    r'(案由|决定的理由|决定理由|处理决定|结论|决定)', n2):
        return True
    if re.match(r'^[、.]?案由$', n2):
        return True
    if re.match(r'^一[、.]?案由', n2):
        return True
    if re.match(r'^三[、.]?决定', n2):
        return True
    return False


def _trim_summary_at_section(s: str) -> str:
    """要点正文误入「一、案由」/末页理由正文后截断。"""
    if not s:
        return s
    for pat in (r'一[、.]?案由', r'二[、.]?决定的理由', r'二[、.]?决定理由',
                r'三[、.]?处理决定', r'三[、.]?决定',
                r'证据\s*[0-9０-９]', r'综上[，,]请求人',
                r'根据以上事实和理由'):
        m = re.search(pat, s)
        if m and m.start() >= 12:
            return s[:m.start()]
    return s


def strip_browser_ui_tail(s: str) -> str:
    """字段末尾误入 Chrome 地址栏 a11y 文案等。"""
    t = s or ''
    for junk in _BROWSER_UI_TAIL:
        if junk in t:
            t = t[:t.index(junk)]
    return t.rstrip('。，、；; \t')


def panel_members_trustworthy(p: str) -> bool:
    """a11y/脏 OCR 名单：缺角色、混长数字、专利局印章噪声 → 不可信，改读 PDF。"""
    p = (p or '').strip()
    if not p or len(p) < 6:
        return False
    if re.search(r'\d{5,}', p):
        return False
    if '专利局' in p and '议组组长' not in p:
        return False
    return panel_complete(p) or sum(h in p for h in ('议组组长', '主审', '参审')) >= 2


# 合议组角色标准写法仅三种；OCR 常见形近字先归一再取姓名
_PANEL_ROLE_FIXES = (
    # 主审员 ← 王/三/圭/土…审员（版式上不可能出现「王审员」）
    (re.compile(r'[王三圭土主]审员?'), '主审员'),
    # 合议组组长 ← 纟且/厶议/且长 等
    (re.compile(r'合议[组纟厶][组且]?[组且]?长'), '合议组组长'),
    (re.compile(r'[组纟厶][且议]?组长'), '合议组组长'),
    (re.compile(r'议组组长'), '合议组组长'),
    (re.compile(r'且长'), '组长'),
    # 参审员 ← 叁审 等
    (re.compile(r'[参叁]审员?'), '参审员'),
)


def normalize_panel_role_text(s: str) -> str:
    """去空格后把角色误识纠到标准标签（合议组组长/主审员/参审员）。"""
    n = despace(s or '')
    if not n:
        return ''
    for pat, rep in _PANEL_ROLE_FIXES:
        n = pat.sub(rep, n)
    return n


def parse_panel_roles(items: list) -> str:
    """末页竖排合议组 → 标准「合议组组长：…；主审员：…；参审员：…」。"""
    roles = {}
    for tx, _bx in items or []:
        n = normalize_panel_role_text(tx or '')
        if not n or '专利局章' in n or re.search(r'\d{5,}', n):
            continue
        if '参审员' in n or n.startswith('参审'):
            m = re.search(r'参审员?[：:．.、；;]*(.+)$', n)
            name = re.sub(r'[^\u4e00-\u9fff]', '', (m.group(1) if m else ''))
            if 1 <= len(name) <= 4:
                roles['参审'] = name
            continue
        if '主审员' in n or n.startswith('主审'):
            m = re.search(r'主审员?[：:．.、；;]*(.+)$', n)
            name = re.sub(r'[^\u4e00-\u9fff]', '', (m.group(1) if m else ''))
            if 1 <= len(name) <= 4:
                roles['主审'] = name
            continue
        if '合议组组长' in n or '组长' in n:
            m = re.search(r'(?:合议组)?组长[：:．.、；;]*(.+)$', n)
            name = re.sub(r'[^\u4e00-\u9fff]', '', (m.group(1) if m else ''))
            if 1 <= len(name) <= 4:
                roles['议组组长'] = name
    if sum(1 for k in ('议组组长', '主审', '参审') if roles.get(k)) < 2:
        return ''
    bits = []
    if roles.get('议组组长'):
        bits.append('合议组组长：' + roles['议组组长'])
    if roles.get('主审'):
        bits.append('主审员：' + roles['主审'])
    if roles.get('参审'):
        bits.append('参审员：' + roles['参审'])
    return '；'.join(bits)


def _norm_summary(s: str) -> str:
    """要点正文：只去空白，保留 ，。；：等标点。"""
    return strip_browser_ui_tail(re.sub(r'\s+', '', s or ''))


def _summary_line_body(raw_tx: str) -> str:
    """决定要点行：去掉标签前缀，保留折行正文与标点。"""
    n = _norm_summary(raw_tx)
    if not n:
        return ''
    if '决定要点' in n:
        i = n.index('决定要点') + len('决定要点')
        n = re.sub(r'^[：:．.、；;]+', '', n[i:])
    if any(h in n.lower() for h in _JUNK_HINTS):
        return ''
    return n


def _collect_decision_summary_lines(items: list, lab: dict, nxt_t: int) -> list:
    """决定要点单元格：同行右侧 + 下方全宽折行（勿只收 t>标签底 且 _is_prose 首行）。"""
    lab_t, lab_b, lab_r = lab['t'], lab['b'], lab['r']
    y_max = max((b for _tx, (_l, _t, _r, b) in items), default=lab_b) 
    # 整窗底栏下载条（y≈屏底）勿进要点
    y_cut = int(y_max * 0.92) if y_max > lab_b + 80 else (1 << 30)
    picked = []
    for tx, (l2, t2, r2, b2) in items:
        if tx.startswith(_HL_PREFIX):
            continue
        if t2 >= y_cut:
            continue
        n0 = norm(tx)
        if not n0:
            continue
        key = n0.replace('凵', '日')
        if (key in CANON_ROWS and len(n0) <= 9 and '决定要点' not in n0
                and t2 >= lab_t - 2):
            continue
        overlap = min(lab_b, b2) - max(lab_t, t2)
        same_row = overlap >= 3 and (l2 > lab_r - 20 or '决定要点' in n0)
        below = t2 >= lab_b - 4 and t2 < nxt_t - 2
        if not (same_row or below):
            continue
        body = _summary_line_body(tx)
        if not body:
            continue
        if _is_sec_head(norm(body)):
            break
        if _cjk_len(body) < 2 and len(body) < 5:
            continue
        picked.append((t2, l2, body))
    picked.sort(key=lambda x: (x[0], x[1]))
    return [p[2] for p in picked]


_HL_PREFIX = '__HL__'


def find_hlines(png_path: str, x0: int | None = None, x1: int | None = None,
                y0: int | None = None, max_dark: int | None = None,
                min_run: int | None = None) -> list:
    """截图中的横向长直线（表格边框）检测（20:57 结构主规则，纯像素）。

    实跑标定（detail_20260901_202133，约 W≈1920）：行分隔 x772-1546（宽775）、
    底边框 x732-1587。229 分辨率/DPI 不同时绝对像素会漏检 → 按图宽比例。
    浏览器缘贴满扫描带的线用「带宽占比≥95%」排除（勿用 be<x1-8，正文线
    贴扫描右缘时会被误杀——229 漏检主因之一）。
    多档 max_dark 合并，吃抗锯齿浅灰线。返回 [(x1,y1,x2,y2,宽)]。
    """
    from PIL import Image
    img = Image.open(png_path).convert('L')
    W, H = img.size
    if W < 80 or H < 80:
        return []
    if x0 is None:
        x0 = max(40, int(W * 0.28))
    if x1 is None:
        x1 = min(W, max(x0 + 80, int(W * 0.92)))
    else:
        x1 = min(x1, W)
    if y0 is None:
        y0 = max(40, int(H * 0.06))
    if min_run is None:
        min_run = max(120, int(W * 0.12))
    band = max(1, x1 - x0)
    darks = (max_dark,) if max_dark is not None else (200, 230, 248)
    px = img.load()
    seen_y = set()
    out = []

    def _scan(md: int) -> None:
        y = y0
        while y < H - 4:
            run = best = 0
            bs = be = s = 0
            for x in range(x0, x1):
                if px[x, y] <= md:
                    if run == 0:
                        s = x
                    run += 1
                    if run > best:
                        best, bs, be = run, s, x
                else:
                    run = 0
            # 贴扫描带左右缘 = 浏览器上下缘；正文线须左右各留 ≥8px
            near = any(abs(y - sy) <= 2 for sy in seen_y)
            if (best >= min_run and bs > x0 + 8 and be < x1 - 8
                    and not near):
                seen_y.add(y)
                out.append((bs, y, be, y + 2, best))
                y += 3
            else:
                y += 1

    for md in darks:
        _scan(md)
    out.sort(key=lambda t: t[1])
    return out


def hline_items(png_path: str) -> list:
    """find_hlines → 伪 OCR 项 [('__HL__775', (x1,y1,x2,y2)), ...]，与文字项
    同序进 pages；落盘格式与真项一致（L|x,y,x,y|__HL__775），离线复算加载器
    无需改动。"""
    return [(f'{_HL_PREFIX}{w}', (a, b, c, d))
            for a, b, c, d, w in find_hlines(png_path)]


def shot_past_summary_table(items: list) -> bool:
    """本屏已越过要点表：见表格底边横线（线下无正文），或已出现「案由」等段题。

    供详情滚屏停机——横线原先只参与要点截断、不控鼠标，229 会一直滚过分页线。
    """
    if not items:
        return False
    for tx, bx in items:
        if str(tx).startswith(_HL_PREFIX) and not _prose_below(items, bx[3]):
            return True
    for tx, _bx in items:
        if str(tx).startswith(_HL_PREFIX):
            continue
        n = norm(tx)
        if n and _is_sec_head(n):
            return True
    return False


def _prose_below(items: list, y_line: int) -> bool:
    """横线下方（y > 线底+2）是否还有正文行（_is_prose）。
    有 → 线只是行间分隔，正文仍是单元格续文，不得停；
    全非正文（段题/短行/无）→ 线是表格收底，停（20:57 设计：不能一线一票停读，
    防止收割起点落在行分隔线上方时误截断）。"""
    for tx, bx in items:
        if tx.startswith(_HL_PREFIX) or bx[1] <= y_line + 2:
            continue
        n = norm(tx)
        if n and _is_prose(n):
            return True
    return False


def join_table_rows(items: list):
    """单屏带框行 → (字段表, 要点正文行列表)。

    标签行 = norm 后（凵→日 兜底）命中 CANON_ROWS 的**短行**（<=9 字）。
    值 = 与标签 y 区间交叠 ≥4px 且在其右侧 x0 > 标签右缘+10 的行，向下吸附
    同列续行（间隔 ≤16px、左缘漂移 ≤40px、未越过下一标签顶）。要点 = 标签
    下方的全宽正文行。
    """
    labels = []
    for text, (l, t, r, b) in items:
        if text.startswith(_HL_PREFIX):   # 横线伪项不参与表格解析
            continue
        n = norm(text)
        if not n:
            continue
        key = n.replace('凵', '日')
        if key in CANON_ROWS and len(n) <= 9:
            labels.append({'field': CANON_ROWS[key], 't': t, 'b': b, 'r': r})
    labels.sort(key=lambda d: d['t'])
    fields: dict = {}
    summary = None
    for i, lab in enumerate(labels):
        nxt_t = labels[i + 1]['t'] if i + 1 < len(labels) else 1 << 30
        if lab['field'] == 'decision_summary':
            summary = _collect_decision_summary_lines(items, lab, nxt_t)
            continue
        col = sorted((t2, b2, l2, tx)
                     for tx, (l2, t2, r2, b2) in items
                     if l2 > lab['r'] + 10 and t2 < nxt_t - 4
                     and min(lab['b'], b2) - max(lab['t'], t2) >= 4)
        if not col:
            continue
        take, cur_b, cur_l = [col[0]], col[0][1], col[0][2]
        for t2, b2, l2, tx in col[1:]:
            if t2 - cur_b <= 16 and abs(l2 - cur_l) <= 40:
                take.append((t2, b2, l2, tx))
                cur_b, cur_l = max(cur_b, b2), l2
        fields[lab['field']] = ''.join(despace(x[3]) for x in take)
    return fields, summary


def panel_complete(p: str) -> bool:
    """名单完整 = 组长+主审+参审三角色齐（收割停屏规则用）。"""
    p = p or ''
    return all(h in p for h in ('议组组长', '主审', '参审'))


def _panel_line(items: list) -> str:
    """合议组签名栏 → 名单串（厶/合 误识、同排拆碎片、竖排名值分列、
    参审多人折行均兼容）。

    版式实证：
      首页（page1-info.png y819-836）：同一排被 OCR 拆成三块
      '厶议组组长：任跷兰'｜'主审'｜'：文妒'，右侧同排还有落款
      '专利复审委员会'（须排除）；
      末页（20:21 tail3）：竖排**名值分列**——标签列 x≈776-866、姓名列
      x≈888+，**同行 y 区间重叠即同一字段**，按 (行,列) 序拼接后
      '合议组组长任晓兰主审员刘婷婷参审员刘静'（角色紧跟其名）。
    → 以首个命中行为锚，收集锚行 + 下方**链式续行**（每行与上一已收行
    底缘距 ≤45px 即续收——参审员多人折到第二行（无标签行）靠此收纳；
    '专利复审委员会'等落款被过滤后，链在 ~119px 间距处自然断），
    名单行 ≤24 字，按 (行,列) 序拼接。
    """
    hit = None
    for tx, (l, t, r, b) in items:
        n = norm(tx)
        if any(h in n for h in _PANEL_HINTS) and _cjk_len(n) >= 2:
            hit = (l, t, r, b)
            break
    if hit is None:
        return ''
    _, t0, _, b0 = hit
    if t0 == 0 and b0 == 0:                       # 无坐标兜底：只取本行
        for tx, _ in items:
            n = norm(tx)
            if any(h in n for h in _PANEL_HINTS):
                return n.replace('厶议组', '合议组')[:120]
        return ''
    cand = []
    for tx, (l, t, r, b) in items:
        if tx.startswith(_HL_PREFIX):      # 横线伪项不进名单
            continue
        n = norm(tx)
        if not n or len(n) > 24 or any(h in n.lower() for h in _JUNK_HINTS):
            continue
        if any(x in n for x in ('委员会', '盖章', '联系', '决定书', '专利局章')):
            continue
        if re.fullmatch(r'[\d\s、．.]+', n):
            continue
        cand.append((t, l, b, n))
    cand.sort()
    block, cur_b = [], b0
    for t, l, b, n in cand:                       # 自锚行起链式收纳
        if min(b, cur_b) - max(t, t0) >= 4:       # 锚行（y 重叠，含同排碎片）
            block.append((t, l, n))
            cur_b = max(cur_b, b)
        elif 0 <= t - cur_b <= 45:                # 下一排（链式窗口可折行）
            block.append((t, l, n))
            cur_b = max(cur_b, b)
    block.sort()
    raw = ''.join(n for _, _, n in block)
    return normalize_panel_role_text(raw).replace('厶议组', '合议组')[:120]


def _scan_result(pages: list) -> str:
    """勾选框结论：三选一行里**未带囗/□前缀**的即勾选项（'冈'=勾选误识）。"""
    hit_any = None
    for items in pages:
        for tx, _ in items:
            raw = (tx or '').strip()
            if not raw:
                continue
            n = norm(raw)
            if len(n) > 14:
                continue
            for w in _OUTCOMES:
                if w in n:
                    if n.startswith(('囗', '□')):
                        hit_any = hit_any or w
                    elif n.startswith('决定'):     # 旧版式 '决定：维持…'
                        return w
                    else:
                        return w                  # 勾选行（冈/☑/无前缀）
                    break
    return hit_any or ''


def parse_detail_pages(pages: list) -> dict:
    """多屏带框 OCR（自上而下收割顺序）→ 详情字段字典。

    跨屏规则：表格字段首屏命中优先；要点正文自标签屏起向下续读，后续屏
    顶部正文行续接（已捕获的表格值/标签去重防串），遇 合议组/主审员 行即
    收尾并捕获合议组名单。
    """
    out: dict = {}
    sum_norms: list = []           # 要点正文（norm 形式，保序）
    summary_started = False
    known: set = set()             # 已见标签 norm（防续读串入）
    panels: list = []              # 各屏签名栏候选（首页/末页各一处，末页更全）
    for items in pages:
        fields, summary = join_table_rows(items)
        for k, v in fields.items():
            if v and not out.get(k):
                out[k] = _extract(k, v) if k in _TYPE_FIELDS else v[:200]
        known |= {norm(t) for t, _ in items
                  if norm(t).replace('凵', '日') in CANON_ROWS}
        known |= {norm(v) for v in fields.values()}
        for tx, _ in items:                     # 文档头兜底（第4Wxxxxx号/第38430号）
            n = norm(tx)
            if not out.get('case_no'):
                m = re.search(r'第(\d{1,3}W\d{4,7})号', n)
                if m:
                    out['case_no'] = m.group(1)
            if not out.get('decision_no'):
                m = re.search(r'第(\d{4,7})号', n)
                if m and '决定' in n:
                    out['decision_no'] = m.group(1)
        if summary is not None:
            summary_started = True
            for tx in summary:
                n = _norm_summary(tx) if not any(
                    h in (tx or '').lower() for h in _JUNK_HINTS) else ''
                nk = norm(n)
                if n and nk not in known and n not in sum_norms:
                    sum_norms.append(n)
            # 表格单元格已收下足够要点 → 禁止跨页续灌（strip_browser_ui_tail
            # 会剥尾句号，不能靠 endswith。；205052：page2→末页灌到 1069）
            if len(''.join(sum_norms)) >= 80:
                summary_started = False
        elif summary_started:
            for tx, bx in items:
                if tx.startswith(_HL_PREFIX):    # 表格横线（底边框/行分隔）
                    if not _prose_below(items, bx[3]):   # 线下全非正文 → 收底，
                        summary_started = False          # 要点结束（结构主规则）
                        break
                    continue                     # 线下还有正文 → 行间分隔，跳过
                if any(h in (tx or '').lower() for h in _JUNK_HINTS):
                    continue
                n = _norm_summary(tx)
                nk = norm(n)
                if not n or nk in known or n in sum_norms:
                    continue
                if _is_sec_head(nk):             # 段题词表：文字兜底（20:57 泛化，
                    summary_started = False      # 不再写死单个"案由"）
                    break
                # 末页「决定的理由」正文特征：勿灌进要点
                if re.match(r'^证据\s*[0-9０-９]', n) or n.startswith('综上') \
                        or '根据以上事实和理由' in n:
                    summary_started = False
                    break
                if any(h in nk for h in _PANEL_HINTS):
                    pl = _panel_line(items)
                    if pl and pl not in panels:
                        panels.append(pl)
                    summary_started = False
                    break
                if not _is_prose(nk):
                    continue
                sum_norms.append(n)
                if n.endswith('”') or n.endswith('"'):
                    summary_started = False
                    break
    for items in pages:                         # 名单可出现在任意屏（首页/末页）
        pl = _panel_line(items)
        if pl and pl not in panels:
            panels.append(pl)
        pr = parse_panel_roles(items)
        if pr and pr not in panels:
            panels.append(pr)
    if panels:
        # 择优：角色数（组长/主审/参审）→字数；**同分取后见（末页）**。
        # 19:54 实跑：首页签名行与末页落款角色数/字数全同，仅 跷/晓 一字
        # 误识之差，max() 取首个会永远选首页误读版；末页落款是独立二次
        # 渲染源，同分必须让末页胜（设计约定：首页/末页各收一遍，择优）。
        def _pk(p):
            return (1 if panel_members_trustworthy(p) else 0,
                    sum(h in p for h in ('议组组长', '主审', '参审')),
                    _cjk_len(p))
        best = panels[0]
        for p in panels[1:]:
            if _pk(p) >= _pk(best):
                best = p
        out['panel_members'] = best[:120]
    if sum_norms:
        # 1200 上限：19:54 实跑要点本体约千字；段题/案由混入再截断
        out['decision_summary'] = strip_browser_ui_tail(
            _trim_summary_at_section(''.join(sum_norms)))[:1200]
    res = _scan_result(pages)
    if res:
        out['decision_result'] = res
    return out


def prep_crop(img_path: str, box, out_path: str, scale: int = 2) -> str:
    """从整窗截图裁内容区并放大（小字号 OCR 提精度），落盘返回路径。"""
    from PIL import Image
    im = Image.open(img_path)
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(im.width, x1), min(im.height, y1)
    if x1 - x0 < 40 or y1 - y0 < 40:
        raise RuntimeError(f'裁剪框无效: {box} / 图 {im.size}')
    crop = im.crop((x0, y0, x1, y1))
    if scale > 1:
        crop = crop.resize((crop.width * scale, crop.height * scale),
                           Image.LANCZOS)
    crop.save(out_path)
    return out_path


# ―― 自测：2026-09-01 ocr_synth_test.png 的 Windows OCR 实际输出（逐字冻结） ――
_FIXTURE_LINES = [
    '无 效 宣 告 请 求 审 查 决 定',
    '冫 央 定 号 ． ． 38430',
    '案 亻 牛 编 号 ． 4W107577',
    '决 定 日 ． 2018 年 12 月 28 日',
    '无 效 宣 告 请 求 人 ： 李 瑶',
    '专 利 权 人 远 大 医 药 （ 天 津 ） 有 限 公 司',
    '法 律 依 据 ． 专 利 法 第 二 十 二 条 第 三 款',
    '决 定 要 点 全 文 ． “ ． 如 果 本 领 域 技 术 人 员 ． ．',
    '决 定 ． 维 持 专 利 权 有 效',
    '合 议 组 组 长 ： 张 三 主 审 员 ： 李 四 参 审 员 ． 王 五',
]


def selftest() -> int:
    # ―― _decode：PowerShell stdout 编码自适配（19:02 实跑 GBK→U+FFFD 教训） ――
    dec_fails = []
    if _decode('中文OCR'.encode('gbk')) != '中文OCR':
        dec_fails.append('gbk')
    if _decode('中文OCR'.encode('utf-8')) != '中文OCR':
        dec_fails.append('utf-8')
    if _decode('\ufeffL|1,2,3,4|abc'.encode('utf-8')) != 'L|1,2,3,4|abc':
        dec_fails.append('bom')
    if dec_fails:
        print(f'SELFTEST FAIL: _decode {dec_fails}')
        return 1

    d = parse_ocr_lines(_FIXTURE_LINES)
    exp = {
        'decision_no': '38430', 'case_no': '4W107577',
        'decision_date': '20181228', 'petitioner': '李瑶',
        'patentee': '远大医药（天津）有限公司',
        'legal_basis': '专利法第二十二条第三款',
        'decision_result': '维持专利权有效',
    }
    fails = [f'{k}: got={d.get(k)!r} want={v!r}' for k, v in exp.items()
             if d.get(k) != v]
    if '维持' in d.get('decision_summary', ''):
        fails.append(f'结论行回灌 decision_summary: {d.get("decision_summary")!r}')

    # ―― 真实两页带框 OCR 回放（page1+page2 冻结夹具） ――
    try:
        import ocr_fixtures_detail as fx
        d2 = parse_detail_pages([fx.PAGE1, fx.PAGE2])
        exp2 = {
            'case_no': '4W107577', 'decision_no': '38430',
            'decision_date': '20181221', 'request_date': '20180724',
            'legal_basis': '专利法第22条第2款，第22条第3款',
            'petitioner': '李瑶', 'class_no': 'C07C229/24',
            'decision_result': '维持专利权有效',
        }
        fails += [f'[real] {k}: got={d2.get(k)!r} want={v!r}'
                  for k, v in exp2.items() if d2.get(k) != v]
        if not (d2.get('invention_title', '').find('琥珀酸酯') > 0
                and d2['invention_title'].endswith('晶体')):
            fails.append(f"[real] invention_title 残缺: {d2.get('invention_title')!r}")
        if not d2.get('decision_summary', '').startswith('在无效程序中'):
            fails.append(f"[real] 要点首行错: {d2.get('decision_summary')[:30]!r}")
        if '三步法' not in d2.get('decision_summary', ''):
            fails.append('[real] 要点未续读到第 2 段（三步法缺失）')
        # 签名栏夹具：厶→厶误识锚词 + 同排碎片拼块 + 落款排除
        psig = parse_detail_pages([fx.PAGE_SIG])
        pm = psig.get('panel_members', '')
        if not (pm.startswith('合议组组长') and '主审' in pm
                and '委员会' not in pm):
            fails.append(f'[sig] 签名栏捕获错: {pm!r}')
        # 高清同排完整形态：组长+主审员+参审员一行（刘婷婷/刘静 应全对）
        psig2 = parse_detail_pages([fx.PAGE_SIG2])
        pm2 = psig2.get('panel_members', '')
        if pm2 != '合议组组长任跷兰主审员刘婷婷参审员刘静':
            fails.append(f'[sig2] 高清签名行捕获错: {pm2!r}')
        # 末页竖排名值分列：全名逐字全对（含 晓），且是多屏候选择优胜者
        psig3 = parse_detail_pages([fx.PAGE_SIG, fx.PAGE_SIG3])
        pm3 = psig3.get('panel_members', '')
        if pm3 != '合议组组长任晓兰主审员刘婷婷参审员刘静':
            fails.append(f'[sig3] 末页名单/择优错: {pm3!r}')
        if not panel_complete(pm3) or panel_complete('合议组组长任跷兰主审文妒'):
            fails.append('[sig3] panel_complete 判定错')
        # 同分择优取末页（2026-09-01 19:54 实跑）：首页签名行与末页落款
        # 角色数/字数全同，仅 跷/晓 一字之差 → 后见（末页）必须胜出
        pg_a = [('合议组组长：任跷兰 主审员：刘婷婷 参审员：刘静',
                 (750, 1009, 904, 1026))]
        pg_b = [('合议组组长：任晓兰 主审员：刘婷婷 参审员：刘静',
                 (760, 405, 910, 422))]
        pm4 = parse_detail_pages([pg_a, pg_b]).get('panel_members', '')
        if pm4 != '合议组组长任晓兰主审员刘婷婷参审员刘静':
            fails.append(f'[sig4] 同分未取末页: {pm4!r}')
        # 参审员多人折行（20:25 用户指出）：名单折到第二行（无标签行）
        # 须链式收纳进参审员；落款 委员会 行过滤后链自然断
        pg5 = [('合 议 组 组 长 ：', (776, 531, 866, 547)),
               ('主 审 员 ；', (777, 558, 866, 574)),
               ('参 审 员', (776, 584, 860, 601)),
               ('任 晓 兰', (888, 531, 939, 547)),
               ('刘 婷 婷', (888, 558, 938, 574)),
               ('刘 静', (888, 584, 921, 600)),
               ('王 芳', (888, 611, 921, 627)),
               ('专 利 复 审 委 员 会', (1343, 720, 1462, 736))]
        pm5 = parse_detail_pages([pg5]).get('panel_members', '')
        if pm5 != '合议组组长任晓兰主审员刘婷婷参审员刘静王芳':
            fails.append(f'[sig5] 参审多人折行漏收: {pm5!r}')
        # 要点引号收束：续读行以 ” 结尾即停，其后案由行不得混入
        q1 = [('决 定 要 点 ；', (709, 743, 785, 761)),
              ('在 无 效 程 序 中 如 果 现 有 技 术 没 有 公 开 晶 型',
               (744, 784, 1460, 801))]
        q2 = [('±0，1。处都不具有衍射峰。”', (738, 155, 962, 170)),
              ('李瑶于2018年07月向专利复审委员会提出无效宣告请求以权利要求不符合',
               (772, 193, 1584, 210))]
        qs = parse_detail_pages([q1, q2]).get('decision_summary', '')
        if not qs.endswith('衍射峰。”') or '李瑶' in qs:
            fails.append(f'[quote] 要点未在引号处收束: {qs!r}')
        # 段题收束（20:50 用户反馈）：要点真末行无引号（原文「创造性。。」双句号），
        # 下一屏是 '、 案 由' 段题碎片 + 案由正文 → 必须止于段题，不得吞正文
        h1 = [('决 定 要 点 ；', (709, 743, 785, 761)),
              ('否 定 涉 案 专 利 的 创 造 性 。 。', (744, 784, 1460, 801))]
        h2 = [('、 案 由', (109, 493, 159, 510)),
              ('本专利的专利号为2m610004173，6，优先权日为2000年02月22日，授',
               (126, 533, 938, 551))]
        hs = parse_detail_pages([h1, h2]).get('decision_summary', '')
        if not hs.endswith('创造性。。') or '本专利' in hs or '优先权日' in hs:
            fails.append(f'[sechead] 要点未在【一、案由】段题处收束: {hs!r}')
        # 横线收束（20:57 结构主规则）：像素检线 + 线下无正文→停 / 有正文→不停
        import tempfile
        from PIL import Image as _Img
        _img = _Img.new('L', (1800, 600), 255)
        for yy in (300, 301):                     # 真表格边框：700px 宽、1-2px 高
            for xx in range(800, 1500):
                _img.putpixel((xx, yy), 90)
        for xx in range(640, 1700):               # 贴满扫描区间 → 浏览器缘，须排除
            _img.putpixel((xx, 200), 90)
        _p = os.path.join(tempfile.gettempdir(), 'cnipa_hl_test.png')
        _img.save(_p)
        hl = find_hlines(_p)
        if len(hl) != 1 or hl[0][1] not in (300, 301) or not 690 <= hl[0][4] <= 710:
            fails.append(f'[hline] 横线检测/贴界排除失效: {hl!r}')
        t1 = [('决 定 要 点 ；', (709, 743, 785, 761)),
              ('在 无 效 程 序 中 如 果 现 有 技 术 没 有 公 开 晶 型', (744, 784, 1460, 801))]
        t2 = [('能 仅 因 为 无 法 将 涉 案 专 利 否 定 其 创 造 性 。 。', (785, 513, 1502, 551)),
              ('__HL__775', (772, 562, 1546, 564)),
              ('__HL__856', (732, 979, 1587, 981)),
              ('、 案 由', (755, 992, 805, 1010))]
        ts = parse_detail_pages([t1, t2]).get('decision_summary', '')
        if not ts.endswith('创造性。。') or '__HL__' in ts or '案由' in ts:
            fails.append(f'[hline-stop] 线下无正文未收底: {ts!r}')
        t3 = [('能 仅 因 为 无 法 将 涉 案 专 利 否 定 其 创 造 性 。 。', (785, 513, 1502, 551)),
              ('__HL__775', (772, 562, 1546, 564)),
              ('、 案 由', (755, 600, 805, 618)),
              ('本专利的专利号为2m610004173优先权日为2000年02月22日授权公告日',
               (736, 640, 1584, 658))]
        ts2 = parse_detail_pages([t1, t3]).get('decision_summary', '')
        if not ts2.endswith('创造性。。') or '本专利' in ts2:
            fails.append(f'[hline-skip] 线下有正文时误停或吞正文: {ts2!r}')
        # 新决定末页：字间空格 + 组长/主审误识 + 印章数字不得盖名单
        pg_tail = [
            ('合 议 纟 且 纟 且 长 ： 熊 婷', (150, 1965, 380, 1994)),
            ('王 审 员 ： 吴 士 芬', (148, 2008, 351, 2038)),
            ('专 利 局 章', (983, 2008, 1353, 2060)),
            ('参 审 员 ： 杜 宇', (149, 2051, 321, 2081)),
            ('7 0 8 1 3 6 、', (1193, 2096, 1301, 2118)),
        ]
        pg_stamp = [('专利局章参审员杜宇708136', (900, 100, 1400, 140))]
        pmt = parse_detail_pages([pg_stamp, pg_tail]).get('panel_members', '')
        if not (panel_members_trustworthy(pmt) and '吴士芬' in pmt and '杜宇' in pmt
                and '熊婷' in pmt and '王审' not in pmt and '708136' not in pmt):
            fails.append(f'[sig6] 末页合议组未胜过印章: {pmt!r}')
        if normalize_panel_role_text('王审员：吴士芬') != '主审员：吴士芬':
            fails.append('[sig6b] 王审员应归一为主审员')
    except FileNotFoundError as e:
        fails.append(f'[real] 夹具缺失: {e}')

    if fails:
        print('SELFTEST FAIL:\n' + '\n'.join(fails))
        return 1
    print('SELFTEST PASS (合成锚词 8 字段 + 真实两页表格连/结论/要点续读全对)')
    return 0


if __name__ == '__main__':
    import sys
    raise SystemExit(selftest())
