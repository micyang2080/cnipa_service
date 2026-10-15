# -*- coding: utf-8 -*-
"""本地持久缓存（docs/cnipa-mcp-本地缓存要求.md，2026-09-03）。

复审/无效决定是已发布的历史文书，内容稳定 → **无 TTL**（区别于可变数据）。
检索/详情/附图三类结果落本地；各 runner 先查缓存、未命中再访国知局；
命中时返回与线上一致的契约 JSON（调用方仅多出可忽略的 from_cache/cache_file）。

目录（默认根 = {repo}/output/cnipa_cache，CNIPA_CACHE_ROOT 可覆盖）：
    cnipa_cache/
    ├── search/{library_type}/{query_key}.json     # 检索契约 JSON
    ├── detail/{library_type}/{application_no}__{case_no}.json
    ├── detail/.../{app}__{case}.a11y.json / .ocr.json  # 通道对照（非契约）
    └── figures/{application_no}.png               # 摘要附图（含点 13 位名）
        figures/{application_no}.no_figure         # 负缓存标记（确认无附图）
        另：CNIPA_FILE_ROOT/{app}/{app}.png 与 figures 同等命中（免再抓 PSS）

开关：CNIPA_CACHE_DISABLED=1 → 全局跳过读/写（联调用）。
      入参 force_refresh=true → 跳过读缓存，成功后覆盖写。

自测（离线，临时目录，不碰网络/窗口）：
    python cnipa_cache.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cnipa_contract   # dotted_appno / review_type_of（本模块对其 lazy 反向引用）

DISABLED_ENV = 'CNIPA_CACHE_DISABLED'
ROOT_ENV = 'CNIPA_CACHE_ROOT'

# 契约 12 项检索键（与 src/index.ts FILTER_KEYS 对齐）
CACHE_FILTER_KEYS = (
    'application_no', 'ipc_class', 'decision_no', 'case_no',
    'invention_title', 'patent_type', 'patentee', 'petitioner',
    'panel_leader', 'panel_examiner', 'panel_member', 'agency',
)


# ---------------------------------------------------------------- 根路径与开关

def cache_disabled() -> bool:
    return os.environ.get(DISABLED_ENV, '').strip() == '1'


def cache_root() -> str:
    """缓存根：{CNIPA_CACHE_ROOT} 或默认 {repo}/output/cnipa_cache。"""
    env = os.environ.get(ROOT_ENV, '').strip()
    if env:
        return os.path.abspath(env)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, 'output', 'cnipa_cache')


def _lib_token(library_type) -> str:
    return 'reexam' if cnipa_contract.review_type_of(library_type) == 1 else 'invalid'


def _norm_max_items(max_items) -> int:
    try:
        n = int(max_items)
    except (TypeError, ValueError):
        n = 50
    return min(max(1, n), 500)


# ---------------------------------------------------------------- 键与路径（§3）

def norm_filters(filters: dict) -> dict:
    """filters 规范化：仅契约 12 键；值 strip()；空串省略；
    application_no / case_no 走 dotted_appno（去 CN/ZL/空白、统一含点/大写）。"""
    raw = filters if isinstance(filters, dict) else {}
    out = {}
    for k in CACHE_FILTER_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        if k in ('application_no', 'case_no'):
            s = cnipa_contract.dotted_appno(s)
        out[k] = s
    return out


def search_key(library_type, filters: dict, max_items) -> str:
    """query_key = sha256(json({library_type, filters 规范化, max_items},
    sort_keys=True))[:16]；page 不入键（内部自动翻页至 max_items，语义不变）。"""
    lib = _lib_token(library_type)
    k = json.dumps({'library_type': lib,
                    'filters': norm_filters(filters),
                    'max_items': _norm_max_items(max_items)},
                   ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(k.encode('utf-8')).hexdigest()[:16]


def search_path(library_type, filters: dict, max_items) -> str:
    return os.path.join(cache_root(), 'search', _lib_token(library_type),
                        search_key(library_type, filters, max_items) + '.json')


def detail_path(library_type, application_no, case_no) -> str:
    """detail/{lib}/{application_no}__{case_no}.json；缺侧 `_` 占位
    （占位名仅规范兼容——写入端要求双号齐全，见 write_detail_cache 门禁）。"""
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    return os.path.join(cache_root(), 'detail', _lib_token(library_type),
                        '%s__%s.json' % (app or '_', case or '_'))


def figure_path(application_no) -> str:
    return os.path.join(cache_root(), 'figures',
                        cnipa_contract.dotted_appno(application_no) + '.png')


def no_figure_path(application_no) -> str:
    return figure_path(application_no) + '.no_figure'


def detail_a11y_path(library_type, application_no, case_no) -> str:
    """与详情 JSON 同侧：{app}__{case}.a11y.json（通道对照，非契约）。"""
    p = detail_path(library_type, application_no, case_no)
    return (p[:-5] if p.endswith('.json') else p) + '.a11y.json'


def detail_ocr_path(library_type, application_no, case_no) -> str:
    """与详情 JSON 同侧：{app}__{case}.ocr.json（PDF OCR 通道对照）。"""
    p = detail_path(library_type, application_no, case_no)
    return (p[:-5] if p.endswith('.json') else p) + '.ocr.json'


def file_root_figure_candidates(application_no) -> list:
    """交付目录附图候选：{CNIPA_FILE_ROOT}/{app}/{app}.png 及历史 figure.png。"""
    app = cnipa_contract.dotted_appno(application_no)
    if not app:
        return []
    root = cnipa_contract.file_root()
    return [
        os.path.join(root, app, app + '.png'),
        os.path.join(root, app, 'figure.png'),
    ]


def decision_file_path(application_no, case_no) -> str:
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    return os.path.join(cache_root(), 'decision_files',
                        '%s__%s.pdf' % (app or '_', case or '_'))


def no_decision_file_path(application_no, case_no) -> str:
    return decision_file_path(application_no, case_no) + '.no_file'


def decision_meta_path(application_no, case_no) -> str:
    """与缓存 PDF 同侧：decision_files/{app}__{case}.pdf.meta.json。"""
    return decision_file_path(application_no, case_no) + '.meta.json'


def _norm_decision_type(v) -> str:
    s = str(v or '').strip().lower()
    if s in ('new', 'old'):
        return s
    return ''


def write_decision_meta(application_no, case_no, decision_type,
                        method: str = '') -> str | None:
    """落盘新旧决定标记（页内判定结果；进页分流不再依赖此文件）。"""
    if cache_disabled():
        return None
    dt = _norm_decision_type(decision_type)
    if not dt:
        return None
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return None
    obj = {'decision_type': dt, 'method': str(method or '').strip()}
    # 缓存侧
    cp = write_json(decision_meta_path(app, case), obj)
    # 与对外 PDF 同目录（file_root/{app}/{case}.pdf.meta.json）
    try:
        disk = cnipa_contract.decision_file_disk_path(app, case)
        if disk:
            write_json(disk + '.meta.json', obj)
    except Exception:
        pass
    return cp


def load_decision_meta(application_no, case_no):
    """读 decision_type 元数据：先 file_root 旁路，再 cache 侧。"""
    if cache_disabled():
        return None
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return None
    try:
        disk = cnipa_contract.decision_file_disk_path(app, case)
        if disk:
            m = read_json(disk + '.meta.json')
            if isinstance(m, dict) and _norm_decision_type(m.get('decision_type')):
                return m
    except Exception:
        pass
    m = read_json(decision_meta_path(app, case))
    if isinstance(m, dict) and _norm_decision_type(m.get('decision_type')):
        return m
    return None


def infer_decision_type_from_pdf(pdf_path: str) -> str:
    """无 meta 时从 PDF 推断：Chrome 打印→new；整页图合成→old。"""
    if not pdf_path or not os.path.isfile(pdf_path):
        return ''
    try:
        import fitz
    except Exception:
        return ''
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return ''
    try:
        meta = doc.metadata or {}
        prod = ('%s %s' % (meta.get('producer') or '',
                           meta.get('creator') or '')).lower()
        if any(x in prod for x in (
                'chromium', 'chrome', 'webkit', 'microsoft edge', 'skia',
                'pdfium', 'headlesschrome')):
            return 'new'
        text_len = 0
        img_pages = 0
        n = min(len(doc), 3)
        for i in range(n):
            page = doc[i]
            text_len += len((page.get_text('text') or '').strip())
            try:
                if page.get_images():
                    img_pages += 1
            except Exception:
                pass
        if text_len < 80 and img_pages >= max(1, n // 2):
            return 'old'
        if text_len >= 200:
            return 'new'
        return ''
    finally:
        try:
            doc.close()
        except Exception:
            pass


def resolve_decision_type(application_no, case_no, pdf_path: str = '') -> str:
    """old|new|''：meta 优先，其次 PDF 推断。"""
    m = load_decision_meta(application_no, case_no)
    if m:
        dt = _norm_decision_type(m.get('decision_type'))
        if dt:
            return dt
    return infer_decision_type_from_pdf(pdf_path) or ''


def pdf_on_file_root(application_no, case_no) -> bool:
    """交付目录 CNIPA_FILE_ROOT/{app}/{case}.pdf 非空即视为已有 PDF。"""
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return False
    try:
        disk = cnipa_contract.decision_file_disk_path(app, case)
        return bool(disk and os.path.isfile(disk) and os.path.getsize(disk) > 0)
    except OSError:
        return False


def pdf_available(application_no, case_no) -> bool:
    """对外 PDF 路径或 decision_files 缓存侧任一侧非空即视为已有 PDF。"""
    if pdf_on_file_root(application_no, case_no):
        return True
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return False
    c = load_decision_file(app, case)
    return c is not None and c.get('kind') == 'pdf'


def ensure_decision_pdf_on_file_root(application_no, case_no) -> bool:
    """交付盘缺 PDF 时从 decision_files 缓存拷贝；成功则 True。"""
    if pdf_on_file_root(application_no, case_no):
        return True
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return False
    c = load_decision_file(app, case)
    if c is None or c.get('kind') != 'pdf':
        return False
    src = str(c.get('path') or '')
    dp = cnipa_contract.decision_file_disk_path(app, case)
    if not src or not dp or not os.path.isfile(src):
        return False
    try:
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copyfile(src, dp)
        return os.path.isfile(dp) and os.path.getsize(dp) > 0
    except OSError:
        return False


def detail_bundle_ready(library_type, application_no, case_no) -> bool:
    """详情 JSON 与交付盘 PDF 同时具备 → 可跳过进页。"""
    if load_detail(library_type, application_no, case_no) is None:
        return False
    if pdf_on_file_root(application_no, case_no):
        return True
    return ensure_decision_pdf_on_file_root(application_no, case_no)


# ---------------------------------------------------------------- 读写原语

def read_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path: str, obj) -> str:
    """原子写（tmp + os.replace），自动建目录。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def copy_figure(src: str, dst: str) -> str:
    """缓存 PNG → save_path。扩展名相同字节拷贝；目标为其它图片格式时
    PIL 转码（不做「png 字节改名 .jpg」的假文件，与 runner 04 惯例一致）。"""
    dst = os.path.abspath(str(dst))
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    src_ext = os.path.splitext(src)[1].lower()
    dst_ext = os.path.splitext(dst)[1].lower()
    if src_ext and dst_ext and src_ext != dst_ext:
        from PIL import Image
        with Image.open(src) as im:
            im.save(dst)
    else:
        shutil.copyfile(src, dst)
    return dst


