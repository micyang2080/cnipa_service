# -*- coding: utf-8 -*-
"""05 UIA 版：pss 摘要附图检索与下载（免 CDP，真实键鼠驱动用户日常 Chrome）。

背景（2026-09-01 21:2x，04 移植完成后仅剩本件）：
    - PW 版 run_cnipa_fetch_abstract_figure_pw.py 依赖 CDP（img src 提取 +
      page.request.get 下载原文件）——瑞数拒绝一切 CDP 形态后整条路出局；
    - UIA 下无 DOM/网络接口 → 取图改三段式（2026-09-02 08:54 用户定案）：
      C 主路径：右键附图 →「图片另存为…」→ 原生另存为对话框键入目标路径
        → 原图文件直接落盘（质量/分辨率最优；菜单词取自用户实拍截图）；
      B 提质：图片右键「在新标签页中打开图片」→ 浏览器原生渲染整图 →
        整窗截图 + 白边自动裁剪；
      A 保底：列表卡片左列像素墨块矩形 + 窗口截图裁剪（一定能出图，屏幕
      分辨率；附图是 canvas 渲染、不进 a11y 树——2026-09-02 07:47 用户
      截图勘误实证：附图就在结果列表页、「摘要」页签左侧，无需进详情页）。
      C/B 失败（菜单词不匹配/对话框形态变化）不阻断，逐级回落。

pss 现场未知点（首跑即探针，output/uia_runs/figure_*/ 全程留证）：
    - pss-system.cponline.cnipa.gov.cn 是否独立瑞数壳/登录跳转；
    - 免责声明弹层【已实证 2026-09-01 21:50】：登录后必弹、盖住检索表单；
      CheckBox'记住我的选择'本体 0 矩形（点标签左侧框），Button'同意'可点；
      弹层正文含「结果」字样，page_state 必须先判弹层再判结果区；
    - 申请号 Tab/输入框形态（PW 版按 input[name=sqh] 定位）；
    - 结果区缩略图的 a11y 暴露形态（cpquery 实测 <img> = control_type 'Image'）。

用法（用户本机执行；Chrome 已 --force-renderer-accessibility 启动）：
    python -X utf8 run_cnipa_fetch_abstract_figure_uia.py <input.json>
    python -X utf8 run_cnipa_fetch_abstract_figure_uia.py --selftest
input.json：application_no（必填，CN 前缀/带点均可自动规范化）、
    save_path（可选，附图落盘绝对路径）、keep_tab（默认 false 跑完关 pss 标签）。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cnipa_contract  # noqa: E402
import cnipa_cache  # noqa: E402
import cnipa_creds  # noqa: E402
import cnipa_uia as uia  # noqa: E402
from run_cnipa_search_decisions_uia import flat_tree_dump  # noqa: E402
from run_cnipa_login_uia import (  # noqa: E402
    SSO_UNSAFE_HINT,
    classify_login_fields,
    fill_login_credentials,
    login_error_in_texts,
    looks_logged_in_texts,
    portal_landed_texts,
    resolve_login_fields,
    sso_unsafe_visible,
    _click_login_button,
    _handle_captcha,
    _login_edits,
)

PSS_URL = 'https://pss-system.cponline.cnipa.gov.cn/conventionalSearch'


def _pss_sso_url() -> str:
    from urllib.parse import quote
    return ('https://sso.cponline.cnipa.gov.cn/login?service='
            + quote(PSS_URL, safe=''))


def _win_title(win) -> str:
    try:
        return win.window_text() or ''
    except Exception:
        return ''


def _abort_if_unsafe(win, step, tag: str) -> bool:
    """命中 /unsafe 返回 True（调用方应 fail）。"""
    tit = _win_title(win)
    texts = uia.all_texts(win)
    if sso_unsafe_visible(texts, tit):
        step('sso_unsafe', where=tag, title=tit[:48])
        return True
    return False


def _portal_return_to_pss(win, step) -> bool:
    """误进 tysf 门户（已登录却点了顶栏「登录」）→ 切回已有常规检索，勿堆新 tab。"""
    if _activate_pss_tab(win, step):
        joined = ' '.join(uia.all_texts(win))
        if is_pss_ui(joined):
            step('pss_login', phase='portal_back_existing_pss_tab')
            return True
    for ct in ('Hyperlink', 'Text', 'Button', 'MenuItem'):
        for t in uia.descendants(win, control_type=ct):
            n = (uia._name(t) or '').strip()
            if n not in ('返回系统', '返回'):
                continue
            r = uia._rect(t)
            if not r:
                continue
            uia.human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2,
                            settle=0.35)
            step('pss_login', phase='portal_return_system', at=list(r))
            time.sleep(2.0)
            if _activate_pss_tab(win, step):
                return True
            break
    _goto_pss_conventional(win, step)
    step('pss_login', phase='portal_goto_conventional_same_tab')
    return True


def _click_pss_header_login(win, step) -> bool:
    """PSS 顶栏「登录」→ 统一身份（比 CLI 直开 SSO 不易触发 /unsafe）。"""
    if is_pss_logged_in(uia.all_texts(win)):
        step('pss_login', phase='skip_header_login_already_in')
        return False
    for ct in ('Hyperlink', 'Text', 'Button', 'MenuItem'):
        for t in uia.descendants(win, control_type=ct):
            n = (uia._name(t) or '').strip()
            if n != '登录':
                continue
            r = uia._rect(t)
            if not r or not (40 <= r[1] <= 220):
                continue
            uia.human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2, settle=0.35)
            step('pss_login', phase='click_header_login', at=list(r))
            time.sleep(2.0)
            return True
    return False


def _open_sso_force(win, step) -> None:
    """末位兜底：CLI 打开 PSS CAS SSO（易触发安全拦截，优先点顶栏登录）。"""
    url = _pss_sso_url()
    ok, how = uia.open_url_via_chrome(url)
    if ok:
        time.sleep(2.8)
        try:
            win.set_focus()
        except Exception:
            pass
        step('open_sso', how=how)
        if _abort_if_unsafe(win, step, 'after_open_sso_cli'):
            return
        return
    step('open_sso', warn=f'chrome_cli失败:{how}，回落键鼠')
    win.set_focus()
    time.sleep(0.3)
    uia.safe_send_keys('^t', win=win, pause=0.05, log=step)
    time.sleep(0.8)
    uia.safe_send_keys(url, win=win, pause=0.03, with_spaces=True, log=step)
    time.sleep(0.3)
    uia.safe_send_keys('{ENTER}', win=win, pause=0.05, log=step)
    time.sleep(1.5)
    step('open_sso', how='keyboard')


def _login_page_visible(texts) -> bool:
    joined = ' '.join(texts or [])
    return any(m in joined for m in (
        '统一身份', '自然人登录', '法人登录', '代理机构登录',
        '手机号/证件号码', '请输入密码', '请登录'))


def _wait_login_fields(win, step, opened_sso: bool) -> dict:
    """等账号+密码框。已在登录页：最长 12s；刚开 SSO：最长 40s。超时即停，不空转。"""
    deadline = time.time() + (40.0 if opened_sso else 12.0)
    last_n = -1
    while time.time() < deadline:
        fields, _kind = resolve_login_fields(win)
        if fields:
            return fields
        texts = uia.all_texts(win)
        err = login_error_in_texts(texts)
        if err:
            step('pss_login', phase='form_wait_err', err=err)
            return {'_err': f'登录页错误: {err}'}
        n = len(_login_edits(win))
        if n != last_n:
            step('pss_login', phase='form_wait', edits=n,
                 remain=round(deadline - time.time(), 1))
            last_n = n
        # 登录页文案已齐但长时间 0 输入框 → 提前放弃（a11y 未暴露）
        if (not opened_sso and _login_page_visible(texts) and n == 0
                and (deadline - time.time()) < 6.0):
            return {}
        time.sleep(1.5)
    return {}


def _ensure_pss_login(win, out_dir: str, step) -> tuple:
    """pss 掉登录态：就地填一次或强制开 SSO 填一次；失败即返回，不反复重登。"""
    creds = cnipa_creds.load_creds()
    if not creds:
        return False, '未配置 account.ini，无法自动登录 pss'
    texts0 = uia.all_texts(win)
    if is_pss_logged_in(texts0) and is_pss_ui(' '.join(texts0)):
        step('pss_login', phase='already_logged_in')
        return True, '搜索表单'
    if portal_landed_texts(texts0):
        step('pss_login', phase='portal_rescue_before_login')
        _portal_return_to_pss(win, step)
        if is_pss_logged_in(uia.all_texts(win)):
            return True, '搜索表单'
    if _abort_if_unsafe(win, step, 'pss_login_start'):
        return False, SSO_UNSAFE_HINT
    fields, _ = resolve_login_fields(win)
    opened_sso = False
    if fields:
        step('pss_login', phase='form_inplace')
    else:
        if is_pss_logged_in(uia.all_texts(win)):
            step('pss_login', phase='logged_in_no_form')
            return True, '搜索表单'
        if portal_landed_texts(uia.all_texts(win)):
            step('pss_login', phase='portal_no_form')
            _portal_return_to_pss(win, step)
            if is_pss_logged_in(uia.all_texts(win)) or is_pss_ui(
                    ' '.join(uia.all_texts(win))):
                return True, '搜索表单'
        # 不点 PSS 顶栏「登录」（已有 SSO 会话时会进门户）；直开 CAS 或等检索跳转
        step('pss_login', phase='open_sso_cas')
        if not fields:
            step('pss_login', phase='open_sso_last_resort')
            _open_sso_force(win, step)
            if _abort_if_unsafe(win, step, 'after_open_sso'):
                return False, SSO_UNSAFE_HINT
            opened_sso = True
            fields = _wait_login_fields(win, step, opened_sso=True)
            if fields.get('_err'):
                return False, fields['_err']
            if not fields:
                _tit = _win_title(win)
                if not (_login_page_visible(uia.all_texts(win))
                        or any(m in _tit for m in ('统一身份', '身份认证'))):
                    return False, 'pss SSO 登录表单未出现（已超时放弃）'
                step('pss_login', phase='form_try_label')
    step('pss_login', phase='form_ok', via=('sso' if opened_sso else 'inplace'))
    ok_fill, fill_via = fill_login_credentials(win, creds, step)
    if not ok_fill:
        return False, f'登录表单无法键入账密（{fill_via}）'
    time.sleep(0.5)
    if not _click_login_button(win, step):
        return False, '未找到登录按钮'
    if not _handle_captcha(win, out_dir, step, max_attempts=6):
        return False, '滑块验证未通过'
    t0 = time.time()
    still_login_since = None
    while time.time() - t0 < 30.0:
        time.sleep(1.5)
        texts = uia.all_texts(win)
        if sso_unsafe_visible(texts, _win_title(win)):
            return False, SSO_UNSAFE_HINT
        if looks_logged_in_texts(texts) or is_pss_logged_in(texts):
            break
        err = login_error_in_texts(texts)
        if err:
            return False, f'登录被拒: {err}'
        st = page_state(texts)
        if st in ('搜索表单', '疑似结果区', '免责声明'):
            break
        if portal_landed_texts(texts):
            step('pss_login', phase='post_submit_portal')
            _portal_return_to_pss(win, step)
            break
        # 提交后仍停在登录墙：超过 12s 即判失败（避免空等 45s）
        if st in ('登录墙', LOGGED_OUT) or _login_page_visible(texts):
            if still_login_since is None:
                still_login_since = time.time()
            elif time.time() - still_login_since >= 12.0:
                return False, f'登录提交后仍停在登录页（state={st}）'
        else:
            still_login_since = None
    else:
        return False, '登录后 30s 未检测到登录态'
    step('pss_login', phase='ok_back_pss')
    if portal_landed_texts(uia.all_texts(win)):
        _portal_return_to_pss(win, step)
    elif not _activate_pss_tab(win, step):
        _goto_pss_conventional(win, step)
    st = _wait_pss_ready(win, step=step, timeout_s=25.0)
    step('pss_login', phase='back', state=st)
    if st in ('搜索表单', '疑似结果区'):
        return True, st
    if st in ('登录墙', LOGGED_OUT):
        return False, f'登录后仍未进检索页（state={st}）'
    return False, f'登录后页态异常（state={st}）'


# ―― 常量（首跑后按探针修订） ――
HEADER_Y = 220          # 窗口顶部带（标签条+地址栏+站点头）内图元视为 logo 排除
MIN_IMG_W, MIN_IMG_H = 60, 60    # 缩略图下限；更小的是 icon/箭头
BANNER_RATIO = 4.0      # 宽/高 > 4 的扁长条是横幅不是附图
RESULT_COUNT_RE = re.compile(r'\d+\s*条')   # 结果区强标记：命中数「…N条 / N 条」
LOGIN_MARKS = ('请登录', '账号', '密码')  # 须成对/强标记；单字「登录」会出现在已登录页
LOGGED_OUT = '未登录(会话过期?)'             # 右上角「登录|注册」齐现 = 掉登录态
PSS_LOGGED_IN_HINTS = ('退出', '个人中心', '我的收藏', '用户中心')  # PSS 已登录（无「欢迎您」）
# 免责声明弹层专属文案（2026-09-01 21:50 实跑树实证：页脚常年有「免责声明」
# 链接，不能只看它；「记住我的选择」/「使用前必读」只在弹层出现）
DISCLAIMER_MARKS = ('记住我的选择', '使用前必读')
PSS_TAB_MARKS = ('常规检索', '检索结果', '检索清单', '专利检索及分析')
PSS_RESULT_TAB_MARKS = ('检索结果', '检索清单')
PSS_SEARCH_TAB_MARKS = ('常规检索', '专利检索及分析')
OPEN_IMG_WORDS = ('在新标签页中打开图片', '打开图片', '打开图像', '新标签页中打开图',
                  'open image', '查看图像')
NOT_IMG_WORDS = ('链接', 'link', '复制', '下载', 'search')  # 命中即排除
# C 主路径：「图片另存为」（2026-09-02 08:54 用户提议——右键 → 另存为 →
# 原图文件直接落盘，质量/分辨率远胜截图裁剪。菜单词取自用户实拍截图：
# 图片另存为… / 复制图片 / 在新标签页中打开图片 / 为此图片创建二维码…）
SAVE_BAD_WORDS = ('链接', '页面', '目标', 'link', 'page', 'target')


def patent_appno_digits(application_no: str) -> str:
    return re.sub(r'\D', '', (application_no or ''))


def looks_like_patent_application_no(application_no: str) -> bool:
    d = patent_appno_digits(application_no)
    return 8 <= len(d) <= 15


def normalize_search_key(application_no: str) -> str:
    """pss 首选：13 位含校验点（站点帮助「通过专利申请号（13位，带.）检索」）。"""
    dotted = cnipa_contract.dotted_appno(application_no)
    if dotted and '.' in dotted:
        return dotted
    raw = re.sub(r'[\s\-]', '', (application_no or '')).upper()
    raw = re.sub(r'^(CN|ZL)', '', raw)
    if re.match(r'^\d{12,14}$', raw):
        return f'CN{raw}'
    return ('CN' + raw) if raw else ''


def search_key_variants(application_no: str) -> list:
    """申请号检索候选：带点 13 位优先，再裸流水/CN 兜底。"""
    raw = re.sub(r'[\s\-]', '', (application_no or '')).upper()
    raw = re.sub(r'^(CN|ZL)', '', raw)
    out: list = []

    def add(k: str) -> None:
        k = (k or '').strip()
        if k and k not in out:
            out.append(k)

    m = re.match(r'^(\d{12,13})\.?(\d)?$', raw)
    if m and (m.group(2) is not None or len(m.group(1)) in (12, 13)):
        # 12+校验无点写成 13 位：2006100041736 → body12+chk
        if m.group(2) is None and len(m.group(1)) == 13:
            body, chk = m.group(1)[:12], m.group(1)[12]
        elif m.group(2) is None and len(m.group(1)) == 12:
            body, chk = m.group(1), None
        else:
            body, chk = m.group(1), m.group(2)
        if chk:
            add(f'CN{body}.{chk}')
            add(f'{body}.{chk}')
        add(f'CN{body}')
        add(body)
    else:
        add(normalize_search_key(application_no))
    return out


PSS_SEARCH_MAX_ATTEMPTS = 3   # 未弹出结果页时同号重试（网站偶发），不换申请号


def preferred_search_key(application_no: str, fallback: str = '') -> str:
    """PSS 申请号检索用键：变体列表首项（CN+带点优先），否则 fallback。"""
    v = search_key_variants(application_no)
    if v:
        return v[0]
    return (fallback or normalize_search_key(application_no) or '').strip()


def _result_page_popped(win, before_tabs: list, st: str) -> bool:
    """检索后是否已进入「检索结果」页（含 0 命中）；仍在常规检索表单则 False。"""
    if st in ('疑似结果区', '无命中'):
        return True
    tit = _win_title(win)
    if any(m in tit for m in PSS_RESULT_TAB_MARKS):
        return True
    for n, _ in _new_tabs_since(win, before_tabs or []):
        if any(m in n for m in PSS_RESULT_TAB_MARKS):
            return True
    return False


def is_smart_search_edit(name: str, y0: int) -> bool:
    """pss 常规检索大输入框识别（纯函数，自测冻结；22:04 实证树）：
    Edit 占位文案含「请输入关键词」+「申请号」，y0>100 排除地址栏
    （'地址和搜索栏' Edit 在 y51）。"""
    return ('请输入关键词' in (name or '')) and ('申请号' in (name or '')) and y0 > 100


def _img_candidates(win) -> list:
    """a11y Image 元素 → [{'name','rect','w','h','rel_y'}]；0 矩形（屏外）剔除。"""
    wr = uia._rect(win)
    out = []
    for el in uia.descendants(win, control_type='Image'):
        r = uia._rect(el)
        if r is None or (r[2] - r[0]) <= 0 or (r[3] - r[1]) <= 0:
            continue
        out.append({'name': (uia._name(el) or '')[:40], 'rect': list(r),
                    'w': r[2] - r[0], 'h': r[3] - r[1],
                    'rel_y': (r[1] - wr[1]) if wr else 0})
    return out


def pick_figure(cands: list) -> dict:
    """图候选 → 摘要附图（纯函数，自测冻结）。

    过滤：真矩形、≥60×60、不在顶部带（rel_y>HEADER_Y 内为站点头/浏览器栏的
    logo banner）、非扁横幅（w/h≤4）；择优：面积最大，同面积取更靠下
    （结果区在页面中下部，logo/icon 普遍在上）。
    """
    ok = [c for c in cands
          if c.get('w', 0) >= MIN_IMG_W and c.get('h', 0) >= MIN_IMG_H
          and c.get('rel_y', 0) > HEADER_Y
          and c.get('w', 0) / max(1, c.get('h', 0)) <= BANNER_RATIO]
    if not ok:
        return {}
    return max(ok, key=lambda c: (c['w'] * c['h'], c['rel_y']))


def match_open_image_menu(name: str) -> bool:
    """右键菜单项名 → 是否「在新标签页中打开图片」（纯函数，自测冻结）。

    必须命中图片词且不命中链接/复制/下载类；大小写不敏感。
    """
    n = (name or '').strip().lower()
    if not n or any(w in n for w in NOT_IMG_WORDS):
        return False
    return any(w in n for w in OPEN_IMG_WORDS)


def match_save_image_menu(name: str) -> bool:
    """右键菜单项名 → 是否「图片另存为」（纯函数，自测冻结；C 主路径）。

    须含「另存为」（或英文 save image as）**且**含「图」；命中「链接/
    页面/目标」即排除（同菜单里有「链接另存为」「将页面另存为」，一词
    之差，存错了就是一张 HTML）。大小写不敏感。
    """
    n = (name or '').strip().lower()
    if not n or any(w in n for w in SAVE_BAD_WORDS):
        return False
    if 'save image as' in n:
        return True
    return ('另存为' in n) and ('图' in n)


def autocrop_figure(png_path: str, out_path: str, tol: int = 12,
                    min_side: int = 40) -> str:
    """整窗截图里的"独立渲染图"自动裁剪：以四边中点采底色（灰/白背景），
    裁出与底色差异 >tol 的内容包围盒；内容过小则原样返回（防裁成空）。

    B 路径（新标签页原生渲染的图）专用：图居中、四周是浏览器纯色背景。
    """
    from PIL import Image
    im = Image.open(png_path).convert('RGB')
    w, h = im.size
    px = im.load()
    samples = [px[w // 2, 3], px[w // 2, h - 4], px[3, h // 2], px[w - 4, h // 2],
               px[w // 4, 3], px[3 * w // 4, 3]]
    bg = [sum(c[i] for c in samples) // len(samples) for i in range(3)]

    def diff(p) -> int:
        return max(abs(p[i] - bg[i]) for i in range(3))

    x0, y0, x1, y1 = w, h, 0, 0
    step = 2                                  # 步进采样，速度优先
    for y in range(0, h, step):
        for x in range(0, w, step):
            if diff(px[x, y]) > tol:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 - x0 < min_side or y1 - y0 < min_side:
        im.save(out_path)
        return out_path
    im.crop((max(0, x0 - 2), max(0, y0 - 2),
             min(w, x1 + 3), min(h, y1 + 3))).save(out_path)
    return out_path


def disclaimer_open_by_texts(texts: list) -> bool:
    """免责声明弹层是否开着（纯函数，自测冻结）。

    弹层专属文案命中即开；页脚常驻的「免责声明」链接不算（不在
    DISCLAIMER_MARKS 里），避免搜索表单页被误杀。
    """
    joined = ' '.join(texts)
    return any(m in joined for m in DISCLAIMER_MARKS)


def is_cpquery_ui(joined: str) -> bool:
    """cpquery 案件/复审无效页（也有申请号输入框，绝不能当 PSS 附图页）。"""
    return any(m in (joined or '') for m in (
        '中国及多国专利审查信息查询', '中国专利审查信息查询',
        '案件查询', '复审无效审查决定', '申请方式查询',
        '共查询到',
    ))


def is_pss_ui(joined: str) -> bool:
    """pss 专利检索及分析（附图检索）专属标记。"""
    return any(m in (joined or '') for m in (
        '专利检索及分析', '请输入关键词', '数据范围',
        '智能识别检索', '常规检索',
    ))


def is_pss_logged_in(texts: list) -> bool:
    """PSS 已登录。

    访客检索页也会渲染「申请号」等 Tab；须先看右上角「登录|注册」齐现 → 未登录。
    """
    joined = ' '.join(texts or [])
    if looks_logged_in_texts(texts):
        return True
    if any(h in joined for h in PSS_LOGGED_IN_HINTS):
        return True
    # 仅 PSS 检索站：顶栏「登录|注册」= 访客，虽有申请号 Tab 也不能检索
    if is_pss_ui(joined) and '登录' in joined and '注册' in joined:
        return False
    if is_pss_ui(joined) and any(m in joined for m in (
            '自动识别', '检索要素', '申请号', '公开号', '发明名称',
            '数据范围', '智能识别', '检索出的结果')):
        return True
    return is_pss_ui(joined)


def page_state(texts: list) -> str:
    """pss 页态分类（探针遥测用）：免责声明/登录墙/结果区/搜索表单/挑战壳。

    免责声明必须最先判：2026-09-01 21:50 实跑教训，弹层正文含
    「检索出的结果不承担责任」——'结果'+数字 命中旧的结果区规则，
    被误判「疑似结果区」提前放行，弹层没关、申请号表单根本没渲染。

    2026-09-02 06:32 实跑教训（假成功）：旧 RESULT_MARKS=('共','条',
    '结果','发明','申请') 太宽松——空表单页自带「申请号」「发明名称」
    Tab + 帮助文案里的日期数字，0.3s 即误判「疑似结果区」；隔夜掉登录
    态后，脚本把页脚站标当附图存了盘。修法：①结果区只认强证据
    「N条」命中数/「命中」；②「登录|注册」右上角齐现 → 未登录，
    表单页先于结果放行前 fail-fast。

    2026-09-08：cpquery「案件查询」也有申请号+查询，旧规则当成搜索表单
    → 在错误页填号。必须先排除 cpquery，且搜索表单只认 PSS 专属文案。
    同日：已登录仍被「登录」单字打成登录墙 → 每次 SSO + 新开常规检索。
    """
    joined = ' '.join(texts)
    if not joined.strip():
        return '空壳(挑战/加载中?)'
    if joined.strip() == '地址和搜索栏':
        return '仅浏览器框(a11y未开?)'
    if disclaimer_open_by_texts(texts):
        return '免责声明'
    if is_cpquery_ui(joined) and not is_pss_ui(joined):
        return '非PSS(cpquery)'
    if any(m in joined for m in ('暂无检索数据', '未查询到', '没有符合条件')):
        return '无命中'
    if re.search(r'(?:共\s*)?0\s*条', joined) and is_pss_ui(joined):
        return '无命中'
    if RESULT_COUNT_RE.search(joined) or '命中' in joined:
        return '疑似结果区'
    if (not is_pss_logged_in(texts)
            and is_pss_ui(joined)
            and '登录' in joined and '注册' in joined):
        return LOGGED_OUT
    # tysf 统一身份认证（PSS 掉态常跳这里；须在 PSS 访客 LOGGED_OUT 之后、搜索表单之前）
    if (not is_pss_logged_in(texts)
            and any(m in joined for m in (
                '统一身份', '自然人登录', '法人登录', '代理机构登录',
                '手机号/证件号码', 'tysf.cponline'))):
        return '登录墙'
    if is_pss_ui(joined):
        return '搜索表单'
    if '登录' in joined and '注册' in joined:
        return LOGGED_OUT
    # 真登录墙：账号+密码同现，或「请登录」；已登录 PSS 不进此支
    if (not is_pss_logged_in(texts)
            and (('请登录' in joined)
                 or ('账号' in joined and '密码' in joined)
                 or ('用户名' in joined and '密码' in joined)
                 or ('手机号' in joined and '密码' in joined)
                 or ('证件号码' in joined and '密码' in joined))):
        return '登录墙'
    return '未知'


def timeout_note(state: str) -> str:
    """就绪超时的话术（纯函数，自测冻结）。"""
    return {'空壳(挑战/加载中?)': '40s 内未出内容',
            '仅浏览器框(a11y未开?)': '页面内容通道未就绪',
            '免责声明': '免责声明未能关闭',
            '未知': '40s 内页态不明'
            }.get(state, f'页态[{state}]不可检索')


def _title_looks_initpage(win) -> bool:
    try:
        t = (win.window_text() or '').strip()
    except Exception:
        t = ''
    tl = t.lower()
    if 'initpage' in tl:
        return True
    # 空白页标题常是裸 URL：https://pss-system.cponline.cn
    if 'pss-system' in tl and 'http' in tl and 'conventional' not in tl:
        return True
    return False


def _goto_pss_conventional(win, step) -> None:
    """地址栏强制打开常规检索（离开空白 Initpage）。"""
    try:
        win.set_focus()
        uia.safe_send_keys('^l', win=win, pause=0.05, log=step)
        time.sleep(0.25)
        uia.safe_send_keys('^a', win=win, pause=0.02, log=step)
        uia.safe_send_keys(PSS_URL, win=win, pause=0.02, with_spaces=True,
                           log=step)
        time.sleep(0.2)
        uia.safe_send_keys('{ENTER}', win=win, pause=0.05, log=step)
        time.sleep(3.0)
    except Exception as e:
        step('pss_blank', warn=str(e)[:80])
        _open_url_tab(win, PSS_URL, step)


def _recover_pss_from_blank(win, out_dir: str, step, state: str) -> str:
    """空白 Initpage / 空壳 → 进 conventionalSearch 再等就绪。"""
    step('pss_blank', state=state, action='goto_conventionalSearch',
         initpage=_title_looks_initpage(win))
    _goto_pss_conventional(win, step)
    return _wait_pss_ready(win, step=step, timeout_s=25.0)


# ---------------------------------------------------------------------------
# 现场动作（首跑探针修订区）
# ---------------------------------------------------------------------------

def _open_url_tab(win, url: str, step) -> None:
    """打开 pss：有已有「常规检索/检索结果」则只切换，禁止 chrome CLI 再开新标签。

    2026-09-08：每次 CLI 开 conventionalSearch → 红框里堆出多个相同 tab。"""
    if _activate_pss_tab(win, step):
        step('open_url', url=url, how='reuse_tab')
        return
    ok, how = uia.open_url_via_chrome(url)
    if ok:
        time.sleep(2.8)
        try:
            win.set_focus()
        except Exception:
            pass
        step('open_url', url=url, how=how)
        _activate_pss_tab(win, step)
        return
    step('open_url', warn=f'chrome_cli失败:{how}，回落键鼠')
    win.set_focus()
    time.sleep(0.3)
    uia.safe_send_keys('^t', win=win, pause=0.05, log=step)
    time.sleep(0.8)
    uia.safe_send_keys(url, win=win, pause=0.03, with_spaces=True, log=step)
    time.sleep(0.3)
    uia.safe_send_keys('{ENTER}', win=win, pause=0.05, log=step)
    time.sleep(1.5)
    step('open_url', url=url, how='keyboard')


def _iter_pss_tabs(win, marks) -> list:
    cands = []
    for t in uia.descendants(win, control_type='TabItem'):
        n = (uia._name(t) or '').strip()
        r = uia._rect(t)
        if not n or not r or r[1] >= 80:
            continue
        for pri, mark in enumerate(marks):
            if mark in n:
                cands.append((pri, n, t))
                break
    cands.sort(key=lambda x: x[0])
    return cands


def _activate_pss_tab(win, step) -> bool:
    """复用已有「常规检索」；点后核验前台已是 PSS。空白 Initpage 不算成功。"""
    title = ''
    try:
        title = win.window_text() or ''
    except Exception:
        pass
    if _title_looks_initpage(win):
        step('reuse_pss_tab', warn='front_is_initpage', title=title[:40])
        return False
    if any(m in title for m in PSS_SEARCH_TAB_MARKS):
        texts = ' '.join(uia.all_texts(win))
        if is_pss_ui(texts) or '常规检索' in title:
            step('reuse_pss_tab', how='already_front', title=title[:40])
            return True
    cands = _iter_pss_tabs(win, PSS_SEARCH_TAB_MARKS)
    if not cands:
        return False
    _pri, n, t = cands[0]
    uia.safe_click(win, t, settle=1.2, log=step)
    time.sleep(0.8)
    title2 = ''
    try:
        title2 = win.window_text() or ''
    except Exception:
        pass
    if _title_looks_initpage(win):
        step('reuse_pss_tab', warn='landed_initpage', tab=n[:40])
        return False
    texts2 = ' '.join(uia.all_texts(win))
    if any(m in title2 for m in PSS_SEARCH_TAB_MARKS) or is_pss_ui(texts2):
        step('reuse_pss_tab', how='switch', tab=n[:40], title=title2[:40])
        return True
    step('reuse_pss_tab', warn='clicked_but_still_not_pss', tab=n[:40],
         title=title2[:40])
    return False


def _activate_results_tab(win, step) -> bool:
    """检索后切到「检索结果」——附图在结果页，停在「常规检索」会空滚右侧条。"""
    title = ''
    try:
        title = win.window_text() or ''
    except Exception:
        pass
    if any(m in title for m in PSS_RESULT_TAB_MARKS):
        step('reuse_result_tab', how='already_front', title=title[:40])
        return True
    cands = _iter_pss_tabs(win, PSS_RESULT_TAB_MARKS)
    if not cands:
        return False
    # 多「检索结果」时取最右侧（通常本次新开；229 常残留旧结果 tab）
    _pri, n, t = max(
        cands,
        key=lambda c: (uia._rect(c[2]) or (0, 0, 0, 0))[0])
    uia.safe_click(win, t, settle=1.2, log=step)
    time.sleep(1.0)
    title2 = ''
    try:
        title2 = win.window_text() or ''
    except Exception:
        pass
    ok = any(m in title2 for m in PSS_RESULT_TAB_MARKS)
    step('reuse_result_tab', how='switch' if ok else 'clicked', tab=n[:40],
         title=title2[:40], pick='rightmost')
    return ok


def _disclaimer_present(win) -> bool:
    """现查弹层是否还开着（动作阶段用；判态用纯函数 disclaimer_open_by_texts）。"""
    return disclaimer_open_by_texts(uia.all_texts(win))


def _remember_choice_checked(win) -> Optional[bool]:
    """「记住我的选择」是否已勾。CheckBox 可能 0 矩形，但仍可读 ToggleState。"""
    for cb in uia.descendants(win, control_type='CheckBox'):
        n = uia._name(cb) or ''
        if '记住' not in n and '选择' not in n:
            continue
        try:
            return int(cb.get_toggle_state()) == 1
        except Exception:
            pass
        try:
            lp = cb.legacy_properties() or {}
            v = str(lp.get('Value') or lp.get('value') or '').lower()
            if v in ('on', '1', 'checked', 'true'):
                return True
            if v in ('off', '0', 'unchecked', 'false'):
                return False
        except Exception:
            pass
    return None


def _handle_disclaimer(win, step=None, attempts: int = 3) -> bool:
    """免责声明弹层：确保「记住我的选择」为勾选 → 点「同意」→ 验证弹层消失。

    2026-09-01 21:50 实跑树（figure_20260901_215056/01b_state_tree.txt）：
        CheckBox '记住我的选择' rect=(0,0,0,0)   ← 无坐标，点不到自身
        Text     '记住我的选择' rect=(163,815,247,834) ← 勾选框在标签左侧~10px
        Button   '同意'         rect=(139,855,205,888)
    首跑教训：弹层进树慢（首轮轮询时还没渲染），只试一次会空手而归；
    必须「点了同意且弹层文案消失」才算过关，否则申请号表单被弹层盖住。
    229 教训：默认已勾选时再点会取消勾选——只在明确未勾时点。
    """
    for att in range(1, attempts + 1):
        if not _disclaimer_present(win):
            return True                            # 本来就没弹层（幂等）
        # 1) 仅未勾时点标签左侧；已勾/读不到状态 → 不动，防误取消
        checked = _remember_choice_checked(win)
        if checked is False:
            for t in uia.descendants(win, control_type='Text', name_contains='记住'):
                r = uia._rect(t)
                if r and (r[2] - r[0]) > 0 and (r[3] - r[1]) > 0:
                    uia.human_click(win, max(8, r[0] - 10), (r[1] + r[3]) // 2)
                    time.sleep(0.4)
                    if step:
                        step('disclaimer', check='记住我的选择', attempt=att,
                             was='off')
                    break
        elif step:
            step('disclaimer', check='记住我的选择', attempt=att,
                 was=('on' if checked else 'unknown_skip'))
        # 2) 点「同意」（Button 优先，MenuItem 兜底；带空格变体也收）
        clicked = None
        for word in ('同意', '接 受', '接受', '确定'):
            for ct in ('Button', 'MenuItem'):
                for b in uia.descendants(win, control_type=ct, name_contains=word):
                    r = uia._rect(b)
                    if r and (r[2] - r[0]) > 0 and (r[3] - r[1]) > 0:
                        uia.human_click(win, (r[0] + r[2]) // 2,
                                        (r[1] + r[3]) // 2)
                        if step:
                            step('disclaimer', click=word, attempt=att)
                        clicked = word
                        break
                if clicked:
                    break
            if clicked:
                break
        if not clicked:
            if step:
                step('disclaimer', warn='未见同意按钮（弹层未进树？）', attempt=att)
            time.sleep(1.5)
            continue
        time.sleep(1.2)                            # 等 DOM 收起弹层再验证
        if not _disclaimer_present(win):
            return True
        if step:
            step('disclaimer', warn='点过同意但弹层仍在，重试', attempt=att)
    return not _disclaimer_present(win)


def _wait_pss_ready(win, timeout_s: float = 40.0, step=None) -> str:
    """等 pss 落定。错页（cpquery/登录）立即处置或早退，禁止对「未知」空等满超时。"""
    t0 = time.time()
    last = ''
    tried_pss_switch = False

    def _noop_step(*_a, **_k):
        pass

    _step = step or _noop_step

    while time.time() - t0 < timeout_s:
        title = ''
        try:
            title = win.window_text() or ''
        except Exception:
            pass
        texts = uia.all_texts(win)
        if disclaimer_open_by_texts(texts):
            _handle_disclaimer(win, step=step)
            texts = uia.all_texts(win)
        st = page_state(texts)
        # 标题兜底：tysf 登录钮常不进 a11y，但窗标题必含统一身份
        if any(m in title for m in ('统一身份', '身份认证')) and st not in (
                '搜索表单', '疑似结果区', '免责声明'):
            st = '登录墙'
        if st != last:
            _step('pss_ready', state=st, after=round(time.time() - t0, 1),
                  title=title[:36])
            last = st
        if st == '非PSS(cpquery)':
            if not tried_pss_switch:
                tried_pss_switch = True
                _step('pss_ready', note='wrong_tab_switch_pss')
                if _activate_pss_tab(win, _step):
                    last = ''
                    continue
            return st
        if (st in ('搜索表单', '疑似结果区', '登录墙', LOGGED_OUT)
                and not disclaimer_open_by_texts(texts)):
            return st
        # 未知/空壳：最多盯 10s，避免「错 tab 干等 40s」
        if st in ('未知', '空壳(挑战/加载中?)', '仅浏览器框(a11y未开?)') and (
                time.time() - t0) >= 10.0:
            _step('pss_ready', state=st, note='unknown_giveup',
                  after=round(time.time() - t0, 1))
            return st
        time.sleep(1.2)
    return last or '空壳(挑战/加载中?)'


def _find_smart_edit(win):
    """pss 常规检索：一个大智能框（占位文案含 请输入关键词+申请号），申请号
    只是它上方的 Text Tab —— 没有与标签同行的 Edit，label_to_edit 必失败
    （22:04 实跑 02_fill_fail_tree 实证）。y0>100 排除浏览器地址栏。"""
    for e in uia.descendants(win, control_type='Edit'):
        r = uia._rect(e)
        if r and is_smart_search_edit(uia._name(e), r[1]):
            return e
    return None


def _click_search(win, edit_rect, step) -> bool:
    """提交：同行「检索」Text/Button 优先（191906 实证 Enter 常不触发检索），Enter 兜底。"""
    row_y0, row_y1 = 350, 520
    if edit_rect:
        row_y0 = max(300, edit_rect[1] - 40)
        row_y1 = edit_rect[3] + 40
    # 1) 智能框右侧同行「检索」Text（树实证 @1398,424）
    for ct in ('Text', 'Button', 'MenuItem'):
        for t in uia.descendants(win, control_type=ct):
            n = (uia._name(t) or '').strip()
            if n != '检索':
                continue
            r = uia._rect(t)
            if not r:
                continue
            if r[1] < 200:          # 顶栏导航「检索」
                continue
            if edit_rect and r[0] < edit_rect[2] - 40:
                continue
            if not (row_y0 <= (r[1] + r[3]) // 2 <= row_y1):
                continue
            uia.human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
            step('search', by=f'{ct}[检索]', rect=list(r))
            return True
    # 只点「检索」，不点「查询」——后者是 cpquery 案件查询按钮
    for name in ('检索',):
        btn = uia.find_button(win, name)
        if btn is not None:
            r = uia._rect(btn)
            if r and r[1] >= 200:
                uia.human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                step('search', by=f'按钮[{name}]')
                return True
    # 放大镜图标旁（\ue778）再试点一次同行右侧
    if edit_rect:
        uia.human_click(win, edit_rect[2] + 50, (edit_rect[1] + edit_rect[3]) // 2)
        step('search', by='click_right_of_edit')
        time.sleep(0.3)
    uia.safe_send_keys('{ENTER}', win=win, pause=0.05, log=step)
    step('search', by='Enter')
    return True


def _fill_appno(win, key: str, step) -> bool:
    """申请号 Tab + PSS 智能框 + 检索。禁止 label_to_edit（会命中 cpquery 申请号框）。"""
    _handle_disclaimer(win, step=step, attempts=1)
    texts = ' '.join(uia.all_texts(win))
    if is_cpquery_ui(texts) and not is_pss_ui(texts):
        step('fill', warn='当前是cpquery页，拒绝填号')
        return False
    for ct in ('Text', 'MenuItem'):
        for t in uia.descendants(win, control_type=ct):
            n = (uia._name(t) or '').strip()
            if n != '申请号':
                continue
            r = uia._rect(t)
            if r and 300 <= r[1] <= 450:
                uia.human_click(win, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                time.sleep(0.5)
                step('tab', what='申请号', text=n[:16])
                break
    edit = _find_smart_edit(win)
    if edit is None:
        step('fill', warn='未见PSS智能框（请输入关键词…）——非附图检索页')
        return False
    rect = uia._rect(edit)
    val = uia.clear_and_type(win, edit, key)
    step('fill', key=key, readback=val, by='智能框')
    return _click_search(win, rect, step)


def _title_looks_sso(win) -> bool:
    """弹页/跳转尚未出表单文案时，标题已能判登录墙（避免空转几十秒）。"""
    tit = _win_title(win)
    return any(m in tit for m in ('统一身份', '身份认证', 'sso.cponline'))


SSO_TAB_MARKS = ('统一身份', '身份认证', '自然人登录')
# 门户/业务办理等常驻 tab：永不主动点（229 机上极多，靠人关不可用）
IGNORE_TAB_MARKS = (
    '业务办理', '账户管理', '证书管理', '偏好设置', 'tysf',
    '集成电路',
)


def _list_top_tab_names(win) -> list:
    """顶栏 Tab 名（左→右），供检索前后 diff。"""
    rows = []
    for t in uia.descendants(win, control_type='TabItem'):
        n = (uia._name(t) or '').strip()
        r = uia._rect(t)
        if not n or not r or r[1] >= 80:
            continue
        rows.append((r[0], n, t))
    rows.sort(key=lambda x: x[0])
    return [n for _x, n, _t in rows]


def _new_tabs_since(win, before_names: list) -> list:
    """相对检索前快照，只返回多出来的顶栏 Tab（名, ctrl）。

    同名多开（多个「检索结果」）时：跳过检索前已有的 N 个，只认新增。
    """
    from collections import Counter
    remain = Counter(before_names or [])
    rows = []
    for t in uia.descendants(win, control_type='TabItem'):
        n = (uia._name(t) or '').strip()
        r = uia._rect(t)
        if not n or not r or r[1] >= 80:
            continue
        rows.append((r[0], n, t))
    rows.sort(key=lambda x: x[0])
    out = []
    for _x, n, t in rows:
        if remain[n] > 0:
            remain[n] -= 1
            continue
        out.append((n, t))
    return out


def _tab_name_ignored(name: str) -> bool:
    n = name or ''
    return any(m in n for m in IGNORE_TAB_MARKS)


def _close_tabs_opened_since(win, before_names: list, step,
                             max_close: int = 10) -> int:
    """关掉相对入口快照新开的顶栏 tab（检索结果/SSO/误开门户/多余常规检索）。

    入口前已有的 tab（含 229 上大量业务办理）一律不动。
    """
    closed = 0
    for _ in range(max_close):
        new_tabs = _new_tabs_since(win, before_names or [])
        if not new_tabs:
            break
        # 从右往左关，少搅动左侧常驻 tab 几何
        n, t = new_tabs[-1]
        try:
            uia.safe_click(win, t, settle=0.45, log=step)
            time.sleep(0.25)
            uia.safe_send_keys('^w', win=win, pause=0.05, log=step)
            time.sleep(0.55)
            step('cleanup_tab', closed=n[:40], left=len(new_tabs) - 1)
            closed += 1
        except Exception as e:
            step('cleanup_tab', warn=f'{n[:24]}: {e}'[:80])
            break
    return closed


def _follow_post_search_tab(win, step, before_tabs=None) -> str:
    """只跟「本次检索后新开」的标签；绝不点残留 SSO/门户 tab。

    返回: 'result' | 'login' | ''
    """
    # 本页已跳到结果/登录：无需切 tab
    tit = _win_title(win)
    if any(m in tit for m in PSS_RESULT_TAB_MARKS):
        return 'result'
    if _title_looks_sso(win):
        return 'login'

    new_tabs = _new_tabs_since(win, before_tabs or [])
    # 优先新开的检索结果
    for n, t in new_tabs:
        if _tab_name_ignored(n):
            continue
        if any(m in n for m in PSS_RESULT_TAB_MARKS):
            uia.safe_click(win, t, settle=1.0, log=step)
            time.sleep(0.6)
            step('follow_search_tab', kind='result', tab=n[:40],
                 title=_win_title(win)[:40], only_new=True)
            return 'result'
    # 仅当「新开」的是登录/SSO 才跟（残留 SSO 一律不理）
    for n, t in new_tabs:
        if _tab_name_ignored(n):
            continue
        if any(m in n for m in SSO_TAB_MARKS):
            uia.safe_click(win, t, settle=1.0, log=step)
            time.sleep(0.6)
            step('follow_search_tab', kind='login', tab=n[:40],
                 title=_win_title(win)[:40], only_new=True)
            return 'login'
    # 无新 tab：再试已有「检索结果」（可能同页改标题）；禁止点旧 SSO
    if before_tabs is None and _activate_results_tab(win, step):
        return 'result'
    if new_tabs:
        step('follow_search_tab', kind='skip',
             new=[n[:24] for n, _ in new_tabs[:5]])
    return ''


def _wait_results(win, timeout_s: float = 25.0, step=None,
                  before_tabs=None) -> list:
    """轮询等结果区出现（文本标记或合格 Image 元素）；返回当时的图候选。

    检索中途跳登录墙时返回 []，并在 step 记 need_login，由调用方登录后重试。
    before_tabs：点检索前的顶栏 Tab 名快照；只跟新增 tab，忽略 229 残留。
    """
    t0 = time.time()
    last_follow = 0.0
    while time.time() - t0 < timeout_s:
        now = time.time()
        if now - last_follow >= 2.0:
            last_follow = now
            kind = _follow_post_search_tab(win, step, before_tabs=before_tabs)
            if kind == 'login' or _title_looks_sso(win):
                if step:
                    step('wait_results', state='登录墙', need_login=True,
                         after=round(now - t0, 1),
                         title=_win_title(win)[:40], via='new_tab')
                return []
        texts = uia.all_texts(win)
        cands = _img_candidates(win)
        st = page_state(texts)
        # 仅当前台已是登录墙才判；不因残留 SSO tab 存在而误判
        if (st in ('登录墙', LOGGED_OUT) or _login_page_visible(texts)
                or _title_looks_sso(win)):
            if step:
                step('wait_results', state=st, need_login=True,
                     after=round(time.time() - t0, 1),
                     title=_win_title(win)[:40])
            return []
        if pick_figure(cands) or st == '疑似结果区':
            if step:
                step('wait_results', state=st,
                     images=len(cands), after=round(time.time() - t0, 1))
            return cands
        if st == '无命中':
            if step:
                step('wait_results', state=st, images=0,
                     after=round(time.time() - t0, 1))
            return []
        time.sleep(1.2)
    if step:
        step('wait_results', warn='超时', state=page_state(uia.all_texts(win)))
    return _img_candidates(win)


FOOTER_MARKS = ('主办单位', '版权所有', '政府网站', '找错', '友情链接', '京ICP备')


def clamp_zone(zone, img_w: int, img_h: int):
    """区带夹到截图范围内（纯函数，自测冻结）。13:52 教训：zone 越出截图
    时 PIL crop 会用**黑边**补齐，黑边全被算成"墨"→ 22% 填充率的假墨块
    （随后被贴边拒绝，表现为"区带内无合格墨块"，极具迷惑性）。夹完任一
    边 <100px 视为无效返回 None。"""
    z = (max(0, int(zone[0])), max(0, int(zone[1])),
         min(int(img_w), int(zone[2])), min(int(img_h), int(zone[3])))
    return z if (z[2] - z[0] >= 100 and z[3] - z[1] >= 100) else None


def ink_box_in_zone(png_path: str, zone, min_ink: int = 800,
                    dark: int = 170, size_lo: int = 60, size_hi: int = 600,
                    aspect_max: float = 3.0, edge_touch: int = 6,
                    fill_min: float = 0.025):
    """区带内像素定位墨块外接框（纯函数，自测冻结）。zone=(x0,y0,x1,y1)
    为截图坐标；返回**整图坐标**外接框 (x0,y0,x1,y1) 或 None：
      - 墨点 < min_ink（空区带 / 缩略图还没懒加载出来——07:09 实测未出图
        时全区带只剩 649px 淡墨假块）→ None；
      - 外接框任一边 < size_lo（残墨/碎字）或 > size_hi（整栏黑）→ None；
      - 宽高比 > aspect_max 的细条（滚动条竖条）→ None；
      - 外接框贴住区带**右/下**边界（<edge_touch px）→ None：右侧/下方是
        著录文本列，贴边=文本墨混进来了（07:09 复盘：区带右缘压到正文列
        x480+，外接框右边=区带右缘，纯文本假阳性）；
      - 墨量/框面积 < fill_min → None：真附图是线条画，实测填充率 ~7%
        （3466/219×224）；07:09 假块 649/213×285≈1.1%（中部 95 行全白 +
        底部一条淡文本带）——稀碎墨不是图。
    由头（2026-09-02 07:09/07:47 勘误）：附图在结果列表卡片左列，canvas
    渲染**不进 a11y 树**，a11y Image 元素只剩页脚徽标——只能靠像素。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return None
    W, H = im.size
    zx0, zy0, zx1, zy1 = [int(round(v)) for v in zone]
    zx0, zy0, zx1, zy1 = max(0, zx0), max(0, zy0), min(W, zx1), min(H, zy1)
    if zx1 - zx0 < size_lo or zy1 - zy0 < size_lo:
        return None
    arr = np.asarray(im.crop((zx0, zy0, zx1, zy1)))
    mask = arr < dark
    ink = int(mask.sum())
    if ink < min_ink:
        return None
    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if not (size_lo <= w <= size_hi and size_lo <= h <= size_hi):
        return None
    if w / h > aspect_max or h / w > aspect_max:
        return None
    if (zx1 - (zx0 + x1 + 1)) < edge_touch or (zy1 - (zy0 + y1 + 1)) < edge_touch:
        return None
    if ink / (w * h) < fill_min:
        return None
    return (zx0 + x0, zy0 + y0, zx0 + x1 + 1, zy0 + y1 + 1)