# ---------------------------------------------------------------- 业务读（含命中判定）

def load_search(library_type, filters: dict, max_items):
    """检索缓存命中 → 契约 JSON；未命中/禁用 → None。
    命中失效规则（§3.2 边界）：缓存 truncated=true 且本次 max_items >
    缓存 items 数 → 视为未命中（缓存可能不完整，重查覆盖）。"""
    if cache_disabled():
        return None
    obj = read_json(search_path(library_type, filters, max_items))
    if not isinstance(obj, dict) or obj.get('success') is not True:
        return None
    if obj.get('truncated') is True:
        n_items = len(obj.get('items') or [])
        if _norm_max_items(max_items) > n_items:
            return None
    return obj


def load_detail(library_type, application_no, case_no):
    """详情缓存命中 → {'success': True, 'detail': {…20 键…}}；否则 None。
    （占位名文件不会被写入端产出，缺侧入参自然 miss。）"""
    if cache_disabled():
        return None
    obj = read_json(detail_path(library_type, application_no, case_no))
    if isinstance(obj, dict) and obj.get('success') is True \
            and isinstance(obj.get('detail'), dict):
        return obj
    return None


def _resolve_figure_png(app, save_path=None):
    """正缓存：cache/figures 或交付盘 PNG；save_path 非空时按需拷贝。"""
    png = figure_path(app)
    try:
        if os.path.isfile(png) and os.path.getsize(png) > 0:
            if save_path:
                return {'kind': 'png', 'path': copy_figure(png, str(save_path))}
            return {'kind': 'png', 'path': os.path.abspath(png)}
    except OSError:
        pass
    try:
        for disk in file_root_figure_candidates(app):
            if os.path.isfile(disk) and os.path.getsize(disk) > 0:
                abs_disk = os.path.abspath(disk)
                if save_path:
                    sp = os.path.abspath(str(save_path))
                    if sp == abs_disk:
                        return {'kind': 'png', 'path': abs_disk}
                    return {'kind': 'png', 'path': copy_figure(disk, str(save_path))}
                return {'kind': 'png', 'path': abs_disk}
    except OSError:
        pass
    return None


def load_figure(application_no, save_path=None):
    """附图缓存判定（§3.4）：
    {'kind': 'no_figure'}        → 负缓存命中（此前确认无附图，且盘上无图）
    {'kind': 'png', 'path': 绝对路径} → PNG 命中（给了 save_path 则拷贝/转码过去）
    None                          → 未命中/禁用。

    正缓存优先于 .no_figure（交付盘后补图 / 人工放图时不应被旧负缓存挡住）。
    """
    if cache_disabled():
        return None
    app = cnipa_contract.dotted_appno(application_no)
    hit = _resolve_figure_png(app, save_path)
    if hit is not None:
        return hit
    try:
        if os.path.isfile(no_figure_path(app)):
            return {'kind': 'no_figure'}
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- 业务写

def write_figure(application_no, src_path: str):
    """抓图成功 → 写入 figures/{application_no}.png（覆盖写）。

    抓图产物可能按调用方 save_path 扩展名另存为 jpg 等 → 走 copy_figure
    同款转码（PIL），不做「jpg 字节改名 .png」的假文件。"""
    if cache_disabled():
        return None
    return copy_figure(src_path, figure_path(application_no))


def write_no_figure(application_no):
    """确认无附图 → 写空标记文件（负缓存；人工删文件可重试）。"""
    if cache_disabled():
        return None
    p = no_figure_path(application_no)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, 'w', encoding='utf-8') as f:
            f.write('')
    return p