def _longest_run(seq):
    """1-D 序列最长连续真值段 (长度, 起始下标)（纯函数）。灰度行列扫描用：
    文字会打断灰段、边框不会——连续段长度是「线」与「文字」的判别器。"""
    best = cur = 0
    bs = s = 0
    for i, v in enumerate(seq):
        if v:
            if cur == 0:
                s = i
            cur += 1
            if cur > best:
                best, bs = cur, s
        else:
            cur = 0
    return best, bs


def locate_pane_frame(png_path: str, zone, h_run: int = 100, v_run: int = 80,
                      lo: int = 140, hi: int = 244, frame_min: int = 90,
                      frame_max: int = 640, ink_lo: int = 25, ink_hi: int = 20000,
                      fill_max: float = 0.08, ink_dark: int = 235, margin: int = 6,
                      span_ratio: float = 0.7, prefer_y=None):
    """区带内定位**带边框的附图窗格**（纯函数，自测冻结）。
    14:13 复盘定案：本页摘要附图**不进 a11y 树**（树里仅页脚/工具栏
    icon），心电式稀疏竖图的墨量（101~376px、填充率<1%）也过不了
    ink_box_in_zone 的 min_ink=800/fill 2.5% 门槛——但它躺在站点自带的
    浅灰边框窗格里（14:12 实测 x206-412 y725-958：上/下/左缘灰≈204、
    右缘≈242）。站点骨架（边框）是**内容无关锚**：心电图/机械图/化学
    结构图长什么样都无所谓，不刻舟求剑。算法：
      1) 横线：行长连续灰∈[lo,hi] ≥h_run（文字断段、线不断）→ 相邻行
         合并成带；
      2) 纵线：列长连续 ≥v_run → 合并成带；
      3) 配对成框：上下横线高差∈[frame_min,frame_max]、水平重叠≥h_run、
         左缘±8px 内有**贯穿**纵线（纵跨框高 ≥span_ratio，且起于框顶/
         终于框底附近）——页面上散落的浅灰横带（分隔线）没有纵线撑腰，
         撑不成窗格，此贯穿校验是关键判别器；
      4) 内部校验：去 margin 后墨（灰<ink_dark）量∈[ink_lo,ink_hi] 且
         填充率≤fill_max——空容器出局（无图可点）、密文本出局（不是图）；
      5) 右缘收紧：墨块右缘+4px 起最近的纵线（框内右边界），无则用横线
         重叠右端。
    prefer_y（整图坐标）非空时：窗格纵向须罩住该 y（±30 容差，列表卡片
    里「摘要」锚同行）——工具栏/表格分隔带拼出的假框罩不住锚行，直接
    出局，不返回 rank-2 候选（宁缺毋滥，防点错）。
    返回 {'rect','ink','ink_cnt','fill'}（整图坐标）或 None。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(png_path).convert('L')
    except Exception:
        return None
    zx0, zy0, zx1, zy1 = [int(round(v)) for v in zone]
    arr = np.asarray(im.crop((zx0, zy0, zx1, zy1)))
    H, W = arr.shape
    if W < h_run or H < frame_min + 10:
        return None
    gray = ((arr >= lo) & (arr <= hi)).tolist()

    def _bands(runs, min_run):
        """相邻合格行/列合并成带（区间取交）。runs=[(run,start),...]。"""
        out, k, n = [], 0, len(runs)
        while k < n:
            run, s = runs[k]
            if run < min_run:
                k += 1
                continue
            e = s + run
            k2 = k + 1
            while k2 < n and runs[k2][0] >= min_run:
                s2, e2 = runs[k2][1], runs[k2][1] + runs[k2][0]
                ns, ne = max(s, s2), min(e, e2)
                if ns >= ne:
                    break
                s, e = ns, ne
                k2 += 1
            out.append((k, k2, s, e))
            k = k2
        return out

    h_bands = _bands([_longest_run(r) for r in gray], h_run)
    v_bands = _bands([_longest_run(c) for c in zip(*gray)], v_run)

    best = None
    for i, (ty, ty2, ts, te) in enumerate(h_bands):
        for by, by2, bs_, be_ in h_bands[i + 1:]:
            hgt = by - ty
            if hgt < frame_min:
                continue
            if hgt > frame_max:
                break                        # h_bands 按 y 升序，后面更高
            os_, oe = max(ts, bs_), min(te, be_)
            if oe - os_ < h_run:
                continue
            left = None                      # 左缘贯穿纵线（±8px）
            for vx0, vx1, vys, vye in v_bands:
                if not (os_ - 8 <= vx0 <= os_ + 8
                        or os_ - 8 <= vx1 - 1 <= os_ + 8):
                    continue
                if (vys <= ty + 10 and vye >= by - 10
                        and (vye - vys) >= span_ratio * hgt):
                    if left is None or vx0 < left:
                        left = vx0
            if left is None:
                continue
            x1 = oe
            iy0, iy1 = ty + margin, by - margin
            ix0, ix1 = left + margin, x1 - margin
            if ix1 - ix0 < 10 or iy1 - iy0 < 10:
                continue
            sub = arr[iy0:iy1, ix0:ix1]
            mask = sub < ink_dark
            ink_cnt = int(mask.sum())
            if not (ink_lo <= ink_cnt <= ink_hi):
                continue                     # 空容器 / 密文本
            if ink_cnt / mask.size > fill_max:
                continue
            ys, xs = np.nonzero(mask)
            ink = (zx0 + ix0 + int(xs.min()), zy0 + iy0 + int(ys.min()),
                   zx0 + ix0 + int(xs.max()) + 1, zy0 + iy0 + int(ys.max()) + 1)
            cands_r = [vx0 for vx0, _vx1, _vs, _ve in v_bands
                       if (ix0 + int(xs.max()) + 5) <= vx0 <= x1 + 8]
            right = min(cands_r) if cands_r else x1
            if right - left < 60:            # 太窄的框不是附图窗格
                continue
            fr = (int(zx0 + left), int(zy0 + ty), int(zx0 + right),
                  int(zy0 + by2))
            if prefer_y is not None and not (fr[1] - 30 <= prefer_y <= fr[3] + 30):
                # 2026-09-08 图文模式：附图在「摘要」页签**下方**左列，不是页签同行
                below = (fr[1] >= prefer_y - 40) and (fr[1] <= prefer_y + 520)
                if not below:
                    continue
            key = (fr[2] - fr[0]) * (fr[3] - fr[1])
            if best is None or key < best[0]:
                best = (key, {'rect': list(fr), 'ink': list(ink),
                              'ink_cnt': ink_cnt,
                              'fill': ink_cnt / mask.size})
    return best[1] if best else None


def _summary_anchor(win):
    """列表页「摘要」页签锚（07:47 用户截图实证：附图躺在「摘要」页签左
    侧、同一张结果卡片内）。取名字恰为「摘要」且在内容区（y0>300）的最
    靠上元素 → (x0, y0)；无则 None（调用方回落保守区带）。"""
    best = None
    for ct in ('TabItem', 'Text', 'MenuItem'):
        for t in uia.descendants(win, control_type=ct):
            if (uia._name(t) or '').strip() != '摘要':
                continue
            r = uia._rect(t)
            if r and r[1] > 300 and r[3] > r[1] and (best is None or r[1] < best[1]):
                best = r
    return (best[0], best[1]) if best else None


FIG_ZONE_LEFT = 60                          # 列表卡片左列（缩略图区带）起点
FIG_ZONE_FALLBACK = (FIG_ZONE_LEFT, 420, 420, 940)   # 无锚时的保守区带


def _zone_ink_mass(png_path: str, zone) -> int:
    """区带内深色像素量：>0 说明结果区已有内容，不应再空滚右侧条。"""
    try:
        import numpy as np
        from PIL import Image
        im = Image.open(png_path).convert('L')
        W, H = im.size
        z = clamp_zone(zone, W, H)
        if not z:
            return 0
        arr = np.asarray(im.crop(z))
        return int((arr < 200).sum())
    except Exception:
        return 0


def _pick_from_pane(pane, name: str) -> dict:
    fr, ink = pane['rect'], pane['ink']
    cx = min(max((ink[0] + ink[2]) // 2, fr[0] + 5), fr[2] - 5)
    cy = min(max((ink[1] + ink[3]) // 2, fr[1] + 5), fr[3] - 5)
    return {'name': name, 'rect': list(fr),
            'w': fr[2] - fr[0], 'h': fr[3] - fr[1], 'rel_y': fr[1],
            'click': (cx, cy)}


def _locate_list_figure(win, out_dir: str, step, tries: int = 3):
    """定位摘要附图。图已在视口时禁止空滚右侧条（229 用户勘误）。

    图文模式布局：附图在「摘要」页签**下方**左列（非页签同行左侧）。
    电路图填充率常 >8%，旧 fill_max=0.08 会把真图当密文扔掉。
    """
    from PIL import Image
    shot = os.path.join(out_dir, 'fig_scan.png')
    for i in range(1, tries + 1):
        n_img = n_zero = 0
        for el in uia.descendants(win, control_type='Image'):
            n_img += 1
            r = uia._rect(el)
            if not r or r[2] <= r[0] or r[3] <= r[1]:
                n_zero += 1
        figs = _visible_figures(win)
        pick = pick_figure(figs)
        if pick:
            step('locate_fig', try_=i, by='a11y', rect=pick['rect'],
                 images=len(figs), img_total=n_img, img_zero_rect=n_zero)
            return dict(pick, name=('a11y图元:' + pick['name'])[:40])

        anchor = _summary_anchor(win)
        uia.screenshot_window(win, shot)
        with Image.open(shot) as _im:
            W, H = _im.size
        if anchor:
            ax, ay = anchor
            # 页签下方左列；右缘可略过锚 x（摘要正文在右）
            zone_raw = (FIG_ZONE_LEFT, max(120, ay - 20),
                        max(FIG_ZONE_LEFT + 220, min(ax + 120, W - 80)),
                        min(ay + 560, _footer_y(win) - 5))
        else:
            zone_raw = FIG_ZONE_FALLBACK
        zone = clamp_zone(zone_raw, W, H)
        probe_z = zone or (max(0, zone_raw[0]), max(0, zone_raw[1]),
                           min(W, zone_raw[2]), min(H, zone_raw[3]))
        try:
            Image.open(shot).crop(tuple(probe_z)).save(
                os.path.join(out_dir, f'fig_zone_{i}.png'))
        except Exception:
            pass

        # 电路图 fill 常 8%~20%；ink 上限放宽
        pane_kw = dict(fill_max=0.28, ink_hi=120000, frame_max=720)
        pane = (locate_pane_frame(shot, zone, prefer_y=anchor[1] if anchor else None,
                                  **pane_kw) if zone else None)
        if pane:
            step('locate_fig', try_=i, by='pane', zone=list(zone),
                 frame=pane['rect'], ink_cnt=pane['ink_cnt'],
                 fill=round(pane['fill'], 4),
                 anchor=list(anchor) if anchor else None)
            return _pick_from_pane(pane, '边框窗格(摘要区)')

        pane2 = (locate_pane_frame(shot, zone, prefer_y=None, **pane_kw)
                 if zone else None)
        if pane2:
            step('locate_fig', try_=i, by='pane_no_prefer', zone=list(zone),
                 frame=pane2['rect'], ink_cnt=pane2['ink_cnt'],
                 fill=round(pane2['fill'], 4))
            return _pick_from_pane(pane2, '边框窗格(放宽)')

        box = (ink_box_in_zone(shot, zone, min_ink=400, fill_min=0.012,
                               size_hi=800, edge_touch=2, aspect_max=4.0)
               if zone else None)
        if box:
            step('locate_fig', try_=i, by='ink', zone=list(zone), frame=list(box))
            return {'name': '墨块(摘要区)', 'rect': list(box),
                    'w': box[2] - box[0], 'h': box[3] - box[1], 'rel_y': box[1],
                    'click': ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)}

        mass = _zone_ink_mass(shot, probe_z)
        step('locate_fig', try_=i, by='-', zone=list(probe_z), zone_ink=mass,
             warn='区带未识别附图窗格',
             images=len(figs), img_total=n_img, img_zero_rect=n_zero)
        # 区带已有明显墨迹=结果已在视口，再滚右侧条只会空转（用户勘误）
        if mass >= 800:
            step('locate_fig', stop='zone_has_ink_no_scroll', zone_ink=mass)
            break
        if i < tries:
            time.sleep(1.2)
            sx = max(120, (probe_z[0] + min(probe_z[2], probe_z[0] + 280)) // 2)
            sy = (probe_z[1] + probe_z[3]) // 2
            uia.human_click(win, sx, sy, settle=0.15)
            uia.scroll_down(win, sx, sy, notches=1)
            time.sleep(0.8)
    return None


def _footer_y(win) -> int:
    """页脚带 y 起点（07:09 实证：党政机关徽标躺在"政府网站/找错"同一行）。
    取页脚标记文本最小 y0；无标记返回大数（不排除）。"""
    ys = []
    for t in uia.descendants(win, control_type='Text'):
        n = uia._name(t) or ''
        if any(m in n for m in FOOTER_MARKS):
            r = uia._rect(t)
            if r and r[1] >= 0 and r[3] > r[1]:
                ys.append(r[1])
    return min(ys) if ys else 10 ** 9


def drop_footer_band(cands: list, footer_y: int) -> list:
    """剔除页脚带候选图（纯函数，自测冻结）：y0 ≥ footer_y-5 全是徽标/logo。"""
    return [c for c in cands if c.get('rect', [0, 10 ** 9])[1] < footer_y - 5]


def _visible_figures(win) -> list:
    """当前视口合格候选图（a11y Image → 去页脚带）。"""
    return drop_footer_band(_img_candidates(win), _footer_y(win))


def _fetch_crop(win, pick: dict, out_dir: str, step) -> str:
    """A 路径：整窗截图 → Image 元素矩形裁剪（+2x 放大提 OCR/查看清晰度）。"""
    import cnipa_ocr as co
    shot = os.path.join(out_dir, 'page.png')
    uia.screenshot_window(win, shot)
    r = pick['rect']
    fig = os.path.join(out_dir, 'figure_crop.png')
    co.prep_crop(shot, (r[0], r[1], r[2], r[3]), fig, scale=2)
    step('fetch_crop', rect=r, out=fig)
    return fig


def _fetch_original(win, pick: dict, out_dir: str, step) -> str:
    """B 路径：右键图 →「在新标签页中打开图片」→ 整窗截图 → 白边自动裁剪。
    任一步失败返回 ''（调用方回落 A）。"""
    r = pick['rect']
    # 14:13：窗格定位的 pick 带 click=框内墨块中心——右键须打在图上而非
    # 边框/空白处，否则 Chrome 弹的是页面菜单，没有「打开图片」项
    cx, cy = pick.get('click') or ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    uia.human_right_click(win, cx, cy)
    time.sleep(0.5)
    target = None
    for m in uia.descendants(win, control_type='MenuItem'):
        if match_open_image_menu(uia._name(m)):
            mr = uia._rect(m)
            if mr:
                target = mr
                break
    if target is None:
        uia.safe_send_keys('{ESC}', win=win, pause=0.05, log=step)
        step('fetch_orig', warn='右键菜单未见「在新标签页中打开图片」')
        return ''
    uia.human_click(win, (target[0] + target[2]) // 2, (target[1] + target[3]) // 2)
    time.sleep(2.5)                        # 新标签原生渲染整图
    shot = os.path.join(out_dir, 'orig_tab.png')
    uia.screenshot_window(win, shot)
    fig = os.path.join(out_dir, 'figure_orig.png')
    autocrop_figure(shot, fig)
    # 用完即关（回到检索标签，恢复现场）
    uia.safe_send_keys('^w', win=win, pause=0.05, log=step)
    time.sleep(0.8)
    step('fetch_orig', menu=(target[1], target[0]), out=fig)
    return fig


def _is_save_dialog_like(w) -> bool:
    """内容认证：窗口里有「保存」按钮或「文件名」标签 → 是另存为对话框。
    15:05 复盘：经典 #32770、标题「另存为」的对话框在 8s 轮询里竟未命中
    （UIA 下 window_text 可能读空/抛错）——认内容不认门牌。"""
    try:
        for b in uia.descendants(w, control_type='Button'):
            n = (uia._name(b) or '').strip()
            if n and ('保存' in n or 'save' in n.lower()):
                return True
        for t in uia.descendants(w, control_type='Text'):
            n = uia._name(t) or ''
            if '文件名' in n or 'file name' in (n or '').lower():
                return True
    except Exception:
        pass
    return False


def _find_save_dialog(timeout_s: float = 15.0, probe_path=None):
    """找「另存为」对话框：只查前台窗（点菜单后对话框即前台）。

    实跑（205052）：duration＜1s。不做 Desktop.windows / EnumWindows 全桌面枚举。
    """
    import ctypes
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        try:
            h = ctypes.windll.user32.GetForegroundWindow()
            if h:
                w = uia.uia_window_from_hwnd(h)
                if w is not None:
                    cls = (w.element_info.class_name or '')
                    title = (w.window_text() or '')
                    last = {'cls': cls, 'title': title[:80]}
                    tl = title.lower()
                    if ((cls == '#32770' or '另存' in title or '保存' in title
                         or 'save' in tl) and _is_save_dialog_like(w)):
                        return w
        except Exception:
            pass
        time.sleep(0.08)
    if probe_path:
        try:
            with open(probe_path, 'w', encoding='utf-8') as f:
                json.dump([last] if last else [], f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return None


def _fetch_saveas(win, pick: dict, out_dir: str, step, ext: str = '.png') -> str:
    """C 主路径（2026-09-02 08:54 用户提议；09:10 勘误）：右键附图 →
    点「图片另存为…」→ 对话框文件名栏直接键入完整路径 **带目标扩展名**
    out_dir\\figure_saveas{ext}——另存本可任选格式（保存类型下拉/文件名
    带扩展名均可），无需「先存 .jpg 再 PIL 转码」→ 保存 → 轮询等文件落盘。
    返回文件路径；任一步失败返回 ''（回落 B/A）。"""
    ext = (ext or '').strip().lower()
    if not ext.startswith('.'):
        ext = ('.' + ext) if ext else '.png'
    # 15:05 用户点名：另存文件名带时间戳（对话框默认名是裸的 image）
    base = os.path.join(out_dir, 'figure_saveas_' + time.strftime('%H%M%S'))
    for old in glob.glob(os.path.join(out_dir, 'figure_saveas_*')):  # 清残留
        try:
            os.remove(old)
        except OSError:
            pass
    r = pick['rect']
    # 14:13：右键点 pick['click']（框内墨块中心）而非框中心——框中心可能
    # 是空白处，弹出的会是页面菜单而非图片菜单
    cx, cy = pick.get('click') or ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    uia.human_right_click(win, cx, cy)
    time.sleep(0.6)
    target = None
    for m in uia.descendants(win, control_type='MenuItem'):
        if match_save_image_menu(uia._name(m)):
            mr = uia._rect(m)
            if mr:
                target = mr
                break
    if target is None:
        uia.safe_send_keys('{ESC}', win=win, pause=0.05, log=step)
        step('fetch_saveas', warn='右键菜单没有「图片另存为」')
        return ''
    uia.human_click(win, (target[0] + target[2]) // 2,
                    (target[1] + target[3]) // 2)
    dlg = _find_save_dialog(
        probe_path=os.path.join(out_dir, 'saveas_windows_probe.json'))
    if dlg is None:
        # 15:03 教训：对话框可能其实开着只是没认出——ESC 收现场，防模态
        # 拦住后续 B/A 路径的右键/截图
        uia.safe_send_keys('{ESC}', win=win, pause=0.05, log=step)
        step('fetch_saveas', warn='「另存为」对话框未识别'
                                  '（看 saveas_windows_probe.json）')
        return ''
    try:
        edits = uia.descendants(dlg, control_type='Edit')
        if edits:
            # 「文件名」框：取位置最靠下的 Edit（新版对话框上部还有搜索框）
            fname = max(edits, key=lambda e: (uia._rect(e) or (0, 0, 0, 0))[1])
            uia.clear_and_type(dlg, fname, base + ext)   # 完整路径+目标扩展名
            clicked = False
            for b in uia.descendants(dlg, control_type='Button'):
                n = uia._name(b) or ''
                if ('保存' in n or n.lower().startswith('save')
                        or 'save' in n.lower()):
                    uia.click(dlg, b)
                    clicked = True
                    break
            if not clicked:                      # 兜底：文件名框里直接回车
                uia.safe_send_keys('{ENTER}', win=dlg, pause=0.05, log=step)
        else:
            # 键盘兜底：对话框打开时焦点默认在文件名框、默认名处于全选态
            # ——Ctrl+A 全选后直接键入完整路径整体替换，回车保存
            step('fetch_saveas', note='未见文件名输入框，键盘直打')
            uia.safe_send_keys('^a', win=dlg, pause=0.05, log=step)
            uia.safe_send_keys(base + ext, win=dlg, pause=0.02,
                               with_spaces=True, log=step)
            uia.safe_send_keys('{ENTER}', win=dlg, pause=0.05, log=step)
    except Exception as e:
        step('fetch_saveas', warn=f'对话框操作失败: {e}')
        try:
            uia.safe_send_keys('{ESC}', win=win, pause=0.05, log=step)
        except Exception:
            pass
        return ''
    full = base + ext
    for _ in range(20):                          # 等落盘（≤6s）
        if os.path.isfile(full):                 # 正常：按所键扩展名落盘
            step('fetch_saveas', menu=(target[1], target[0]), out=full)
            return full
        hits = [p for p in glob.glob(os.path.join(out_dir, 'figure_saveas_*'))
                if os.path.isfile(p)]
        if hits:                                 # 站点意外补了别的扩展名：收下，第5步兜底
            step('fetch_saveas', menu=(target[1], target[0]), out=hits[0],
                 note=f'扩展名非 {ext}，第5步统一')
            return hits[0]
        time.sleep(0.3)
    uia.safe_send_keys('{ESC}', win=win, pause=0.05, log=step)
    step('fetch_saveas', warn='点了保存但文件未出现（已 ESC 关对话框）')
    return ''


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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
    out_dir = os.path.join(ROOT, 'output', 'uia_runs', f'figure_{ts}')
    os.makedirs(out_dir, exist_ok=True)
    payload: dict = {'mode': 'uia_pss_figure', 'run_dir': out_dir,
                     'input_file': argv[1],
                     'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'steps': []}

    def step(name: str, **kw) -> None:
        kw['step'] = name
        payload['steps'].append(kw)
        print(f"[figure] {name}: {kw}")

    application_no = cnipa_contract.dotted_appno(
        (args.get('application_no') or '').strip()) or (args.get('application_no') or '').strip()
    if not application_no:
        payload['ok'] = False
        payload['error'] = 'application_no 必填'
        _finish(payload, out_dir)
        return 1
    key = normalize_search_key(application_no)
    payload['application_no'] = application_no
    payload['search_key'] = key

    # —— 本地缓存优先：盘上/cache 已有 PNG → 直接返回（含 force_refresh；
    #    要重抓请先删 cnipa_file/{app}/{app}.png 或 figures 下文件）。
    #    force_refresh 仅清 .no_figure，便于「确认无图」后重试 PSS。
    force_refresh = bool(args.get('force_refresh'))
    save_arg = (args.get('save_path') or '').strip() or None
    hit = cnipa_cache.load_figure(application_no, save_path=save_arg)
    if hit is not None and hit.get('kind') == 'png':
        payload['figure_path'] = hit['path']
        payload['method'] = 'cache'
        payload['from_cache'] = True
        payload['cache_file'] = hit['path']
        payload['elapsed_s'] = round(time.time() - t0, 1)
        payload['ok'] = True
        _finish(payload, out_dir)
        print(f"[figure] cache hit => {hit['path']}")
        return 0
    if force_refresh:
        cnipa_cache.clear_no_figure(application_no)
    elif hit is not None and hit.get('kind') == 'no_figure':
        payload['ok'] = False
        payload['error'] = ('无摘要附图（本地负缓存命中，未访问 pss；'
                            '确需重试请 force_refresh=true）')
        payload['from_cache'] = True
        _finish(payload, out_dir)
        print('[figure] no_figure cache hit（未访问 pss）')
        return 1

    # 1) 找一个可交互的 Chrome 窗口（find_any_chrome 已按进程名 chrome.exe
    #    过滤——首跑教训：类名 Chrome_WidgetWin_1 会命中 Electron 宿主/Edge）。
    #    多窗口时优先标题带 cpquery 的（登录会话在那个窗口）。
    try:
        return _main_live(args, application_no, key, payload, out_dir, step, t0)
    except Exception as e:
        payload['ok'] = False
        payload['error'] = f'RUNTIME_ERROR: {e}'[:500]
        payload['elapsed_s'] = round(time.time() - t0, 1)
        step('crash', err=str(e)[:200])
        try:
            _finish(payload, out_dir)
        except Exception:
            try:
                cnipa_contract.maybe_emit('figure', payload)
            except Exception:
                pass
        print(f'[figure] 异常退出: {e}')
        return 1


def _main_live(args, application_no, key, payload, out_dir, step, t0) -> int:
    chromes = uia.find_any_chrome()
    usable = [c for c in chromes if not c[1]['minimized']]
    if not usable:
        payload['ok'] = False
        payload['error'] = '无可交互的浏览器窗口'
        _finish(payload, out_dir)
        return 1
    def _win_has_pss_tab(w):
        for t in uia.descendants(w, control_type='TabItem'):
            n = (uia._name(t) or '')
            if '常规检索' in n or '检索结果' in n or '专利检索及分析' in n:
                return True
        return False

    pref_pss = [c for c in usable if '常规检索' in (c[1].get('title') or '')
                or '专利检索' in (c[1].get('title') or '')]
    tab_pss = [c for c in usable if _win_has_pss_tab(c[0])]
    win = (pref_pss or tab_pss or usable)[0][0]
    step('window', title=win.window_text(), chrome_n=len(chromes),
         pref_pss=bool(pref_pss), has_pss_tab=bool(tab_pss))

    def probe(tag: str) -> None:
        try:
            uia.screenshot_window(win, os.path.join(out_dir, f'{tag}.png'))
        except Exception as e:
            step('probe', warn=f'screenshot: {e}')
        try:
            payload['tree_flat'] = flat_tree_dump(
                win, os.path.join(out_dir, f'{tag}_tree.txt'))
        except Exception as e:
            step('probe', warn=f'tree: {e}')
        try:
            with open(os.path.join(out_dir, f'{tag}_texts.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(uia.all_texts(win), f, ensure_ascii=False, indent=1)
        except Exception as e:
            step('probe', warn=f'texts: {e}')

    if uia.dismiss_chrome_restore_bubble(win):
        step('dismiss_restore', ok=True)

    # 入口 tab 快照：结束时只关本次新开的，不动 229 上原有大量 tab
    tabs_at_entry = _list_top_tab_names(win)
    step('tabs_at_entry', n=len(tabs_at_entry),
         sample=[x[:20] for x in tabs_at_entry[:8]])

    def cleanup_opened_tabs() -> None:
        if args.get('keep_tab'):
            step('cleanup', skipped=True, why='keep_tab')
            return
        try:
            n = _close_tabs_opened_since(win, tabs_at_entry, step)
            payload['cleanup_ok'] = True
            step('cleanup', closed_n=n, by='new_tabs_since_entry')
        except Exception as e:
            step('cleanup', warn=str(e)[:80])

    try:
        return _main_live_body(
            args, application_no, key, payload, out_dir, step, t0,
            win, probe, cleanup_opened_tabs)
    finally:
        cleanup_opened_tabs()


def _main_live_body(args, application_no, key, payload, out_dir, step, t0,
                    win, probe, cleanup_opened_tabs) -> int:
    # 2) 优先复用已有「常规检索」标签；没有/切不过去才开 pss
    if not _activate_pss_tab(win, step):
        _open_url_tab(win, PSS_URL, step)
    probe('01_loaded')
    state0 = _wait_pss_ready(win, step=step)
    # 前台仍是 cpquery/结果页：强制再开/再切 PSS，禁止在案件查询页干等
    if state0 == '非PSS(cpquery)':
        step('wrong_page', state=state0, action='force_open_pss')
        if not _activate_pss_tab(win, step):
            _open_url_tab(win, PSS_URL, step)
        state0 = _wait_pss_ready(win, step=step, timeout_s=25.0)
    # initpage/空白壳：勿在空白页乱点；强制进常规检索（含 cponline.cn/Initpage）
    if (state0 in ('空壳(挑战/加载中?)', '仅浏览器框(a11y未开?)', '未知')
            or _title_looks_initpage(win)):
        state0 = _recover_pss_from_blank(win, out_dir, step, state0)
    # 未知但标题已是统一身份 → 按登录墙处理（a11y 漏字时）
    try:
        _tit = win.window_text() or ''
    except Exception:
        _tit = ''
    if state0 in ('未知', '空壳(挑战/加载中?)') and any(
            m in _tit for m in ('统一身份', '身份认证')):
        step('pss_state', state=state0, promote='登录墙', title=_tit[:40])
        state0 = '登录墙'
    probe('01b_state')
    texts_login_probe = uia.all_texts(win)
    if (state0 in ('未知', '空壳(挑战/加载中?)', '仅浏览器框(a11y未开?)')
            and _login_page_visible(texts_login_probe)
            and not is_pss_logged_in(texts_login_probe)):
        step('pss_state', state=state0, promote='登录墙', why='login_visible')
        state0 = '登录墙'
    step('pss_state', state=state0)
    # 仅真登录墙先登；PSS 访客壳（顶栏登录|注册）先填号检索，由站点跳转 SSO
    if state0 == LOGGED_OUT:
        step('pss_state', note='guest_shell_search_first')
        state0 = '搜索表单'
    if state0 == '登录墙' and not is_pss_logged_in(uia.all_texts(win)):
        probe('01b_logged_out')
        ok_login, detail = _ensure_pss_login(win, out_dir, step)
        if not ok_login:
            payload['ok'] = False
            payload['error'] = f'pss 登录墙，自动登录失败：{detail}'
            _finish(payload, out_dir)
            return 1
        state0 = detail
        probe('01b_relogin_ok')
        step('pss_state', state=state0, after_login=True)
    elif state0 == '登录墙' and is_pss_logged_in(uia.all_texts(win)):
        step('pss_state', state=state0, note='skip_login_already_in')
        state0 = '搜索表单' if is_pss_ui(' '.join(uia.all_texts(win))) else state0
    if state0 not in ('搜索表单', '疑似结果区', '无命中'):
        t_fix = uia.all_texts(win)
        if _login_page_visible(t_fix) and not is_pss_logged_in(t_fix):
            ok_login, detail = _ensure_pss_login(win, out_dir, step)
            if not ok_login:
                payload['ok'] = False
                payload['error'] = f'pss 登录墙，自动登录失败：{detail}'
                _finish(payload, out_dir)
                return 1
            state0 = detail if detail in ('搜索表单', '疑似结果区', '无命中') else '搜索表单'
            step('pss_state', state=state0, after_login_retry=True)
        else:
            probe('01c_timeout')
            payload['ok'] = False
            payload['error'] = f'pss {timeout_note(state0)}（看 01b_state/01c 探针）'
            _finish(payload, out_dir)
            return 1

    if not looks_like_patent_application_no(application_no):
        payload['ok'] = False
        payload['error'] = f'附图申请号无效（非专利号）: {application_no!r}'
        _finish(payload, out_dir)
        return 1

    # 3) 申请号检索：未弹出「检索结果」页时同号最多重试 3 次（站点偶发，不换号）
    try_key = preferred_search_key(application_no, key)
    payload['search_key'] = try_key
    st1 = '未知'
    cands = []
    mid_login_done = False
    result_page_ok = False
    for ki in range(PSS_SEARCH_MAX_ATTEMPTS):
        uia.dismiss_chrome_restore_bubble(win)
        if ki > 0:
            _activate_pss_tab(win, step)
            step('search_retry', key=try_key, why='no_result_page_same_key',
                 attempt=ki + 1, max=PSS_SEARCH_MAX_ATTEMPTS)
        tabs_before = _list_top_tab_names(win)
        if not _fill_appno(win, try_key, step):
            if ki == 0:
                probe('02_fill_fail')
                payload['ok'] = False
                payload['error'] = '申请号输入框未定位（看 02_fill_fail 探针）'
                _finish(payload, out_dir)
                return 1
            continue
        cands = _wait_results(win, step=step, before_tabs=tabs_before)
        st1 = page_state(uia.all_texts(win))
        for _ in range(6):
            if st1 in ('疑似结果区', '无命中', '登录墙', LOGGED_OUT):
                break
            time.sleep(1.2)
            st1 = page_state(uia.all_texts(win))
        if (st1 in ('空壳(挑战/加载中?)', '仅浏览器框(a11y未开?)', '未知')
                or _title_looks_initpage(win)):
            st1 = _recover_pss_from_blank(win, out_dir, step, st1)
            tabs_before = _list_top_tab_names(win)
            if _fill_appno(win, try_key, step):
                cands = _wait_results(win, step=step, before_tabs=tabs_before)
                st1 = page_state(uia.all_texts(win))
                for _ in range(6):
                    if st1 in ('疑似结果区', '无命中', '登录墙', LOGGED_OUT):
                        break
                    time.sleep(1.2)
                    st1 = page_state(uia.all_texts(win))
        if (st1 in ('登录墙', LOGGED_OUT) or _login_page_visible(uia.all_texts(win))
                or _title_looks_sso(win)) and not mid_login_done:
            mid_login_done = True
            step('result_state', state=st1, key=try_key, action='mid_search_login')
            ok_login, detail = _ensure_pss_login(win, out_dir, step)
            if not ok_login:
                payload['ok'] = False
                payload['error'] = f'检索中途登录失败：{detail}'
                _finish(payload, out_dir)
                return 1
            _activate_pss_tab(win, step)
            tabs_before = _list_top_tab_names(win)
            if not _fill_appno(win, try_key, step):
                payload['ok'] = False
                payload['error'] = '登录后申请号输入框未定位'
                _finish(payload, out_dir)
                return 1
            cands = _wait_results(win, step=step, before_tabs=tabs_before)
            st1 = page_state(uia.all_texts(win))
            for _ in range(6):
                if st1 in ('疑似结果区', '无命中'):
                    break
                time.sleep(1.2)
                st1 = page_state(uia.all_texts(win))
        popped = _result_page_popped(win, tabs_before, st1)
        step('result_state', state=st1, key=try_key, result_page=popped)
        if not popped:
            continue
        result_page_ok = True
        key = try_key
        break
    if not result_page_ok:
        probe('02b_no_result')
        payload['ok'] = False
        payload['error'] = (
            f'同号检索 {PSS_SEARCH_MAX_ATTEMPTS} 次后仍未进入检索结果页'
            f'（key={try_key!r} state={st1}）——看 02b_no_result 探针')
        _finish(payload, out_dir)
        return 1
    if st1 == '无命中':
        step('search', note='结果页已打开但 0 命中', key=key)
    # 附图在「检索结果」页：有该标签就切过去，再定位（否则会在常规检索空滚）
    _activate_results_tab(win, step)
    time.sleep(3.5)                                  # 缩略图懒加载
    pick = _locate_list_figure(win, out_dir, step)
    a11y_figs = _visible_figures(win)                # 遥测：a11y 图元（对照）
    payload['img_candidates'] = [{k: c[k] for k in ('name', 'rect', 'w', 'h', 'rel_y')}
                                 for c in a11y_figs[:20]]
    step('pick', found=bool(pick), images=len(a11y_figs))
    if not pick:
        probe('03_no_figure')
        payload['ok'] = False
        payload['error'] = ('「摘要」区未定位到边框附图窗格——看 fig_zone_* 探针'
                            '（14:13 定案：附图不进 a11y 树、稀疏图过不了墨块'
                            '门槛，改认边框骨架；探针可判窗格未渲染/滚出视口/'
                            '锚偏移）')
        # 真无图点（§3.4 负缓存）：结果区在场但无附图窗格 → 写 .no_figure 标记，
        # 后续同号直接 NO_FIGURE 不开 pss。仅此处写——其它失败（登录过期/超时/
        # 0 命中）不是「无图」，写标记会毒化重试。
        cnipa_cache.write_no_figure(application_no)
        _finish(payload, out_dir)
        return 1

    # 4) 三段式取图（08:54 用户定案）：C 另存为原图优先 → B 新标签整图
    #    → A 截图裁剪保底（一定能出图）
    figure_path = ''
    method = ''
    save_path = (args.get('save_path') or '').strip()
    # 09:10 用户勘误：另存对话框可任选保存格式，C 路径直接按 save_path
    # 扩展名另存（未指定时默认 .png），不做「存 .jpg 再转码」
    dst_ext = (os.path.splitext(save_path)[1].lower() if save_path else '') or '.png'
    try:
        fig_c = _fetch_saveas(win, pick, out_dir, step, dst_ext)
        if fig_c and os.path.isfile(fig_c):
            figure_path, method = fig_c, 'saveas'
    except Exception as e:
        step('fetch_saveas', warn=f'C 路径异常: {e}')
    if not figure_path:
        try:
            fig_b = _fetch_original(win, pick, out_dir, step)
            if fig_b and os.path.isfile(fig_b):
                figure_path, method = fig_b, 'orig_tab'
        except Exception as e:
            step('fetch_orig', warn=f'B 路径异常: {e}')
    if not figure_path:
        try:
            fig_a = _fetch_crop(win, pick, out_dir, step)
            if fig_a and os.path.isfile(fig_a):
                figure_path, method = fig_a, 'crop'
        except Exception as e:
            step('fetch_crop', warn=f'A 路径异常: {e}')

    # 5) 落盘到 save_path（可选）。C 路径已按目标扩展名直接另存 → 字节拷贝
    #    即可；仅 B/A（截图产物恒为 .png）或另存扩展名被意外改写时才 PIL
    #    转码兜底，不做「jpg 字节改名叫 .png」的假文件）
    final_path = figure_path
    if figure_path and save_path:
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        src_ext = os.path.splitext(figure_path)[1].lower()
        dst_ext = os.path.splitext(save_path)[1].lower()
        if src_ext and dst_ext and src_ext != dst_ext:
            from PIL import Image
            with Image.open(figure_path) as im:
                im.save(save_path)
            step('save', path=os.path.abspath(save_path), transcode=f'{src_ext}→{dst_ext}')
        else:
            with open(figure_path, 'rb') as src, open(save_path, 'wb') as dst:
                dst.write(src.read())
            step('save', path=os.path.abspath(save_path))
        final_path = os.path.abspath(save_path)
    payload['figure_path'] = final_path
    payload['method'] = method

    # 6) 关本次新开 tab 由 _main_live.finally → cleanup_opened_tabs 统一处理
    cleanup_opened_tabs()

    payload['elapsed_s'] = round(time.time() - t0, 1)
    payload['ok'] = bool(figure_path)
    if payload['ok']:
        try:
            cf = cnipa_cache.write_figure(application_no, figure_path)
            if cf:
                payload['cache_file'] = cf
                payload['from_cache'] = False
        except Exception as e:
            step('cache_write', warn=str(e)[:120])
    try:
        _finish(payload, out_dir)
    except Exception as e:
        step('finish', warn=str(e)[:120])
        try:
            cnipa_contract.maybe_emit('figure', payload)
        except Exception:
            pass
    print(f"[figure] {'成功' if payload['ok'] else '失败'}：method={method or '-'}, "
          f"耗时 {payload.get('elapsed_s')}s，产物目录：{out_dir}")
    return 0 if payload['ok'] else 1


def _finish(payload: dict, out_dir: str) -> None:
    """payload=交付物 / debug=遥测（04 20:50 惯例）：steps/shots/tree 移 debug。"""
    debug = {k: payload.pop(k) for k in ('steps', 'tree_flat') if k in payload}
    debug['note'] = '遥测（每步动作/树 dump）；结果看 payload.json'
    with open(os.path.join(out_dir, 'payload.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, 'debug.json'), 'w', encoding='utf-8') as f:
        json.dump(debug, f, ensure_ascii=False, indent=1)
    cnipa_contract.maybe_emit('figure', payload)


# ---------------------------------------------------------------------------
# 自测（纯函数，离线可跑）
# ---------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    fails = []

    def check(name: str, cond: bool) -> None:
        print(('[PASS] ' if cond else '[FAIL] ') + name)
        if not cond:
            fails.append(name)

    # 1) 申请号规范化（pss 格式：CN+12流水[.校验]，22:04 页面帮助实证）
    SMART_PH = ('请输入关键词、申请号/公开号、申请人/发明人、申请日/公开日、'
                'IPC分类号/CPC分类号，系统根据规则智能识别检索')
    check('P1) 规范化：优先 13 位带点（PSS 申请号检索）',
          normalize_search_key('CN201210005859.5') == '201210005859.5'
          and normalize_search_key('zl 200610004173.6') == '200610004173.6'
          and normalize_search_key('2006100041736') == '200610004173.6'
          and normalize_search_key('200610004173.6') == '200610004173.6'
          and normalize_search_key('') == ''
          and normalize_search_key('CN200410082265') == 'CN200410082265')
    check('M5) 智能框识别：占位文案+y>100（排除 y51 地址栏）',
          is_smart_search_edit(SMART_PH, 476)
          and not is_smart_search_edit('地址和搜索栏', 476)
          and not is_smart_search_edit(SMART_PH, 51)
          and not is_smart_search_edit('', 476))

    # 2) 选图：logo(小)/横幅(扁)/头带图全排除，结果区中图胜出
    cands = [
        {'name': '', 'rect': [10, 60, 100, 100], 'w': 90, 'h': 40, 'rel_y': 60},    # 头带 logo
        {'name': '', 'rect': [200, 260, 1200, 290], 'w': 1000, 'h': 30, 'rel_y': 260},  # 扁横幅
        {'name': '', 'rect': [300, 400, 420, 520], 'w': 120, 'h': 120, 'rel_y': 400},   # 附图
        {'name': '', 'rect': [500, 420, 540, 460], 'w': 40, 'h': 40, 'rel_y': 420},     # 小 icon
    ]
    pk = pick_figure(cands)
    check('P2) 选图：排除头带/横幅/小icon，取结果区图', pk.get('rect') == [300, 400, 420, 520])
    check('P3) 选图：全不合格 → 空', pick_figure(cands[:2]) == {})

    # 4) 右键菜单词匹配
    check('M1) 菜单词：中文名命中',
          match_open_image_menu('在新标签页中打开图片(T)'))
    check('M2) 菜单词：链接类排除',
          not match_open_image_menu('在新标签页中打开链接(T)'))
    check('M3) 菜单词：复制/下载排除',
          not match_open_image_menu('复制图片') and not match_open_image_menu('下载图片'))
    check('M4) 菜单词：空/无关不命中',
          not match_open_image_menu('') and not match_open_image_menu('检查(I)'))

    # 4b) 「图片另存为」菜单词（2026-09-02 08:54 用户实拍菜单回归）
    check('M6) 另存菜单词：图片另存为…/将图像另存为…/英文命中',
          match_save_image_menu('图片另存为…')
          and match_save_image_menu('将图像另存为…')
          and match_save_image_menu('Save image as…'))
    check('M7) 另存菜单词：链接另存为/将页面另存为 排除（一词之差）',
          not match_save_image_menu('链接另存为…')
          and not match_save_image_menu('将页面另存为…')
          and not match_save_image_menu('Save link as…'))
    check('M8) 另存菜单词：复制图片/二维码/打开图片 排除',
          not match_save_image_menu('复制图片')
          and not match_save_image_menu('为此图片创建二维码')
          and not match_save_image_menu('在新标签页中打开图片'))
    check('M9) 另存菜单词：空/None 安全',
          not match_save_image_menu('') and not match_save_image_menu(None))

    # 5) 白边自动裁剪：白底 + 偏移黑块 → 精确 bbox
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), 'cnipa_fig_autocrop.png')
    im = Image.new('RGB', (400, 300), (255, 255, 255))
    for x in range(120, 260):
        for y in range(80, 220):
            im.putpixel((x, y), (10, 10, 10))
    im.save(tmp)
    out = os.path.join(tempfile.gettempdir(), 'cnipa_fig_autocrop_out.png')
    autocrop_figure(tmp, out)
    w, h = Image.open(out).size
    check('A1) 自动裁剪：黑块 bbox（容差±6px）',
          abs(w - 140) <= 8 and abs(h - 140) <= 8)
    im2 = Image.new('RGB', (200, 100), (240, 240, 240))   # 全底色 → 原样返回
    im2.save(tmp)
    autocrop_figure(tmp, out)
    w2, h2 = Image.open(out).size
    check('A2) 自动裁剪：全底色不裁空', (w2, h2) == (200, 100))

    # 6) 页态分类
    check('S1) 页态：登录墙',
          page_state(['请登录', '账号', '密码']) == '登录墙')
    check('S1a) tysf统一身份 → 登录墙',
          page_state(['专利和集成电路布图设计业务办理统一身份认证平台',
                      '自然人登录', '手机号/证件号码', '请输入密码',
                      '登录', '立即注册']) == '登录墙')
    check('S1b) 已登录PSS不因单字登录进墙',
          page_state(['专利检索及分析', '常规检索', '请输入关键词', '申请号'])
          == '搜索表单')
    check('S2) 页态：搜索表单',
          page_state(['专利检索及分析', '申请号', '检索', '请输入关键词'])
          == '搜索表单')
    check('S2b) 页态：暂无检索数据 → 无命中',
          page_state(['筛选', '暂无检索数据', '申请号']) == '无命中')
    check('S2c) 申请号变体 CN+带点优先',
          search_key_variants('200610004173.6')[0] == 'CN200610004173.6'
          and '200610004173.6' in search_key_variants('CN200610004173.6'))
    check('S2e) 首选检索键 CN+带点',
          preferred_search_key('202010200834.2') == 'CN202010200834.2')
    check('S2d) cpquery案件查询不得当搜索表单',
          page_state(['中国及多国专利审查信息查询', '案件查询', '申请号',
                      '查询', '共查询到 0 条结果']) == '非PSS(cpquery)')
    check('S3) 页态：空壳',
          page_state(['', '']) == '空壳(挑战/加载中?)')
    check('S4) 超时话术：空壳/未知有对应文案',
          '未出内容' in timeout_note('空壳(挑战/加载中?)')
          and '页态不明' in timeout_note('未知'))
    # 7) 免责声明弹层（2026-09-01 21:50 实跑回归）
    disc = ['免责声明', '使用前必读', '记住我的选择', '检索', '登录',
            '检索出的结果不承担责任', '010-62088060']
    check('S5) 页态：弹层优先于登录墙/结果区', page_state(disc) == '免责声明')
    check('S6) 回归：弹层正文含「结果」不再误判结果区',
          page_state(disc) != '疑似结果区')
    check('S7) 弹层判定：页脚「免责声明」链接不算弹层',
          not disclaimer_open_by_texts(['免责声明', '检索', '申请号']))
    check('S8) 弹层判定：命中「记住我的选择」即算',
          disclaimer_open_by_texts(['记住我的选择']))

    # 8) 未登录 / 结果区强证据（2026-09-02 06:32 假成功回归，原文回放）
    form_logged_out = ['专利检索及分析', '检索', '热门工具', '登录', '注册', '中文',
                       '常规检索', '自动识别', '申请号', '发明名称', '公开号',
                       '请输入关键词、申请号/公开号、申请人/发明人、申请日/公开日、'
                       'IPC分类号/CPC分类号，系统根据规则智能识别检索',
                       '检索模式：自动识别',
                       '7.中国专利申请号长度12位或14位可以正常检索，例如201920517482.6'
                       '或201920517482；如果长度是13位，需删除最后一位检索',
                       '京ICP备05069085号-14']
    check('S9) 页态：表单+登录|注册 → 未登录（须先登，禁访客直填）',
          page_state(form_logged_out) == LOGGED_OUT)
    check('S9a) is_pss_logged_in：访客表单+登录|注册 不得判已登录',
          not is_pss_logged_in(form_logged_out))
    check('S9b) 页态：仅登录|注册无表单 → 未登录',
          page_state(['登录', '注册', '中文']) == LOGGED_OUT)
    check('S10) 回归：空表单+帮助数字不再误判结果区',
          page_state([t for t in form_logged_out if t not in ('登录', '注册')])
          == '搜索表单')
    check('S11) 页态：命中数「N条」强证据 → 结果区（登录词同现不误判）',
          page_state(['共 12 条', '发明名称', '登录', '注册']) == '疑似结果区'
          and page_state(['命中', '检索']) == '疑似结果区')
    check('S12) 页态：树里只有地址栏 → a11y 未开',
          page_state(['地址和搜索栏']) == '仅浏览器框(a11y未开?)'
          and '内容通道' in timeout_note('仅浏览器框(a11y未开?)'))

    # 9) 列表页像素定位（2026-09-02 07:47 用户勘误回归：附图在列表卡片
    #    左列、canvas 渲染不进 a11y 树；07:09「进详情页」链路作废）
    check('F1) 页脚带排除：徽标(616,954)剔除、正文图保留',
          drop_footer_band([
              {'name': '', 'rect': [300, 500, 420, 620], 'w': 120, 'h': 120},
              {'name': '党政机关徽标', 'rect': [616, 954, 697, 1035], 'w': 81, 'h': 81},
          ], footer_y=940) == [
              {'name': '', 'rect': [300, 500, 420, 620], 'w': 120, 'h': 120}])
    import tempfile as _tf
    from PIL import Image as _Img
    with _tf.TemporaryDirectory() as _td:
        _blk = _Img.new('L', (600, 800), 255)     # 黑块 150x150 @ (100,300)
        _px = _blk.load()
        for _x in range(100, 250):
            for _y in range(300, 450):
                _px[_x, _y] = 20
        _p1 = os.path.join(_td, 'a.png')
        _blk.save(_p1)
        check('F2) 墨块定位：区带内 150x150 黑块 → 整图坐标外接框',
              ink_box_in_zone(_p1, (60, 200, 400, 700)) == (100, 300, 250, 450))
        check('F3) 空区带（缩略图未懒加载）→ None',
              ink_box_in_zone(_p1, (300, 30, 560, 180)) is None)
        _bar = _Img.new('L', (600, 800), 255)     # 26px 宽细竖条（滚动条）
        _bx = _bar.load()
        for _x in range(300, 326):
            for _y in range(200, 700):
                _bx[_x, _y] = 20
        _p2 = os.path.join(_td, 'b.png')
        _bar.save(_p2)
        check('F4) 细竖条（滚动条）→ None，不误当附图',
              ink_box_in_zone(_p2, (260, 150, 400, 750)) is None)
        check('F5) 贴边拒绝：外接框贴区带右缘/下缘（正文墨混入）→ None',
              ink_box_in_zone(_p1, (60, 200, 250, 700)) is None
              and ink_box_in_zone(_p1, (60, 200, 400, 450)) is None)
        _junk = _Img.new('L', (600, 800), 255)    # 07:09 假块复刻：稀碎墨 ~650px
        _jx = _junk.load()
        for _y in range(300, 340, 3):             # 上部零星点
            for _x in range(120, 320, 5):
                _jx[_x, _y] = 60
        for _x in range(100, 300, 5):             # 底部一条淡文本带（合计~2.2% 填充）
            for _y in range(560, 580):
                _jx[_x, _y] = 90
        _p3 = os.path.join(_td, 'c.png')
        _junk.save(_p3)
        check('F6) 稀碎墨假块（07:09 复刻：墨<min_ink 或填充率<2.5%）→ None',
              ink_box_in_zone(_p3, (60, 200, 400, 700)) is None)

        # clamp_zone：区带夹进截图（13:52 复盘：越界 crop 补黑边=假墨块）
        check('Z1) 区带夹取：范围内原样返回',
              clamp_zone((60, 712, 440, 940), 1936, 1056) == (60, 712, 440, 940))
        check('Z2) 区带夹取：越界 y1=1142 夹到 1056（13:52 现场）',
              clamp_zone((60, 712, 440, 1142), 1936, 1056) == (60, 712, 440, 1056))
        check('Z3) 区带夹取：夹完太扁/太窄 → None',
              clamp_zone((60, 1010, 440, 1200), 1936, 1056) is None
              and clamp_zone((60, 500, 130, 900), 1936, 1056) is None)

        # locate_pane_frame：边框窗格定位（14:13 定案：附图不进 a11y 树、
        # 心电式稀疏竖图墨量 101~376px 过不了墨块门槛 → 认站点边框骨架，
        # 内容无关不刻舟求剑）
        def _pane_img(fig=True, dense=False):
            """合成复刻 14:12 实测窗格：边框灰204 + 稀疏折线 + 文本列 +
            两条无纵线撑腰的假横带（页分隔线）。"""
            _p = _Img.new('L', (520, 820), 255)
            _q = _p.load()
            for _y in range(125, 359):            # 窗格左右竖框
                _q[106, _y] = 204
                _q[360, _y] = 204
            for _x in range(106, 361):            # 上下横框
                _q[_x, 125] = 204
                _q[_x, 358] = 204
            if dense:                             # 密文本内部（非图）
                for _x in range(120, 350):
                    for _y in range(140, 345):
                        _q[_x, _y] = 160
            elif fig:                             # 心电式稀疏折线
                for _x in range(200, 228):
                    for _d in range(0, 60, 3):
                        _q[_x, 170 + (_x - 200) % 40 + _d] = 160
            for _y in range(125, 359):            # 著录文本列框+碎字块
                _q[400, _y] = 204
            for _bx in range(420, 500, 14):
                for _x in range(_bx, _bx + 8):
                    for _y in range(150, 330, 6):
                        _q[_x, _y] = 60
            for _x in range(30, 500):             # 假横带：无贯穿纵线撑腰
                _q[_x, 500] = 204
                _q[_x, 700] = 204
            return _p

        _pf1 = os.path.join(_td, 'pane.png')
        _pane_img().save(_pf1)
        _fr = locate_pane_frame(_pf1, (0, 0, 520, 820), prefer_y=200)
        check('PF1) 窗格定位：框+稀疏折线+假横带 → 框矩形（±4px）',
              _fr is not None
              and all(abs(_fr['rect'][_k] - (106, 125, 360, 359)[_k]) <= 4
                      for _k in range(4)))
        _pf2a = os.path.join(_td, 'pane_empty.png')
        _pane_img(fig=False).save(_pf2a)
        check('PF2a) 窗格内部校验：空容器（无图可点）→ None',
              locate_pane_frame(_pf2a, (0, 0, 520, 820), prefer_y=200) is None)
        _pf2b = os.path.join(_td, 'pane_dense.png')
        _pane_img(dense=True).save(_pf2b)
        check('PF2b) 窗格内部校验：密文本（墨>上限/填充率爆表）→ None',
              locate_pane_frame(_pf2b, (0, 0, 520, 820), prefer_y=200) is None)
        _txtimg = _Img.new('L', (520, 820), 255)
        _tq = _txtimg.load()
        for _bx in range(40, 480, 16):            # 纯文字页：碎字+一条横带
            for _x in range(_bx, _bx + 9):
                for _y in range(100, 700, 5):
                    _tq[_x, _y] = 60
        for _x in range(0, 520):
            _tq[_x, 400] = 204
        _pf3 = os.path.join(_td, 'textonly.png')
        _txtimg.save(_pf3)
        check('PF3) 纯文字页（无贯穿边框）→ None',
              locate_pane_frame(_pf3, (0, 0, 520, 820)) is None)
        _real = os.path.join(ROOT, 'output', 'uia_runs',
                             'figure_20260902_141208', '03_no_figure.png')
        if os.path.isfile(_real):
            _r = locate_pane_frame(_real, (60, 242, 448, 1051), prefer_y=734)
            check('PF4) 实跑证据回放：14:12 失败现场定位出 y725-958 窗格',
                  _r is not None
                  and abs(_r['rect'][0] - 206) <= 6
                  and abs(_r['rect'][1] - 725) <= 6
                  and abs(_r['rect'][3] - 958) <= 6
                  and 390 <= _r['rect'][2] <= 460)
        else:
            print('[SKIP] PF4) 实跑证据缺 03_no_figure.png，跳过')

    print('[selftest] ' + ('OK' if not fails else f'FAIL {fails}'))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