def clear_no_figure(application_no):
    """force_refresh 时清除负缓存标记（有图重抓后不应被旧标记挡住）。"""
    try:
        os.remove(no_figure_path(application_no))
    except OSError:
        pass


def load_decision_file(application_no, case_no):
    if cache_disabled():
        return None
    app = cnipa_contract.dotted_appno(application_no)
    case = str(case_no or '').strip()
    if not app or not case:
        return None
    try:
        if os.path.isfile(no_decision_file_path(app, case)):
            return {'kind': 'no_file'}
    except OSError:
        pass
    pdf = decision_file_path(app, case)
    try:
        if os.path.isfile(pdf) and os.path.getsize(pdf) > 0:
            return {'kind': 'pdf', 'path': os.path.abspath(pdf)}
    except OSError:
        pass
    return None


def write_decision_file(application_no, case_no, src_path: str) -> str:
    if cache_disabled():
        return None
    dst = decision_file_path(application_no, case_no)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src_path, dst)
    return dst


def write_no_decision_file(application_no, case_no) -> str:
    if cache_disabled():
        return None
    p = no_decision_file_path(application_no, case_no)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, 'w', encoding='utf-8') as f:
            f.write('')
    return p


# ---------------------------------------------------------------- 自测（离线）

def _selftest() -> int:
    import tempfile

    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    saved = {k: os.environ.get(k) for k in (
        ROOT_ENV, DISABLED_ENV, cnipa_contract.FILE_ROOT_ENV)}
    tmp = tempfile.mkdtemp(prefix='cnipa_cache_st_')
    os.environ[ROOT_ENV] = tmp
    os.environ[cnipa_contract.FILE_ROOT_ENV] = os.path.join(tmp, 'file_root_empty')
    os.environ.pop(DISABLED_ENV, None)
    try:
        # 1) norm_filters：strip / 空串省略 / 非契约键忽略 / 申请号含点 / 案件号大写
        nf = norm_filters({'application_no': ' CN2006100041736 ', 'case_no': '4w107577',
                           'patentee': '  远大  ', 'ipc_class': '', 'foo': 'x'})
        check(nf == {'application_no': '200610004173.6', 'case_no': '4W107577',
                     'patentee': '远大'}, 'norm_filters 不符: %r' % nf)

        # 2) search_key：稳定 / max_items 入键 / 库入键 / filters 键序无关 / page 无关
        k1 = search_key('invalid', {'application_no': '200610004173.6'}, 50)
        k2 = search_key('invalid', {'application_no': 'CN200610004173.6',
                                    'extra': 1}, 50)
        k3 = search_key('invalid', {'application_no': '200610004173.6'}, 100)
        k4 = search_key('reexam', {'application_no': '200610004173.6'}, 50)
        k5 = search_key('无效决定库', {'application_no': '2006100041736'}, '50')
        check(k1 == k2, '同语义 filters 应同键: %s vs %s' % (k1, k2))
        check(k1 != k3, '不同 max_items 应异键')
        check(k1 != k4, '不同库应异键')
        check(k1 == k5, '库别名/无点申请号应归一同键')
        check(len(k1) == 16 and all(c in '0123456789abcdef' for c in k1),
              'query_key 应为 16 位 hex')

        # 3) 检索 round-trip：契约投影写入 → load 命中；from_cache/cache_file 不入文件
        payload = {'ok': True, 'library_radio': '无效决定库', 'total_after': 1,
                   'results': [{'title': '吡啶酮衍生物', 'case_no': '4W107577',
                                'app_no': 'CN2006100041736',
                                'issue_date': '20181221'}]}
        p = cnipa_contract.write_search_cache('invalid', {'application_no': '200610004173.6'},
                                              50, payload)
        check(isinstance(p, str) and os.path.isfile(p), 'write_search_cache 未落盘: %r' % p)
        cached = load_search('invalid', {'application_no': 'CN2006100041736'}, 50)
        check(cached is not None, 'load_search 未命中')
        if cached:
            check(cached.get('success') is True and cached.get('total') == 1,
                  '缓存内容不符: %r' % cached)
            check('from_cache' not in cached and 'cache_file' not in cached,
                  '缓存文件不应含 from_cache/cache_file')
            it = cached['items'][0]
            check(it['application_no'] == '200610004173.6' and
                  it['detail_url'].endswith('anjianbh=4W107577&searchType=8'),
                  '缓存 items 投影不符')
        check(load_search('invalid', {'application_no': '200610004173.6'}, 100) is None,
              '未写过的 max_items 应 miss')

        # 3b) truncated 失效：同键（max_items=5）缓存 3 条 truncated → 同请求 miss；
        #     不同 max_items 本就是不同键（天然 miss，与 truncated 无关）
        trunc_payload = dict(payload, total_after=8, truncated=True,
                             results=[dict(payload['results'][0], case_no='C%d' % i)
                                      for i in range(3)])
        p2 = cnipa_contract.write_search_cache('invalid', {'case_no': 'X'}, 5, trunc_payload)
        check(os.path.isfile(p2 or ''), 'truncated 写入失败')
        check(load_search('invalid', {'case_no': 'X'}, 5) is None,
              'truncated 缓存（3/8 条）同请求应 miss 重查')
        check(load_search('invalid', {'case_no': 'X'}, 3) is None,
              '不同 max_items 是不同键，应 miss')

        # 4) 详情路径与门禁
        dp = detail_path('invalid', 'CN200610004173.6', '4W107577')
        check(dp.endswith(os.path.join('detail', 'invalid',
                                       '200610004173.6__4W107577.json')),
              'detail_path 不符: %s' % dp)
        check(os.path.basename(detail_path('reexam', '', 'C')) == '___C.json',
              '缺申请号应 _ 占位: %s' % detail_path('reexam', '', 'C'))
        d_payload = {'ok': True, 'library_radio': 'invalid',
                     'detail': {'case_no': '4W107577', 'decision_no': '38430',
                                'decision_date': '20181221', 'invention_title': 'T',
                                'application_no': 'CN2006100041736',
                                'decision_result': '维持专利权有效'}}
        wp = cnipa_contract.write_detail_cache('invalid', '2006100041736', '4W107577',
                                               d_payload)
        check(isinstance(wp, str) and os.path.isfile(wp), 'write_detail_cache 未落盘')
        got = load_detail('invalid', 'CN200610004173.6', '4W107577')
        check(got is not None and got['detail']['application_no'] == '200610004173.6',
              'load_detail 未命中/内容不符')
        check(tuple(got['detail'].keys()) == cnipa_contract.CASE_OUT_KEYS,
              '缓存 detail 键集不符')
        # 缺一侧号 → 不写缓存
        miss = cnipa_contract.write_detail_cache('invalid', '2006100041736', '', d_payload)
        check(miss is None, '缺 case_no 不应写详情缓存')

        # 4b) 决定 PDF 新旧 meta
        check(write_decision_meta('200610004173.6', '4W107577', 'old',
                                  method='page_merge'),
              'write_decision_meta 应落盘')
        meta = load_decision_meta('CN200610004173.6', '4W107577')
        check(isinstance(meta, dict) and meta.get('decision_type') == 'old',
              'load_decision_meta 未命中: %r' % meta)
        check(resolve_decision_type('200610004173.6', '4W107577') == 'old',
              'resolve_decision_type 应读 meta')
        check(not detail_bundle_ready('invalid', '200610004173.6', '4W107577'),
              '仅有 detail JSON 时 bundle 未齐')
        fake_pdf = os.path.join(tmp, 'fake.pdf')
        with open(fake_pdf, 'wb') as f:
            f.write(b'%PDF-1.4\n')
        write_decision_file('200610004173.6', '4W107577', fake_pdf)
        check(ensure_decision_pdf_on_file_root('200610004173.6', '4W107577'),
              'ensure 应从 decision_files 拷到交付盘')
        check(detail_bundle_ready('invalid', '200610004173.6', '4W107577'),
              'detail+pdf 应 bundle ready')

        # 5) 附图：写 png → 命中；save_path 拷贝；size=0 视为 miss；负缓存标记
        app = '200610004173.6'
        src_png = os.path.join(tmp, 'src.png')
        with open(src_png, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\nFAKE')
        fp = write_figure(app, src_png)
        check(os.path.isfile(fp), 'write_figure 未落盘')
        h = load_figure('CN2006100041736')
        check(h is not None and h['kind'] == 'png' and
              h['path'] == os.path.abspath(figure_path(app)), 'load_figure 未命中: %r' % h)
        # 5b) 交付盘命中：cache/figures 空时仍认 CNIPA_FILE_ROOT/{app}/{app}.png
        os.remove(figure_path(app))
        fr = os.path.join(tmp, 'file_root')
        os.environ[cnipa_contract.FILE_ROOT_ENV] = fr
        disk_fig = os.path.join(fr, app, app + '.png')
        os.makedirs(os.path.dirname(disk_fig), exist_ok=True)
        shutil.copy2(src_png, disk_fig)
        hd = load_figure(app)
        check(hd is not None and hd['kind'] == 'png' and
              os.path.abspath(hd['path']) == os.path.abspath(disk_fig),
              'file_root 附图应命中: %r' % hd)
        os.remove(disk_fig)
        os.environ[cnipa_contract.FILE_ROOT_ENV] = os.path.join(tmp, 'file_root_empty')
        write_figure(app, src_png)  # 恢复后续负缓存用例
        dst = os.path.join(tmp, 'out', 'fig.png')
        h2 = load_figure(app, save_path=dst)
        check(h2 is not None and h2['path'] == dst and os.path.isfile(dst),
              'save_path 拷贝失败: %r' % h2)
        with open(figure_path(app), 'wb') as f:
            f.write(b'')                      # size=0 → 视为损坏
        check(load_figure(app) is None, 'size=0 的 png 应 miss')
        # 负缓存：无正缓存时 .no_figure 生效；clear 后恢复
        try:
            os.remove(figure_path(app))
        except OSError:
            pass
        write_no_figure(app)
        check(load_figure(app) == {'kind': 'no_figure'}, '负缓存标记未生效')
        # 5c) 负缓存 + 交付盘有图 → 正缓存优先（勿误报 NO_FIGURE）
        fr2 = os.path.join(tmp, 'file_root2')
        os.environ[cnipa_contract.FILE_ROOT_ENV] = fr2
        disk2 = os.path.join(fr2, app, app + '.png')
        os.makedirs(os.path.dirname(disk2), exist_ok=True)
        shutil.copy2(src_png, disk2)
        hnf = load_figure(app)
        check(hnf is not None and hnf['kind'] == 'png',
              'no_figure 存在时交付盘有图仍应命中: %r' % hnf)
        os.remove(disk2)
        os.environ[cnipa_contract.FILE_ROOT_ENV] = os.path.join(tmp, 'file_root_empty')
        clear_no_figure(app)
        write_figure(app, src_png)
        check(load_figure(app) is not None and load_figure(app)['kind'] == 'png',
              'clear_no_figure 后应恢复 png 命中')

        # 6) 禁用开关：读写全跳过
        os.environ[DISABLED_ENV] = '1'
        check(load_search('invalid', {'application_no': '200610004173.6'}, 50) is None,
              '禁用时应不读')
        check(write_no_figure('999999999999.9') is None, '禁用时应不写')
        os.environ.pop(DISABLED_ENV, None)

        # 7) 根路径环境变量生效（search 落盘在 tmp 下）
        check(search_path('invalid', {}, 50).startswith(tmp), 'CNIPA_CACHE_ROOT 未生效')

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print('SELFTEST FAIL (%d)' % len(fails))
        for x in fails:
            print(' -', x)
        return 1
    print('SELFTEST OK')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
