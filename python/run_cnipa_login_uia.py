# -*- coding: utf-8 -*-
"""CNIPA 自动登录 · UIA 版 runner（免 CDP 架构，2026-09-02）。

第一天 PW 版自动登录（cnipa_sso_login：账密填表 + verify 滑块自动通过 +
门户落地修正）的 UIA 移植——「大脑」照搬：
  - 滑块缺口识别：_ncc_gap_detect 块带 Sobel NCC 集成（2026-09-03 重写，
    替换首日 PW 版 gap_detection）；
  - 拖动轨迹：PW human_drag 的 30 步 ease-out + 停止期，已移植为
    cnipa_uia.human_drag / drag_hold_start / drag_held_to / drag_release；
  - 入口 URL：CAS SSO `sso.cponline/login?service=<cpquery>`（登录后 302
    直接回 cpquery，避开 tysf URL 落门户的坑）；门户兜底跳回同 PW。
只换「手脚」：DOM 选择器 → a11y 树检索；page.screenshot → 窗口截图裁剪；
page.mouse → SendInput。

凭证流程（docs/接口契约.md §登录）：
  - 输入带 username/password → 先存 account.ini 再登录（cnipa_creds）；
  - 输入不带 → 读 account.ini；没有 → NEED_CREDENTIALS（由 AI agent 向
    用户索要后经 cnipa_login 工具传入）；
  - 密码绝不写 payload.json / 契约 JSON / stdout。

运行（用户手工执行；前置：Chrome 以 --force-renderer-accessibility 启动）：
    python -X utf8 python/run_cnipa_login_uia.py                       # 读 account.ini 登录
    python -X utf8 python/run_cnipa_login_uia.py python/test_inputs/01b_login_uia.json
    python -X utf8 python/run_cnipa_login_uia.py --probe               # 只探测不动作
    python -X utf8 python/run_cnipa_login_uia.py --captcha-replay <run目录或png>
                                                                       # 离线校准滑块几何

产出：python/output/uia_runs/login_<ts>/ payload.json + 截图 + tree_flat.txt
自检（离线，不碰网站、不碰窗口；CV 定位项用合成随机照片自测，需 numpy/
cv2/PIL，缺失则跳过该项）：
    python -X utf8 python/run_cnipa_login_uia.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

import cnipa_contract
import cnipa_creds
import cnipa_uia as uia

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- URL 常量（与 cnipa_common.py 对拍；不 import 它——那是 PW 大模块） ------
SSO_URL = 'https://sso.cponline.cnipa.gov.cn'
CPQUERY_INDEX = 'https://cpquery.cponline.cnipa.gov.cn/chinesepatent/index'


def sso_login_url_for_cpquery() -> str:
    """CAS SSO 登录 URL：service 参数由 SSO 服务端处理，登录后 302 回 cpquery。

    第一天教训（PW 实证）：tysf 的 redirectUrl 系列 query 在 hash 路由前端
    不被读取，登录后落到 tysfjk 门户（错误站）；必须走本 CAS 入口。
    """
    from urllib.parse import quote
    return f'{SSO_URL}/login?service={quote(CPQUERY_INDEX, safe="")}'


# 登录页表单：字段识别关键词（Chrome a11y 用 placeholder 给 Edit 命名）
USERNAME_NAME_MARKS = ('手机号', '手机', '账号', '用户名', '证件号码', '证件')
PASSWORD_NAME_MARKS = ('密码',)
USERNAME_LABELS = ('手机号', '手机', '账号', '用户名', '登录账号', '证件号码')
PASSWORD_LABELS = ('密码', '登录密码')

LOGIN_BUTTON_MARKS = ('登 录', '登录', '立即登录', '登入')

# 滑块在场标记（a11y 文本；canvas 内容 a11y 通常看不到 → 截图 CV 兜底）
SLIDER_TEXT_MARKS = ('拖动滑块', '滑块', '完成拼图', '拼图', '向右拖动', '完成验证')

# 登录失败页错误文案 → AUTH_REJECTED
AUTH_ERROR_MARKS = ('密码错误', '账号或密码', '用户名或密码', '账号不存在',
                    '错误次数', '已被锁定', '登录失败')

SSO_UNSAFE_HINT = (
    '国知局安全异常(/unsafe)：请完全退出 Chrome，手动打开「常规检索」登录后再跑；'
    '勿连续自动重开 SSO。'
)


def sso_unsafe_visible(texts, title: str = '') -> bool:
    joined = ' '.join(texts or [])
    if '安全异常' in joined and '登录环境' in joined:
        return True
    if '登录环境安全等级' in joined:
        return True
    return '/unsafe' in (title or '').lower()


# 门户落地标记（登录成功但落错站 → 兜底 goto cpquery）
PORTAL_MARKS = ('统一门户', '门户首页', 'tysfjk', '返回系统', '账户管理', '偏好设置')

# 登录成功标记
LOGGED_IN_MARK = '欢迎您'
CPQUERY_TITLE_MARK = '中国及多国专利审查信息查询'

# cpquery 使用说明弹窗标记
USAGE_DIALOG_MARK = '使用说明'
USAGE_DIALOG_BTN = '确定'


# ---------------------------------------------------------------------------
# 纯函数（离线可测）
# ---------------------------------------------------------------------------

def classify_login_fields(edits) -> dict:
    """把 (name, rect, elem) 列表分出 username / password。

    规则（PW 选择器的 a11y 等价物）：
      - name 含手机/账号/用户名 → username（placeholder 会被 Chrome a11y
        用作 Edit 的 accessible name，等价 PW 的 input[placeholder*="手机"]）；
      - name 含密码 → password；
      - 都没命中时几何兜底：登录卡内纵向相邻两个 Edit，上=账号 下=密码。
    """
    named_u = [e for e in edits if any(m in (e[0] or '') for m in USERNAME_NAME_MARKS)]
    named_p = [e for e in edits if any(m in (e[0] or '') for m in PASSWORD_NAME_MARKS)
               and '确认' not in (e[0] or '')]
    out = {}
    if named_u:
        out['username'] = named_u[0]
    if named_p:
        out['password'] = named_p[0]
    if 'username' in out and 'password' in out:
        return out
    # 几何兜底：拿无名 Edit，按 y 排序取纵向最近的一对（差 40~120px、x 接近）
    anon = [e for e in edits if not (e[0] or '').strip()]
    anon.sort(key=lambda e: (e[1][1], e[1][0]))
    for i in range(len(anon) - 1):
        (n1, r1, _), (n2, r2, _) = anon[i], anon[i + 1]
        dy = r2[1] - r1[3]
        dx = abs((r2[0] + r2[2]) - (r1[0] + r1[2])) / 2
        if 0 <= dy <= 90 and dx <= 120 and (r2[2] - r2[0]) > 100:
            out.setdefault('username', anon[i])
            out.setdefault('password', anon[i + 1])
            break
    return out


def _login_edits_relaxed(win):
    """tysf 等：Edit 较窄时仍收录。"""
    out = []
    for el in uia.descendants(win, control_type='Edit'):
        n, r = uia._name(el), uia._rect(el)
        if r is None or (r[2] - r[0]) < 30 or (r[3] - r[1]) < 10:
            continue
        out.append((n or '', r, el))
    return out


def resolve_login_fields(win) -> tuple:
    """Edit 命名 / 无名对 / 失败返回 ({}, '')。"""
    for fn in (_login_edits, _login_edits_relaxed):
        fields = classify_login_fields(fn(win))
        if 'username' not in fields or 'password' not in fields:
            continue
        un = (fields['username'][0] or '').strip()
        pn = (fields['password'][0] or '').strip()
        if any(m in un for m in USERNAME_NAME_MARKS) and any(
                m in pn for m in PASSWORD_NAME_MARKS):
            return fields, 'named_edits'
        if not un and not pn:
            return fields, 'anon_edits'
    return {}, ''


def _type_login_via_labels(win, creds: dict, step) -> bool:
    """placeholder 只在 Text 节点（tysf）：点标签行右侧入焦后键入。"""
    wr = uia._rect(win) or (0, 0, 1920, 1080)
    rows = (
        ('username', USERNAME_NAME_MARKS + ('手机号/证件号码',), creds['username']),
        ('password', PASSWORD_NAME_MARKS + ('请输入密码',), creds['password']),
    )
    for tag, labels, text in rows:
        done = False
        for lab in labels:
            for t in uia.descendants(win, control_type='Text'):
                n = (uia._name(t) or '').strip()
                if lab not in n:
                    continue
                r = uia._rect(t)
                if not r or r[1] < 80:
                    continue
                cx = min(max(r[2] + 100, r[0] + 180), wr[2] - 40)
                cy = (r[1] + r[3]) // 2
                uia.human_click(win, cx, cy, settle=0.35)
                uia.safe_send_keys('^a', win=win, pause=0.02)
                uia.safe_send_keys('{DELETE}', win=win, pause=0.05)
                uia.safe_send_keys(str(text), win=win, pause=0.03,
                                   with_spaces=True, log=step)
                if step:
                    step('login_type', row=tag, via='label_focus', label=lab[:24])
                done = True
                break
            if done:
                break
        if not done:
            return False
        time.sleep(0.25)
    return True


def fill_login_credentials(win, creds: dict, step) -> tuple:
    """填账密：优先 Edit；无 Edit 时 tysf 标签行入焦键入。"""
    fields, kind = resolve_login_fields(win)
    if fields:
        uia.clear_and_type(win, fields['username'][2], creds['username'])
        uia.clear_and_type(win, fields['password'][2], creds['password'])
        if step:
            step('login_fill', via=kind)
        return True, kind
    if _type_login_via_labels(win, creds, step):
        if step:
            step('login_fill', via='label_focus')
        return True, 'label_focus'
    return False, 'no_fields'


def login_error_in_texts(texts) -> str:
    """页面可见的账密错误文案（有则返回原文，无则 ''）。最长优先——
    '账号或密码错误' 会同时命中 '密码错误' 与 '账号或密码'，取更具体者。"""
    joined = ' '.join(texts)
    for m in sorted(AUTH_ERROR_MARKS, key=len, reverse=True):
        if m in joined:
            return m
    return ''


def looks_logged_in_texts(texts) -> bool:
    return any(LOGGED_IN_MARK in t for t in texts)


def portal_landed_texts(texts) -> bool:
    joined = ' '.join(texts)
    return any(m in joined for m in PORTAL_MARKS) and CPQUERY_TITLE_MARK not in joined


def drag_target_x(handle_cx: float, gap_x_img: float, piece_x_img: float,
                  ratio: float) -> float:
    """计算拖动目标光标 x（屏幕逻辑像素）。

    PW 等价式：need = gap_css - piece_offset（都是面板内 CSS px）→
    target = handle_cx + need。UIA 版把「图像 px」统一乘 ratio
    （= 面板屏幕宽 / 面板截图宽，同时吃掉 DPI 缩放与截图缩放）。
    """
    need = (gap_x_img - piece_x_img) * ratio
    return handle_cx + need


def panel_ratio(panel_w_screen: float, panel_w_img: float) -> float:
    if panel_w_img <= 0:
        return 1.0
    return panel_w_screen / panel_w_img


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _finish(payload: dict, out_dir: str) -> None:
    payload['finished_at'] = datetime.now().isoformat()
    p = os.path.join(out_dir, 'payload.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print('[uia] payload => ' + p)
    cnipa_contract.maybe_emit('login', payload)


def _fg_is_win(win) -> bool:
    """前台窗口是否为 win 本身（HWND 级比较，防串窗键入）。"""
    import ctypes
    try:
        fg = ctypes.windll.user32.GetForegroundWindow()
        return bool(fg) and int(fg) == int(getattr(win, 'handle', 0))
    except Exception:
        return False


def _focus_guard(win, tries=3, wait=0.3) -> bool:
    """set_focus 后用前台 HWND 复核；未夺回则放弃键入。

    live 实测（142406）：set_focus 是异步尽力而为，用户在 0.3-0.8s 间隙
    点击聊天框即可抢走焦点，Ctrl+T/URL/ENTER 全部注入别的窗口。
    """
    for _ in range(tries):
        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(wait)
        if _fg_is_win(win):
            return True
    return _fg_is_win(win)


def _open_url_tab(win, url: str, step) -> bool:
    """Ctrl+T 新标签 → 地址栏键入 URL → Enter（figure runner 同款）。

    每批按键前做前台 HWND 复核；焦点不在本窗口立即中止（宁可不导航，
    也不把 URL 打进用户正在用的窗口）。返回是否完成导航。

    2026-09-08 审核修订：裸 send_keys 换 uia.safe_send_keys——SendInput
    偶发被系统瞬时阻断（inserted 0/N，如 UAC 弹层抢前台），安全版抢回
    前台+退避重试；本函数原有的「焦点丢失即中止」闸门语义保持不变。
    """
    if not _focus_guard(win):
        step('open_url_focus_lost', stage='pre')
        return False
    uia.safe_send_keys('^t', win=win, pause=0.05, log=step)
    time.sleep(0.8)
    if not _fg_is_win(win):
        step('open_url_focus_lost', stage='after_ctrl_t')
        return False
    uia.safe_send_keys(url, win=win, pause=0.03, with_spaces=True, log=step)
    time.sleep(0.3)
    if not _fg_is_win(win):
        step('open_url_focus_lost', stage='pre_enter')
        return False
    uia.safe_send_keys('{ENTER}', win=win, pause=0.05, log=step)
    time.sleep(1.5)
    step('open_url', url=url)
    return True


def _pick_window(step):
    """选交互窗口：优先登录页/cpquery，避免点到 IPSERVICE 等无关窗。"""
    LOGIN_TITLE_MARKS = ('统一身份', '身份认证', '登录', 'sso.cponline', 'tysf.cponline')
    titled = uia.find_chrome_windows()
    if titled:
        return titled[0], 'cpquery_titled'
    anys = uia.find_any_chrome()
    for w, info in anys:
        if info.get('minimized'):
            continue
        title = info.get('title') or ''
        if any(m in title for m in LOGIN_TITLE_MARKS):
            return w, 'login_titled:' + title[:40]
    for w, info in anys:
        if not info.get('minimized'):
            return w, 'any_chrome:' + (info.get('title') or '')[:40]
    if anys:
        return anys[0][0], 'any_chrome_minimized:' + (anys[0][1].get('title') or '')[:40]
    return None, ''


def _login_edits(win):
    """枚举登录页 Edit（name+rect+elem），供 classify_login_fields。"""
    out = []
    for el in uia.descendants(win, control_type='Edit'):
        n, r = uia._name(el), uia._rect(el)
        if r is None or (r[2] - r[0]) < 60 or (r[3] - r[1]) < 12:
            continue
        out.append((n or '', r, el))
    return out


def _click_login_button(win, step) -> bool:
    for mark in LOGIN_BUTTON_MARKS:
        cands = [b for b in uia.descendants(win, control_type='Button')
                 if mark in (uia._name(b) or '')]
        # 排除「注册」按钮的误命中（'登录'是'立即登录'子串，反向无碍）
        cands = [b for b in cands if '注册' not in (uia._name(b) or '')]
        if cands:
            uia.click(win, cands[0])
            step('click_login_button', btn=uia._name(cands[0]))
            return True
    return False


# --- 滑块几何定位：CV 优先、内容无关（2026-09-02 第四版） --------------------
# 回应「换一张照片怎么办」：
#   1) 定位认「结构特征」，不认具体像素：面板内是真实照片（边缘密集 +
#      色彩方差高），页面其余是平色块。任何照片内容都满足，尺寸用区间。
#   2) 缺口识别 = 块带 Sobel NCC 集成（_ncc_gap_detect，离线 6 例回归）；
#      拼图块宽由微拖帧差 bbox 实测（W = bbox_w − d），不写死比例。
#   3) 每次尝试重新截图重新定位——照片每次刷新都换，流程不依赖上次结果。
# 19:17 实测教训（第三版轮廓法报废原因）：整窗截图含窗框/浏览器栏/任务栏/
# 右滚动条，膨胀后焊成单个整窗轮廓（唯一候选 1929x1053），照片被吞。
# 第四版无轮廓：全宽横线切出页面内容带 → 尺寸网格积分图稠密搜索。

def _ncc_gap_detect(panel_gray, piece_w_est, min_gap_x,
                    out_dir=None, attempt=1, piece_band=None):
    """tianai 族滑块缺口检测 v2（2026-09-03 像素取证后重写，离线 4 例验证）。

    离线验证结论（manual GT=327 命中；130711 同实例；113955 try1=203 /
    try3=160，均经「微拖帧差分块带 + 缺口左缘竖缘能量」双证据核实）：
      1. 模板必须裁到「块 y 带」：全高模板把相关值稀释到 0.10-0.22
         （旧版三方法零票的根因）。块带来源优先级：微拖 bbox23 →
         左条带(x2:47)水平边缘峰对(间隔35-75) → 全带滑窗兜底。
      2. 块=照片×加亮、缺口=照片×加暗：结构相关在 Sobel(1,1) 域最强
         （0.52-0.93）；灰度域相关符号不稳定(-0.5~+0.6)不能当门禁；
         Canny 边缘太碎，均弃用。
      3. 缺口左缘是带内最强竖直边缘之一（z 4.5-8.1、rank 0-6/331），
         右缘渐变弱是正常特征 → 左缘 z≥2.0 作确认门禁。
      4. 搜索右界 pw-8：缺口右缘最远可达 pw-27，旧 max_gap_x=pw-30 会把
         答案整个排除在搜索窗外（manual 实例即中招）。

    流程：候选带×模板宽{pw_est-4, pw_est+1, pw_est+6} → Sobel(1,1) NCC
    → PSR=(峰-中位)/std 排序 → 以最高票为锚 35px 聚簇（≥3票）→
    簇内左缘 z 最高者≥2.0 确认 → PSR 加权均值。
    返回 (gap_x, detail_dict) 或 (None, detail_dict)。
    """
    import cv2
    import numpy as np
    g = np.asarray(panel_gray)
    if g.ndim == 3:
        g = g.mean(axis=2)
    g = g.astype(np.uint8)
    # 防御：剪掉底部混入的白轨道行（旧定位链裁剪超高时出现过）
    while g.shape[0] > 60 and float(g[-1, :].mean()) > 246:
        g = g[:-1]
    ph, pw = g.shape
    X0 = max(int(min_gap_x), 60)
    X1 = pw - 8
    detail = {'min_gap_x': X0, 'max_gap_x': X1, 'panel_size': (pw, ph),
              'piece_w_est': piece_w_est, 'piece_band_in': piece_band}
    if X1 - X0 < 40:
        detail['error'] = 'search_too_narrow'
        return None, detail

    def _sob(im):
        return np.clip(np.abs(cv2.Sobel(im, cv2.CV_32F, 1, 1, ksize=3)),
                       0, 255).astype(np.uint8)

    # --- 候选块带（按优先级） ---
    cand_bands = []
    if piece_band and piece_band[1] - piece_band[0] >= 25:
        for dy in (-4, 0, 4):
            y0 = max(0, int(piece_band[0]) + dy)
            y1 = min(ph, int(piece_band[1]) + dy)
            # 与入口门限一致（142406 try2 实测带高 27 行曾被 >=30 全灭，
            # 静默降级 edge_pair；实测带是唯一 ground truth，不应丢弃）
            if y1 - y0 >= 25:
                cand_bands.append((y0, y1, 'diff_bbox'))
    if not cand_bands:
        strip = g[:, 2:47]
        ey = np.abs(cv2.Sobel(strip, cv2.CV_32F, 0, 1, ksize=3)).sum(axis=1)
        ethr = float(ey.mean() + ey.std())
        peaks = [y for y in range(4, ph - 4)
                 if ey[y] >= ethr and ey[y] >= float(ey[max(0, y-3):y+4].max())]
        pairs = [(a, b) for i, a in enumerate(peaks)
                 for b in peaks[i+1:] if 35 <= b - a <= 75]
        for a, b in pairs[:6]:
            for dy in (-3, 0, 3):
                y0, y1 = max(0, a + dy), min(ph, b + dy)
                if y1 - y0 >= 30:
                    cand_bands.append((y0, y1, 'edge_pair'))
        detail['edge_peaks'] = peaks
        detail['edge_pairs'] = pairs[:6]
    if not cand_bands:
        cand_bands = [(y, min(ph, y + 56), 'slide')
                      for y in range(10, ph - 40, 6)]

    def _zwin(exz, gx, lo=-3, hi=7):
        """窗口探针：在 [gx+lo, gx+hi) 找最强竖缘的 (z, 对齐x)。

        模板裁自 band[:, 1:1+tw]，块静止左缘可能位于 x>1（142406 try2
        实测垫 3px），NCC 峰 gap_x 系统性偏左 1-3px；在峰点取 z 会落在
        缘的缓坡上（实测 z=1.85 而物理缘 z=6.3 → 门禁误杀正确簇）。
        窗口取峰既修门禁，也对齐到物理左缘。"""
        a = max(0, gx + lo - X0)
        b = min(len(exz), gx + hi - X0)
        if b <= a:
            return 0.0, gx
        seg = exz[a:b]
        j = int(np.argmax(seg))
        return float(seg[j]), a + j + X0

    tws = sorted({max(40, int(piece_w_est) - 4), int(piece_w_est) + 1,
                  int(piece_w_est) + 6})
    votes = []
    for (y0, y1, src) in cand_bands:
        band = g[y0:y1, :]
        ex = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0,
                              ksize=3)).sum(axis=0)
        exz = (ex[X0:X1] - float(np.median(ex[X0:X1]))) / \
              (float(ex[X0:X1].std()) or 1e-6)
        for tw in tws:
            if X0 + tw >= X1:
                continue
            tmpl = band[:, 1:1 + tw]
            if float(tmpl.std()) < 5:
                continue
            T = _sob(tmpl)
            B = _sob(band)
            r = cv2.matchTemplate(B[:, X0:X1], T, cv2.TM_CCOEFF_NORMED)
            _, v, _, loc = cv2.minMaxLoc(r)
            gx = loc[0] + X0
            psr = (float(v) - float(np.median(r))) / (float(r.std()) or 1e-6)
            zv, zx = _zwin(exz, gx)
            votes.append({'psr': round(psr, 2), 'val': round(float(v), 3),
                          'gap_x': int(gx), 'band': (y0, y1), 'src': src,
                          'tw': tw, 'z_left': round(zv, 2), 'zx': int(zx)})
    if not votes:
        detail['error'] = 'no_votes'
        return None, detail
    votes.sort(key=lambda t: -t['psr'])
    detail['top5'] = votes[:5]

    anchor = votes[0]['gap_x']
    cluster = [t for t in votes if abs(t['gap_x'] - anchor) <= 35]
    detail['cluster_n'] = len(cluster)
    if len(cluster) < 3:
        detail['error'] = 'cluster_thin'
        return None, detail
    zbest = max(cluster, key=lambda t: t['z_left'])
    detail['z_left_best'] = zbest['z_left']
    if zbest['z_left'] < 2.0:
        detail['error'] = 'weak_left_edge'
        return None, detail
    wsum = sum(t['psr'] for t in cluster)
    mean_x = int(round(sum(t['gap_x'] * t['psr'] for t in cluster) / wsum))
    # 吸附到簇锚带内 [mean-3, mean+7) 最强竖缘，消除模板左垫片系统性
    # 偏左（gap_x 语义 = 块左缘目标位置，与 piece_left_now 同轴对齐）
    a0, a1 = cluster[0]['band']
    band = g[a0:a1, :]
    ex = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=0)
    sa, sb = max(0, mean_x - 3), min(pw, mean_x + 7)
    gap_x = int(sa + np.argmax(ex[sa:sb])) if sb > sa else mean_x
    detail['agreement'] = {
        'gap_x': gap_x, 'n': len(cluster),
        'val_mean': round(float(np.mean([t['val'] for t in cluster])), 3),
        'psr_mean': round(float(np.mean([t['psr'] for t in cluster])), 2),
        'band': cluster[0]['band'], 'band_src': cluster[0]['src'],
        'snap': (mean_x, gap_x)}

    # 诊断图：2x 放大 + 红框标注
    if out_dir:
        try:
            vis = np.repeat(np.repeat(g, 2, 0), 2, 1)
            vis = cv2.cvtColor(vis, cv2.COLOR_GRAY2BGR)
            cv2.rectangle(vis, (gap_x * 2, 0),
                          ((gap_x + int(piece_w_est)) * 2, vis.shape[0] - 1),
                          (0, 0, 255), 2)
            cv2.imwrite(os.path.join(out_dir, f'ncc_gap_try{attempt}.png'), vis)
        except Exception:
            pass
    return gap_x, detail


def _dump_a11y(win, out_dir, name):
    """落一份 a11y 可见文本，供离线校准/举证（canvas 滑块在树里不可见）。"""
    try:
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
            f.write('\n'.join(uia.all_texts(win)))
    except Exception:
        pass


def _find_photo_rect(img, scale=1.0, min_y=0, debug=False):
    """在窗口截图(PIL)中找「照片矩形」= 滑块面板。图像 px（±8px 精度）。

    返回 ((x, y, w, h), score) 或 (None, 0.0)。纯函数（可离线/合成图自测）。
    第四版（无轮廓，免疫「整窗粘连」）：
      1) 内容带提取：行边缘数 ≥0.7W 的全宽横线 = 窗框/浏览器栏底/任务栏顶，
         取相邻线最大空档为页面内容带（兜底：顶 6px + 底 8% 剔除）；
      2) 1/4 分辨率密度图上按面板尺寸网格（w 200-560·s，h 70-240·s，
         宽高比 1.5-6）做积分图稠密搜索，密度最高者胜（须 ≥0.05；
         照片区实测 8-25%，滚动条/窗框竖线在最小窗内 <4%）；
         score = 密度 + 0.4·min(sat_std,90)/90（照片色彩方差加分）。
    """
    import cv2
    import numpy as np
    arr = np.asarray(img.convert('RGB'))
    H, W = arr.shape[:2]
    if H < 300 or W < 500:
        return None, 0.0
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 100, 200)

    rowsum = edges.sum(axis=1) / 255.0
    busy = np.where(rowsum >= 0.7 * W)[0]
    y_lo, y_hi = 6, H - int(0.08 * H) - 6     # 兜底：去窗顶边 + 底 8%（任务栏）
    lines = []
    if len(busy):
        s, p = int(busy[0]), int(busy[0])
        for y in busy[1:]:
            if y - p <= 3:
                p = int(y)
            else:
                lines.append((s + p) // 2)
                s = p = int(y)
        lines.append((s + p) // 2)
    if len(lines) >= 2:
        gaps = [(lines[i + 1] - lines[i], lines[i], lines[i + 1])
                for i in range(len(lines) - 1)]
        gap, a, b = max(gaps)
        if gap >= 0.4 * H:
            y_lo, y_hi = max(6, a + 4), min(H - 6, b - 4)
    y_lo = max(y_lo, min_y)
    if y_hi - y_lo < int(90 * scale):
        return None, 0.0
    if debug:
        print('  [dbg] lines=%r → 内容带 y[%d,%d] (min_y=%d)'
              % (lines, y_lo, y_hi, min_y))

    q = 4                                       # 1/4 分辨率（密度不变，快 ~16x）
    es = edges[:H // q * q, :W // q * q].reshape(
        H // q, q, W // q, q).mean(axis=(1, 3)) / 255.0   # 归一化到 [0,1]
    ii = cv2.integral(es)
    if isinstance(ii, tuple):
        ii = ii[0]
    lo4, hi4 = y_lo // q, y_hi // q
    sub = ii[lo4:hi4 + 1, :]
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    best = None                                 # (density, x, y, w, h)
    dmax, dmax_at = 0.0, None                   # debug：floor 之前的全局最大密度
    for w4 in range(max(10, int(200 * scale) // q),
                    int(560 * scale) // q + 1, 7):
        for h4 in range(max(6, int(70 * scale) // q),
                        int(240 * scale) // q + 1, 6):
            if not 1.5 <= w4 / float(h4) <= 6.0:
                continue
            if sub.shape[0] <= h4 or es.shape[1] <= w4:
                continue
            D = (sub[h4:, w4:] - sub[:-h4, w4:]
                 - sub[h4:, :-w4] + sub[:-h4, :-w4]) / float(w4 * h4)
            iy, ix = np.unravel_index(int(np.argmax(D)), D.shape)
            d = float(D[iy, ix])
            if d > dmax:
                dmax = d
                dmax_at = (int(ix) * q, (lo4 + int(iy)) * q, w4 * q, h4 * q)
            if d < 0.05:
                continue
            x, y = int(ix) * q, (lo4 + int(iy)) * q
            if best is None or d > best[0]:
                best = (d, x, y, w4 * q, h4 * q)
    if best is None:
        if debug:
            print('  [dbg] 全尺寸最大密度 %.4f @ %r（< 0.05 → None）'
                  % (dmax, dmax_at))
        return None, 0.0
    if debug:
        print('  [dbg] best density=%.4f rect=(%d,%d,%d,%d)'
              % (best[0], best[1], best[2], best[3], best[4]))

    # —— 种子外扩：均匀纹理里稠密搜索天然偏向最小窗（小窗不被页面平色
    # 稀释、密度恒高），需从最优种子向四边外扩、直到「新增条带」不像照片
    # 为止，还原整块照片（±1 格 ≈ 4px 精度）。
    # 条带判定 = 双通道（20:05 真截图实测）：照片内条带边缘密度 0.019~0.046
    # / 页面 0.005~0.010，但照片平色区（天空等）边缘会失灵；色彩通道照片
    # sat_std≈68 / 页面≈0~10，分离度更大 → 边缘密度 ≥0.015 或 饱和度方差
    # ≥25，任一命中即算照片条带。
    x4, y4 = best[1] // q, best[2] // q
    w4s, h4s = best[3] // q, best[4] // q
    cap_w4, cap_h4 = int(700 * scale) // q, int(300 * scale) // q

    def _band_d(bx, by, bw, bh):        # es 坐标条带密度（积分图）
        s = (ii[by + bh, bx + bw] - ii[by, bx + bw]
             - ii[by + bh, bx] + ii[by, bx])
        return s / float(bw * bh)

    def _band_sat(bx, by, bw, bh):      # 条带饱和度方差（img px）
        return float(hsv[by * q:(by + bh) * q,
                         bx * q:(bx + bw) * q, 1].std())

    def _photo_band(bx, by, bw, bh):    # 双通道：边缘 或 色彩，任一即照片
        return (_band_d(bx, by, bw, bh) >= 0.015
                or _band_sat(bx, by, bw, bh) >= 25.0)

    grown = True
    while grown:
        grown = False
        if (w4s < cap_w4 and x4 + w4s < es.shape[1]
                and _photo_band(x4 + w4s, y4, 1, h4s)):
            w4s += 1
            grown = True
        if (w4s < cap_w4 and x4 > 0
                and _photo_band(x4 - 1, y4, 1, h4s)):
            x4 -= 1
            w4s += 1
            grown = True
        if (h4s < cap_h4 and y4 + h4s < hi4
                and _photo_band(x4, y4 + h4s, w4s, 1)):
            h4s += 1
            grown = True
        if (h4s < cap_h4 and y4 > lo4
                and _photo_band(x4, y4 - 1, w4s, 1)):
            y4 -= 1
            h4s += 1
            grown = True
    x, y, w, h = x4 * q, y4 * q, w4s * q, h4s * q
    d = _band_d(x4, y4, w4s, h4s)
    if debug:
        def _bs(bx, by, vert):          # 停扩边带双通道值 (d, sat)
            return (round(_band_d(bx, by, 1, h4s if vert else 1), 4),
                    round(_band_sat(bx, by, 1, h4s if vert else 1), 1))
        bands = {
            '右': _bs(x4 + w4s, y4, True) if x4 + w4s < es.shape[1] else None,
            '左': _bs(x4 - 1, y4, True) if x4 > 0 else None,
            '下': (_bs(x4, y4 + h4s, False) if y4 + h4s < hi4 else None),
            '上': _bs(x4, y4 - 1, False) if y4 > lo4 else None,
        }
        print('  [dbg] 外扩后 density=%.4f rect=(%d,%d,%d,%d) '
              '门槛(边≥0.015 或 sat≥25) 停扩边带(d,sat)=%r'
              % (d, x, y, w, h, bands))
    sat_std = float(hsv[y:y + h, x:x + w, 1].std())
    return (x, y, w, h), d + 0.4 * min(sat_std, 90.0) / 90.0


def _find_track_below(img, panel, scale=1.0):
    """面板正下方等宽滑轨：上下边框是两条长横线。图像 px，找不到给几何兜底。"""
    import cv2
    import numpy as np
    arr = np.asarray(img.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    x, y, w, h = panel
    y0 = y + h
    y1 = min(y0 + int(80 * scale), gray.shape[0])
    if y1 - y0 < int(10 * scale) or x + w > gray.shape[1]:
        return None
    strip = edges[y0:y1, x:x + w]
    rowsum = strip.sum(axis=1) / 255.0
    rows = np.where(rowsum >= 0.55 * w)[0]
    if len(rows) >= 2:
        top, bot = int(rows.min()), int(rows.max())
        if int(8 * scale) <= (bot - top) <= int(70 * scale):
            return (int(x), y0 + top, int(x + w), y0 + bot)
    ty = y0 + int(6 * scale)                     # 几何兜底（tianai 版式）
    return (int(x), ty, int(x + w), ty + int(44 * scale))


def _panel_above_track(img, track, scale=1.0, debug=False):
    """滑条正上方的照片矩形（图像 px，返回 x,y,w,h；找不到 → None）。

    白缝锚点法（2026-09-03 改版）：照片顶上方、对话框顶内边是连续纯白
    行带（manual 实测 y384-413 全 1.000 白占比），照片行自身不会整行
    >246；旧版「行方差 std<8 从底向上找第一个低方差行」会被照片内部
    均匀带（天空）截断（manual 实测误停 y550，面板被裁成 68px 高）。
    照片底边紧贴滑条顶边（tysf 版式），故 photo_bottom = 滑条顶 - 2px。
    x 向取滑轨边界（右侧白条由 _trim_panel_x 裁掉；白占比统计排除
    右侧 30px 白条与左端 10px 圆角）。
    """
    import numpy as np
    if not track or track[2] - track[0] < 40:
        return None
    x0, y_top_track, x1, y1_track = track
    track_w = x1 - x0
    y_bot = y_top_track - max(2, int(round(2 * scale)))
    if y_bot < 20:
        return None
    arr = np.asarray(img.convert('L'))
    scan_top = max(0, y_bot - 500)
    xa, xb = x0 + 10, max(x0 + 11, x1 - 30)        # 排除左圆角与右白条
    col = arr[scan_top:y_bot, xa:xb]
    if col.size == 0:
        return None
    white = (col > 246).mean(axis=1) > 0.95
    # 取最下方的连续白行带（≥8 行；「最长」会误选对话框上方的页面背景，
    # 130711 实测页面背景带 36 行 > 对话框内缝隙 30 行）；照片顶 = 带末 + 1
    best_end, best_len, i = -1, 0, 0
    n = len(white)
    while i < n:
        if white[i]:
            j = i
            while j + 1 < n and white[j + 1]:
                j += 1
            if j - i + 1 >= 8:
                best_len, best_end = j - i + 1, j
            i = j + 1
        else:
            i += 1
    if best_end < 0:
        if debug:
            print('  [dbg] panel: no white gap (best %d rows)' % best_len)
        return None
    photo_top = scan_top + best_end + 1
    h = y_bot - photo_top
    if debug:
        print('  [dbg] 白缝锚点法 photo=(%d,%d,%d,%d) gap_rows=%d'
              % (x0, photo_top, track_w, h, best_len))
    if h < 60 or h > 400:
        return None
    return (x0, photo_top, track_w, h)


def _real_track_from_text(img, trect, debug=False):
    """从 a11y 文本元素矩形（图像 px）推导真实滑轨矩形（图像 px）。

    2026-09-03 手工截图实锤（idle + 手工拖到缺口两张对照）：
      - 文本「向右滑动完成验证」的 a11y rect 仅 ~113px 宽，水平居中于
        ~400px 白色滑轨；直接把文本 rect 当轨道用，落点会按在文字上
        （130711 run：knob=(1169,647) 落在第一个字上，3 次空按 diff=0）。
      - 轨内纯白 255、轨外对话框灰底 221-244；左缘与文字之间有两个浅
        障碍：">"箭头暗簇（~4-6px, gray 55-174）与旋钮盒右边框（~2px,
        196-248）——用「持续非白 ≥10px」判缘可跳过它们（真缘灰底 ≥30px
        持续）；右缘无障碍。
      - 带高实测 ~39-40px（= 旋钮 40x40）。
    方法：文本中心行、从文本两缘（外扩 8px 防 rect 偏小压到字形）向外
    扫描；再从中心行上下扩行（净白区间避开左端旋钮与中部文字）定上下缘。
    返回 (x0, y0, x1, y1)；失败 None。
    """
    import numpy as np
    arr = np.asarray(img.convert('L')).astype(np.int16)
    h, w = arr.shape
    tx0, ty0, tx1, ty1 = [int(round(v)) for v in trect]
    if tx1 - tx0 < 40 or ty1 - ty0 < 8:
        return None
    cy = (ty0 + ty1) // 2
    WHITE, SUSTAIN = 246, 10

    def _edge(x0, step, lim):
        """从 x0 向 step 方向扫，返回真缘 x（持续非白段首）。"""
        run, run_start, x = 0, None, x0
        while 0 < x < lim:
            if arr[cy, x] > WHITE:
                run = 0
            else:
                run += 1
                if run == 1:
                    run_start = x
                if run >= SUSTAIN:
                    return run_start
            x += step
        return None

    left_e = _edge(max(1, tx0 - 8), -1, w - 1)     # ① 左缘（跳过箭头/盒框）
    right_e = _edge(min(w - 2, tx1 + 8), 1, w - 1)  # ② 右缘
    if left_e is None or right_e is None:
        if debug:
            print('  [dbg] real_track no edge L=%r R=%r' % (left_e, right_e))
        return None
    left, right = left_e + 1, right_e - 1
    if not (240 <= right - left <= 700):           # tianai 轨宽实测 ~400
        if debug:
            print('  [dbg] real_track bad width L=%d R=%d' % (left, right))
        return None
    # ③ 上下缘：净白区间 = 旋钮区右侧↔文字左缘 ∪ 文字右缘↔轨右内侧
    spans = [(left + 55, max(left + 56, tx0 - 5)),
             (min(right - 1, tx1 + 5), right - 55)]
    n_px = sum(b - a for a, b in spans if b > a)
    if n_px < 60:
        return None

    def _row_white(y):
        cnt = 0
        for a, b in spans:
            if b > a:
                cnt += int((arr[y, a:b] > WHITE).sum())
        return cnt >= 0.9 * n_px

    if not _row_white(cy):
        return None
    top = cy
    while top > 0 and _row_white(top - 1):
        top -= 1
    bot = cy
    while bot < h - 1 and _row_white(bot + 1):
        bot += 1
    if not (25 <= bot - top <= 70):                # 带高实测 ~39
        if debug:
            print('  [dbg] real_track bad band h=%d' % (bot - top))
        return None
    return (left, top, right, bot)


def _find_knob_in_track(img, track, scale=1.0):
    """滑轨左端旋钮（tianai 初始位）：轨道左 60px 内找 ">" 箭头暗簇质心。

    2026-09-03 像素实锤：idle 态箭头 gray≈55，位于 track_left+~22（manual
    与 run 截图同构，位置仅差窗口偏移）；提示文字 gray≥99 且在
    track_left+~143 之后，不会混入左 60px 搜索窗。找不到暗簇时几何兜底
    (track_left+20, 带中心) —— 旋钮 40x40 贴轨道左缘。
    返回 (cx, cy) 图像 px。
    """
    import numpy as np
    arr = np.asarray(img.convert('L'))
    x0, y0, x1, y1 = track
    xs, xe = max(0, x0 + 2), min(arr.shape[1] - 1, x0 + 60)
    ys, ye = max(0, y0 + 2), max(1, y1 - 2)
    if xe > xs and ye > ys:
        strip = arr[ys:ye, xs:xe]
        dark = strip < 100
        if int(dark.sum()) >= 3:
            dyc, dxc = np.nonzero(dark)
            return (int(xs + dxc.mean()), int(ys + dyc.mean()))
    return (int(x0 + 20), int((y0 + y1) // 2))


def _locate_captcha(win, out_dir, step=None, save_png='captcha_probe.png'):
    """截图 + CV 定位滑块三件套（面板/滑轨/旋钮，图像 px）。找不到返回 None。

    2026-09-03 改版：a11y 文本 rect 只是锚点，真实滑轨由
    _real_track_from_text 扩出（~400px 白轨与文本同心）；此前直接把
    113px 文本 rect 当轨道，旋钮落点按在文字上 → 3 次空按 diff=0。
    """
    png = os.path.join(out_dir, save_png)
    uia.screenshot_window(win, png)
    from PIL import Image
    img = Image.open(png)
    win_r = uia._rect(win)
    if not win_r:
        if step:
            step('captcha_cv_no_window_rect')
        return None
    scale = img.width / float(win_r[2] - win_r[0])
    # ① a11y 锚点（滑条文本元素）→ ② 扩出真实白轨 → ③ 轨上方找照片
    bar = _slider_bar_rect(win)
    track = None
    if bar:
        trect = (int(round((bar[0] - win_r[0]) * scale)),
                 int(round((bar[1] - win_r[1]) * scale)),
                 int(round((bar[2] - win_r[0]) * scale)),
                 int(round((bar[3] - win_r[1]) * scale)))
        track = _real_track_from_text(img, trect)
    if not track:
        if step:
            step('captcha_anchor_no_track', shot=save_png,
                 bar=(list(bar) if bar else None), cands=_SLIDER_CANDS)
        return None
    panel = _panel_above_track(img, track, scale)
    if not panel:
        if step:
            step('captcha_anchor_no_panel', shot=save_png,
                 bar=(list(bar) if bar else None), cands=_SLIDER_CANDS)
        return None
    # 面板 x 取滑轨全宽：照片右侧 ~16px 灰填充带（237 平坦）无害，
    # 列方差裁剪实测会误伤照片大片平滑区（130711 裁成 177/399）→ 不裁，
    # NCC 搜索右界由 max_gap_x 排除右缘即可。
    knob = _find_knob_in_track(img, track, scale)
    if step:
        step('captcha_anchor_located', shot=save_png,
             bar=[bar[0], bar[1], bar[2] - bar[0], bar[3] - bar[1]],
             track_img=list(track), panel_img=list(panel),
             knob=(list(knob) if knob else None))
    try:                                          # 人类可读标注图（校准用）
        import cv2
        import numpy as np
        dbg = np.asarray(img.convert('RGB')).copy()
        cv2.rectangle(dbg, (panel[0], panel[1]),
                      (panel[0] + panel[2], panel[1] + panel[3]), (0, 220, 0), 3)
        if track:
            cv2.rectangle(dbg, (track[0], track[1]), (track[2], track[3]),
                          (255, 160, 0), 2)
        if knob:
            cv2.circle(dbg, knob, 6, (255, 0, 0), 2)
        cv2.imwrite(os.path.join(out_dir, 'captcha_located.png'),
                    cv2.cvtColor(dbg, cv2.COLOR_RGB2BGR))
    except Exception:
        pass
    return {'panel_img': panel, 'track_img': track, 'knob_img': knob,
            'win_r': win_r, 'scale': scale, 'shot': png}


def _find_real_track_x(img, track, scale=1.0, debug=False):
    """从 a11y 锚点出发，用面板区列方差扫描找真实轨道/面板 X 边界。

    a11y「向右滑动完成验证」文本元素通常只有 100-120px 宽，但真实面板
    宽 350-500px。照片内容有高列方差（std≥12），白底/边框方差低。
    扫描面板区（轨道上方 ~200px）而非轨道本身（太窄、含文字干扰方差）。
    返回修正后的 (x0, y0, x1, y1) 图像 px；找不到 → 原 track。
    """
    import numpy as np
    if not track:
        return track
    x0_a11y, y0, x1_a11y, y1 = track
    anchor_cx = (x0_a11y + x1_a11y) // 2
    arr = np.asarray(img.convert('L'), dtype=np.float32)
    H, W = arr.shape

    # 扫描面板内容区域（照片区），而非仅轨道上方——面板照片列方差高
    # （std≥12），页面背景/白缝方差低。从中部向下扫到轨道附近覆盖面板主体。
    panel_scan_h = int(200 * scale)
    scan_y_mid = max(0, y0 - int(80 * scale))   # 面板中下部（靠近滑条）
    scan_y_lo = max(0, scan_y_mid - panel_scan_h)
    scan_y_hi = min(arr.shape[0], y0 + int(10 * scale))  # 延伸到轨道区域
    scan_band = arr[scan_y_lo:scan_y_hi, :]
    if scan_band.size == 0 or scan_band.shape[0] < 20:
        return track
    col_std = scan_band.std(axis=0)

    min_w = int(200 * scale)
    search_lo = max(0, anchor_cx - int(350 * scale))
    search_hi = min(W, anchor_cx + int(350 * scale))

    left = anchor_cx
    while left > search_lo and col_std[left] >= 12:
        left -= 1
    left += 1

    right = anchor_cx
    while right < search_hi - 1 and col_std[right] >= 12:
        right += 1
    right -= 1

    if right - left < min_w:
        if debug:
            print('  [dbg] 列方差未找到宽面板, width=%d < %d' %
                  (right - left, min_w))
        return track

    y_bot = min(H, y1 + int(6 * scale))
    for y in range(y1, min(H, y1 + int(30 * scale))):
        row = arr[y, left:right]
        if row.mean() > 235 and row.std() < 8:
            y_bot = y
            break

    if debug:
        print('  [dbg] 列方差面板修正: (%d,%d,%d,%d) → (%d,%d,%d,%d)'
              % (x0_a11y, y0, x1_a11y, y1, left, y0, right, y_bot))
    return (left, y0, right, y_bot)


def _extract_piece_from_diff(panel_arr_s1, panel_arr_s2, piece_x_range=(0, 120)):
    """从面板双截图 diff 提取拼图块 bbox（面板相对坐标）。

    几何推导（块宽 W 高 H，帧间右移 d）：
      diff = 左强带[P0, P0+d](旧位露背景) + 中间弱带[P0+d, P0+W]
      (两帧均被块覆盖，diff=α·|tex(x)-tex(x-d)|) + 右强带[P0+W, P0+W+d]。
      ⇒ 无论中间弱带过不过阈值（blob 融合或分裂成两条宽 d 的带），
        属于块的连通域【并集】恒为 [P0, P0+W+d]×[P0y, P0y+H]。
      ⇒ 必须取并集，不能取最大单域（分裂时只剩一条宽 d 的带）。

    实跑噪声模型（2026-09-03 payload 证据：bbox 高达面板底、宽 ~75px）：
      a) 全高噪声条/域（面板边缘渲染抖动）——判别：高度 > 60% 面板 → 弃。
      b) 面板裁剪带入的底部滑轨/旋钮区域——旋钮与块【同步右移 d】，
         位置/位移都无法与块区分，唯一判别器是 y 波段：旋钮在面板底部
         （y 波段与照片区内块的 y 波段不重叠）。
      ⇒ 按 y 波段分组：同组条件 = y 范围重叠 ≥ 60%（块分裂出的左右带
        y 范围完全一致，必然同组；底部旋钮域不会进组）。取总面积最大组
        （块 H≈46-60 × (W+d) 面积 >> 旋钮 ~20 × (w_k+d)）。
      c) 4-连通代替 8-连通：对角粘连的噪声不与块并域。

    返回 (x0, y0, x1, y1) 面板相对坐标；找不到 → None。
    """
    import numpy as np
    cv2 = __import__('cv2')
    panel_h, panel_w = panel_arr_s1.shape[0], panel_arr_s1.shape[1]
    diff = np.abs(panel_arr_s2.astype(np.int16) - panel_arr_s1.astype(np.int16))
    if len(diff.shape) == 3:
        diff = diff.mean(axis=2)
    gray = diff.astype(np.uint8)
    # 自适应阈值：Otsu + 最小阈值 15 兜底（浅色块场景 diff 可能只有 10-20）
    otsu_thresh, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresh = max(15, min(otsu_thresh, 60))  # 限制在 [15, 60] 避免极端值
    _, bw = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    x_lo, x_hi = piece_x_range
    bw[:, :max(0, x_lo)] = 0
    bw[:, min(bw.shape[1], x_hi):] = 0

    # 连通域（4-连通），过滤：面积 < 40 的斑点 / 高度 > 60% 面板的全高噪声
    num_labels, _labels, stats, _c = cv2.connectedComponentsWithStats(
        bw, connectivity=4)
    comps = []
    for lbl in range(1, num_labels):
        lx, ly, lw, lh, la = stats[lbl]
        if la < 40 or lh > panel_h * 0.6:
            continue
        comps.append((lx, ly, lx + lw, ly + lh, int(la)))
    if not comps:
        return None

    # y 波段分组（并查集式合并：y 重叠 ≥ 较小组高的 60%）
    groups = []
    for comp in comps:
        placed = False
        for g in groups:
            y_ov = min(comp[3], g['y1']) - max(comp[1], g['y0'])
            y_min_h = min(comp[3] - comp[1], g['y1'] - g['y0'])
            if y_min_h > 0 and y_ov >= 0.6 * y_min_h:
                g['x0'] = min(g['x0'], comp[0]); g['y0'] = min(g['y0'], comp[1])
                g['x1'] = max(g['x1'], comp[2]); g['y1'] = max(g['y1'], comp[3])
                g['area'] += comp[4]
                placed = True
                break
        if not placed:
            groups.append({'x0': comp[0], 'y0': comp[1], 'x1': comp[2],
                           'y1': comp[3], 'area': comp[4]})

    # 取总面积最大组（块组 >> 旋钮组/其它噪声组）
    best = max(groups, key=lambda g: g['area'])
    x0, y0, x1, y1 = best['x0'], best['y0'], best['x1'], best['y1']

    # 合理性：高度不得接近面板高，宽度不得接近面板宽
    if (y1 - y0) > panel_h * 0.6 or (x1 - x0) > panel_w * 0.9:
        return None
    return (int(x0), int(y0), int(x1), int(y1))


def _profile_shift_subpixel(diff_a, diff_b, x_win, y_win, max_shift=60):
    """列投影互相关 + 抛物线亚像素插值，测 diff_b 相对 diff_a 的右移量。

    前提（由两次【等距】微拖保证）：diff12 与 diff23 是同一形状右移 d，
    列投影 p_b(x) ≈ p_a(x - d)。互相关峰即 d；抛物线插值把精度提到
    亚像素级（±0.3px 典型）——bbox 宽度差法只有 ±2-4px，不足以把
    scale 标定误差压到 2% 以内（远缺口落点误差会放大到几十 px）。

    diff_a/diff_b: 灰度 diff 数组（面板坐标）。
    x_win: (x_lo, x_hi) 参与相关的窗口（bbox 并集外扩）。
    y_win: (y_lo, y_hi) 行窗口——只取块的 y 波段，排除底部旋钮域对
           列投影的污染（旋钮虽同步位移会加强信号，但为稳妥仍排除）。
    返回亚像素 shift（float）；失败 → None。
    """
    import numpy as np
    def _profile(d):
        g = d.astype(np.float32)
        if g.ndim == 3:
            g = g.mean(axis=2)
        return g[y_win[0]:y_win[1], :].sum(axis=0)   # 每列 diff 能量和

    pa = _profile(diff_a)[x_win[0]:x_win[1]]
    pb_full = _profile(diff_b)
    if pa.size < 10:
        return None
    pa0 = float(pa.mean())
    if pa0 <= 0:
        return None
    scores = []
    for k in range(2, max_shift + 1):
        xb0, xb1 = x_win[0] + k, x_win[1] + k
        if xb1 > pb_full.size:
            break
        pb = pb_full[xb0:xb1]
        # 均值归一 SAD（抵抗整体亮度漂移）
        sad = float(np.abs(pa - pb - (pb.mean() - pa0)).sum())
        scores.append((sad, k))
    if len(scores) < 3:
        return None
    scores.sort()
    best_sad, best_k = scores[0]
    # 抛物线亚像素：需要 best_k-1 与 best_k+1 的分数
    s_km1 = next((s for s, k in scores if k == best_k - 1), None)
    s_kp1 = next((s for s, k in scores if k == best_k + 1), None)
    if s_km1 is None or s_kp1 is None:
        return float(best_k)
    denom = s_km1 - 2 * best_sad + s_kp1
    if abs(denom) < 1e-6:
        return float(best_k)
    offset = 0.5 * (s_km1 - s_kp1) / denom
    if abs(offset) > 1.0:               # 插值发散 → 退回整数
        return float(best_k)
    return best_k + offset


def _mask_piece_in_image(img_arr, piece_bbox):
    """遮蔽拼图块区域：用邻域列平移填充（消除 slide_match 自匹配）。

    优先取右侧邻域；若块接近右缘导致源区与目标区重叠，则改用左侧邻域。
    """
    import numpy as np
    x0, y0, x1, y1 = piece_bbox
    arr = img_arr.copy()
    pw = x1 - x0
    img_w = arr.shape[1]
    # 优先右侧
    src_x0 = x1
    src_x1 = src_x0 + pw
    if src_x1 > img_w:
        # 右侧不够 → 改用左侧
        src_x1 = x0
        src_x0 = src_x1 - pw
    if src_x0 < 0:
        return arr  # 面板太窄，无法遮蔽
    arr[y0:y1, x0:x1] = arr[y0:y1, src_x0:src_x1]
    return arr


def _solve_slider_once(win, out_dir, step, attempt=1):
    """一次完整尝试：截图→定位→微拖标定→NCC 缺口检测→精确拖拽。

    流水线（2026-09-03 NCC 版，替换首日 diff+blob 原型）：
      1. S1 截图 → CV 定位面板/滑轨/旋钮（a11y 锚点 + 行方差扫描）
      2. 按住旋钮 → 微拖 +15/+15 屏px → S2 → S3（全程按住）
      3. diff12/diff23 → 亚像素互相关 → scale 标定（屏px/图px）
      4. diff bbox → piece_w 估计（W = bbox_w − d，无 W≈H 假设）
      5. S1 面板灰度 → 块带裁剪 Sobel NCC 集成（PSR 排序+左缘 z 门禁）→ gap_x
      6. 缺口左缘对齐：target_x = handle_cx + micro_drag + (gap_x − piece_x) × scale

    改进（vs 首日 diff+blob 原型）：
      - 砍掉 _find_real_track_x（列方差 std≥12 在亮照片上脆弱）
      - 砍掉 _extract_piece_from_diff 做缺口模板（Otsu+blob 常误选旋钮区）
      - 砍掉外部 ONNX slide_match 引擎（对 tianai 族 captcha 不稳定）
      - _ncc_gap_detect v2：块带（bbox23→边缘峰对→滑窗）×Sobel(1,1) NCC
        ×PSR 聚簇，缺口左缘竖缘 z≥2.0 确认（离线 4 例像素级验证）

    任何测量环节不合格 → 松手放弃本轮（无静默回退，外层刷新重试）。
    """
    MICRO_DRAG_PX = 15

    geom = _locate_captcha(win, out_dir, step, save_png=f'slider_try{attempt}.png')
    if not geom:
        return None

    from PIL import Image
    import cv2
    import numpy as np

    img = Image.open(geom['shot'])
    px, py, pw, ph = geom['panel_img']
    px, py = max(0, px), max(0, py)
    pw = min(pw, img.width - px)
    ph = min(ph, img.height - py)
    if pw < 100 or ph < 30:
        step('slider_crop_bad', pw=pw, ph=ph)
        return None

    def _fail(reason, **kw):
        uia.drag_release(win, settle=0.5)
        step(reason, **kw)
        return None

    def _panel_strip(shot_path):
        im = Image.open(shot_path)
        return np.asarray(im.crop((px, py, px + pw, py + ph)).convert('RGB'))

    # S1: 面板裁剪（微拖前基准帧）
    strip_s1 = img.crop((px, py, px + pw, py + ph))
    strip_s1.save(os.path.join(out_dir, f'captcha_strip_s1_try{attempt}.png'))
    arr_s1 = np.asarray(strip_s1.convert('RGB'))

    # --- 微拖：按住旋钮 → +15 → S2 → +15 → S3（全程按住） -----------------
    win_r, scale = geom['win_r'], geom['scale']
    knob = geom.get('knob_img')
    track = geom.get('track_img')
    if knob:
        handle_cx = win_r[0] + knob[0] / scale
        handle_cy = win_r[1] + knob[1] / scale
    elif track:
        handle_cx = win_r[0] + (track[0] + max(12, (track[3] - track[1]) // 2)) / scale
        handle_cy = win_r[1] + (track[1] + track[3]) / 2.0 / scale
    else:
        step('slider_no_handle_pos')
        return None

    uia.drag_hold_start(win, int(handle_cx), int(handle_cy))
    uia.drag_held_to(win, int(handle_cx + MICRO_DRAG_PX), int(handle_cy),
                     steps=10)
    time.sleep(0.4)
    s2_path = os.path.join(out_dir, f'captcha_strip_s2_try{attempt}.png')
    uia.screenshot_window(win, s2_path)
    arr_s2 = _panel_strip(s2_path)

    uia.drag_held_to(win, int(handle_cx + 2 * MICRO_DRAG_PX), int(handle_cy),
                     steps=10)
    time.sleep(0.4)
    s3_path = os.path.join(out_dir, f'captcha_strip_s3_try{attempt}.png')
    uia.screenshot_window(win, s3_path)
    arr_s3 = _panel_strip(s3_path)

    # --- diff → 亚像素 d → scale 标定（保留已验证的互相关方法） -----------
    diff12 = np.abs(arr_s2.astype(np.int16) - arr_s1.astype(np.int16))
    diff23 = np.abs(arr_s3.astype(np.int16) - arr_s2.astype(np.int16))
    diff13 = np.abs(arr_s3.astype(np.int16) - arr_s1.astype(np.int16))
    try:
        Image.fromarray(np.clip(diff13.mean(axis=2), 0, 255).astype(np.uint8)
                        ).save(os.path.join(out_dir,
                                            f'captcha_diff_try{attempt}.png'))
    except Exception:
        pass

    diff13_gray = diff13.mean(axis=2) if diff13.ndim == 3 else diff13
    diff_max = int(diff13_gray.max())
    if diff_max < 20:
        step('slider_light_piece', diff_max=diff_max,
             hint='浅色块场景，diff 幅度不足，建议刷新重试')

    bbox12 = _extract_piece_from_diff(arr_s1, arr_s2, piece_x_range=(0, 140))
    bbox23 = _extract_piece_from_diff(arr_s2, arr_s3, piece_x_range=(0, 150))
    if bbox12 is None or bbox23 is None:
        return _fail('slider_diff_no_piece',
                     bbox12=list(bbox12) if bbox12 else None,
                     bbox23=list(bbox23) if bbox23 else None,
                     diff_max=diff_max)

    w12 = bbox12[2] - bbox12[0]
    w23 = bbox23[2] - bbox23[0]
    if abs(bbox23[1] - bbox12[1]) > 4 or abs(w23 - w12) > 5:
        return _fail('slider_diff_inconsistent', bbox12=list(bbox12),
                     bbox23=list(bbox23))

    x_win = (max(0, min(bbox12[0], bbox23[0]) - 6),
             min(pw, max(bbox12[2], bbox23[2]) + 6))
    y_win = (max(0, bbox12[1] - 2), min(ph, bbox12[3] + 2))
    d_sub = _profile_shift_subpixel(diff12, diff23, x_win, y_win, max_shift=60)
    d_bbox = bbox23[0] - bbox12[0]
    if d_sub is None or not (5 <= d_sub <= 45):
        return _fail('slider_calib_bad_shift', d_sub=d_sub, d_bbox=int(d_bbox))
    if abs(d_sub - d_bbox) > 5:
        return _fail('slider_calib_mismatch', d_sub=round(d_sub, 2),
                     d_bbox=int(d_bbox))

    calibrated_scale = MICRO_DRAG_PX / d_sub
    piece_w = w12 - d_sub
    if not (15 <= piece_w <= 150):
        return _fail('slider_calib_bad_w', piece_w=round(piece_w, 1),
                     w12=w12, d=round(d_sub, 2))

    piece_left_now = bbox23[0] + d_sub
    step('slider_micro_drag',
         bbox12=list(bbox12), bbox23=list(bbox23),
         d_sub=round(d_sub, 2), d_bbox=int(d_bbox),
         piece_w=round(piece_w, 1), piece_left_now=round(piece_left_now, 1),
         calibrated_scale=round(calibrated_scale, 3))

    # --- NCC ensemble 缺口检测 ---------------------------------------------
    # piece_band 直接用微拖帧差分块盒（bbox23 的 y 范围）——比边缘峰对稳。
    panel_gray = cv2.cvtColor(arr_s1, cv2.COLOR_RGB2GRAY)
    min_gap_x = max(int(piece_left_now + piece_w * 0.5),
                    int(piece_w * 1.5), 60)
    gap_x, ncc_detail = _ncc_gap_detect(
        panel_gray, int(piece_w), min_gap_x, out_dir, attempt,
        piece_band=(bbox23[1], bbox23[3]))

    # 保存 piece 裁剪供事后取证（--captcha-replay 诊断用）
    piece_rect = (int(round(piece_left_now)),
                  max(0, bbox23[1]),
                  int(round(piece_left_now + piece_w)),
                  min(ph, bbox23[3]))
    try:
        piece_img = Image.fromarray(arr_s1).crop(
            (max(0, piece_rect[0]), piece_rect[1],
             min(pw, piece_rect[2]), piece_rect[3]))
        piece_img.save(os.path.join(out_dir, f'captcha_piece_try{attempt}.png'))
    except Exception:
        pass

    if gap_x is None:
        # 设计原则：无串行降级兜底（2026-09-03 复审定案）。外部引擎在
        # live 唯一一次兜底即 +50 脱靶（142406 try2：NCC 簇 297 正确却被
        # 旧 z 门禁误拒 → 兜底 348 → 拖错）。主路径都不信的答案，交给
        # 它只会把「无害拒绝」变成「自信的错拖」。松手 → 外层刷新换新
        # 验证码 → NCC 重新主攻，期望值严格更高。
        return _fail('slider_gap_not_found',
                     min_gap_x=min_gap_x, ncc=ncc_detail)
    step('slider_gap_found_ncc', gap_x=gap_x, ncc=ncc_detail)

    # --- 精确拖拽 --------------------------------------------------------
    needed_img = gap_x - piece_left_now
    needed_screen = needed_img * calibrated_scale
    target_x = handle_cx + 2 * MICRO_DRAG_PX + needed_screen
    step('slider_drag',
         handle=(int(handle_cx), int(handle_cy)),
         target_x=round(target_x, 1),
         needed_img=round(needed_img, 1),
         needed_screen=round(needed_screen, 1))
    uia.drag_held_to(win, int(target_x), int(handle_cy), steps=25)
    uia.drag_release(win, settle=1.2)
    return geom


def _refresh_slider(win, geom, step):
    """失败重试前点面板右上角刷新钮（tianai 家族惯例位；校准点，点偏无害）。"""
    if not geom:
        step('slider_refresh_skip')
        return
    win_r, scale = geom['win_r'], geom['scale']
    x, y, w, h = geom['panel_img']
    sx = win_r[0] + (x + w) / scale - 14
    sy = win_r[1] + y / scale + 10
    uia.human_click(win, int(sx), int(sy), settle=1.0)
    step('slider_refresh_click')
    time.sleep(1.5)


_SLIDER_CANDS = []       # 最近一次「滑动」候选矩形（屏px），锚点失败时 step 打印


def _slider_bar_rect(win):
    """滑条矩形（屏px）：name 含「滑动」的 a11y 文本元素。18:50 实跑
    captcha_tree.txt 佐证：「向右滑动完成验证」在照片 canvas 渲染前 ~20s
    就在树里（文本元素与 canvas 不同、a11y 可见），其矩形≈滑条本身。
    找不到或尺寸不像滑条（120-700 × 8-90 屏px）→ None。
    2026-09-02：升级为锚点主入口，全部候选记入 _SLIDER_CANDS 供排查——
    18:58 复盘它「从未触发」是因为密度法每次都假成功，轮不到它。"""
    global _SLIDER_CANDS
    _SLIDER_CANDS = []
    for el in uia.descendants(win, name_contains='滑动'):
        r = uia._rect(el)
        if not r:
            continue
        _SLIDER_CANDS.append(list(r))
        w, h = r[2] - r[0], r[3] - r[1]
        if 40 <= w <= 700 and 8 <= h <= 90:
            return r
    return None


def _handle_captcha(win, out_dir, step, max_attempts=6):
    """PW handle_cnipa_verify_captcha 的 UIA 移植：出现→识别→拖→验→刷新重试。

    实跑教训（17:38 / 18:50 两条）：
      1) canvas 滑块 a11y 树 n=0，出现判定只能靠 文本标记 或 CV 命中；
      2) 「向右滑动完成验证」文字点登录后立刻出现，照片 canvas ~20s 后才
         渲染——文字出现 ≠ 图片就绪；抢跑会把尝试次数烧在空白滑条上
         （18:50 run：3 张 slider_try 截图字节级相同即页面静止）。
    故拆两段：出现等待（30s，文本或 CV）→ 面板就绪等待（60s，仅 CV，
    no_panel 不烧尝试次数；首轮顺带点一下滑条中心，覆盖「点击/悬停展开」
    型控件，点轨道对拖拽型无害）。
    """
    texts0 = uia.all_texts(win)
    if not any(m in ' '.join(texts0) for m in SLIDER_TEXT_MARKS):
        # 无滑块文案时只短等：多数登录无验证码，原先 30s 空转像死机
        t0 = time.time()
        appear_budget = 8.0
        while time.time() - t0 < appear_budget:
            if _locate_captcha(win, out_dir, step, save_png='captcha_probe.png'):
                step('captcha_appeared_by_cv',
                     waited_s=round(time.time() - t0, 1))
                break
            if any(m in ' '.join(uia.all_texts(win)) for m in SLIDER_TEXT_MARKS):
                step('captcha_appeared_by_text',
                     waited_s=round(time.time() - t0, 1))
                break
            time.sleep(1.5)
        else:
            uia.screenshot_window(win, os.path.join(out_dir, 'captcha_missing.png'))
            _dump_a11y(win, out_dir, 'captcha_tree.txt')
            step('captcha_not_appeared')
            return True                          # 没出现滑块 ≈ 无需验证（PW 同义）
    _dump_a11y(win, out_dir, 'captcha_tree.txt')

    geom = None
    t1 = time.time()
    nudged = False
    while time.time() - t1 < 45.0:
        geom = _locate_captcha(win, out_dir, None, save_png='captcha_ready.png')
        if geom:
            step('captcha_panel_ready', waited_s=round(time.time() - t1, 1))
            break
        bar = _slider_bar_rect(win)
        if bar and not nudged:
            cx, cy = (bar[0] + bar[2]) // 2, (bar[1] + bar[3]) // 2
            uia.human_click(win, cx, cy, settle=0.8)   # 唤醒滑条（点轨道，无害）
            step('captcha_bar_nudge',
                 bar=[bar[0], bar[1], bar[2] - bar[0], bar[3] - bar[1]])
            nudged = True
        time.sleep(2.5)
    if not geom:
        step('captcha_panel_never', waited_s=round(time.time() - t1, 1))
        uia.screenshot_window(win, os.path.join(out_dir, 'captcha_missing.png'))
        return False                             # 滑条在但照片 60s 未渲染

    last_geom = geom
    for attempt in range(1, max_attempts + 1):
        step('captcha_attempt', n=attempt)
        geom = _solve_slider_once(win, out_dir, step, attempt)
        if geom:
            last_geom = geom
            time.sleep(1.5)                      # 等验证结果渲染
            texts = uia.all_texts(win)
            err = login_error_in_texts(texts)
            if err:
                step('captcha_ok_but_auth_error', err=err)
                return True                      # 滑块过了，账密问题交上层
            if looks_logged_in_texts(texts):
                step('captcha_passed')
                return True
            # canvas 滑块无文本反馈 → 照片矩形消失即通过
            after = _locate_captcha(win, out_dir, None,
                                    save_png=f'captcha_after{attempt}.png')
            if after is None:
                step('captcha_passed_panel_gone')
                return True
            step('captcha_still_there')
            last_geom = after
            # 拖动失败后站点已自动换图，勿再点刷新钮（否则连换两图）
            time.sleep(1.2)
            continue
        _refresh_slider(win, last_geom, step)
        time.sleep(1.0)
    step('captcha_exhausted')
    return False


def _wait_post_login(win, step, timeout_s=40.0):
    """等登录生效：欢迎您 / cpquery 标题；门户落地则兜底 goto cpquery。"""
    t0 = time.time()
    portal_saved = False
    while time.time() - t0 < timeout_s:
        time.sleep(2.0)
        texts = uia.all_texts(win)
        if looks_logged_in_texts(texts):
            return 'logged_in'
        err = login_error_in_texts(texts)
        if err:
            return 'auth_error:' + err
        title = win.window_text() or ''
        if CPQUERY_TITLE_MARK in title and '申请号' in ' '.join(texts):
            return 'cpquery_form'
        if not portal_saved and portal_landed_texts(texts):
            step('portal_landed_rescue')
            _open_url_tab(win, CPQUERY_INDEX, step)   # 第一天 PW 同款兜底
            portal_saved = True
    # 门户已落地 = SSO 已接受登录（live 142406：登录实际成功，只因兜底
    # 键入串窗导致后续探测超时，被误报 fail 10）
    return 'portal_landed' if portal_saved else 'timeout'


def _dismiss_usage_dialog(win, step, timeout_s=12.0, absent_giveup_s=3.5):
    return uia.dismiss_usage_dialog(
        win, step, timeout_s=timeout_s, absent_giveup_s=absent_giveup_s)


def selftest() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 1) SSO URL 构造
    url = sso_login_url_for_cpquery()
    check(url.startswith(SSO_URL + '/login?service='), 'SSO URL 前缀不符: ' + url)
    check('cpquery.cponline.cnipa.gov.cn' in url, 'SSO service 未指向 cpquery')
    check(url.endswith('%2Fchinesepatent%2Findex') or 'index' in url, 'service 未含 index')

    # 2) 表单字段分类（命名命中）
    edits = [('请输入手机号', (100, 200, 300, 224), 'e1'),
             ('请输入密码', (100, 240, 300, 264), 'e2'),
             ('', (700, 50, 800, 70), 'omnibox')]
    got = classify_login_fields(edits)
    check(got.get('username', (None,))[2] == 'e1', 'username 未按 name 命中')
    check(got.get('password', (None,))[2] == 'e2', 'password 未按 name 命中')

    # 3) 表单字段分类（几何兜底：无名成对 Edit）
    anon = [('', (200, 300, 480, 322), 'a1'), ('', (200, 350, 480, 372), 'a2'),
            ('', (900, 100, 980, 120), 'far')]
    got2 = classify_login_fields(anon)
    check(got2.get('username', (None,))[2] == 'a1', '几何兜底 username 不符: %r' % got2)
    check(got2.get('password', (None,))[2] == 'a2', '几何兜底 password 不符: %r' % got2)

    # 4) 页面错误文案 / 登录态 / 门户判定
    check(login_error_in_texts(['提示', '账号或密码错误，请重试']) == '账号或密码', '错误文案未识别')
    check(login_error_in_texts(['欢迎您，张三']) == '', '正常页误报错误')
    check(looks_logged_in_texts(['欢迎您，张三']) is True, '欢迎您未识别为已登录')
    check(portal_landed_texts(['统一门户 首页']) is True, '门户落地未识别')
    check(portal_landed_texts(['中国及多国专利审查信息查询', '申请号']) is False,
          'cpquery 页误判为门户')
    # 4b) 门户页实际文本检测（tysfjk 门户：返回系统/账户管理/偏好设置）
    check(portal_landed_texts(['返回系统', '您好', '账户管理', '证书管理', '偏好设置'])
          is True, '门户页(返回系统+账户管理)未识别')
    check(portal_landed_texts(['偏好设置', '证书管理', '返回系统'])
          is True, '门户页(偏好设置)未识别')

    # 5) 拖动数学：need = (gap - piece) * ratio；target = handle + need
    tx = drag_target_x(handle_cx=500, gap_x_img=240.0, piece_x_img=0.0, ratio=1.25)
    check(abs(tx - 800.0) < 1e-6, 'drag_target_x 不符: %r' % tx)
    check(abs(panel_ratio(320, 400) - 0.8) < 1e-9, 'panel_ratio 不符')

    # 6) 凭证流：无凭证 → None；保存后 → 读回（临时目录隔离）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ini = os.path.join(td, 'account.ini')
        os.environ['CNIPA_ACCOUNT_INI'] = ini
        try:
            check(not cnipa_creds.has_creds(ini), '空凭证应 has_creds=False')
            cnipa_creds.save_creds('13800001234', 'pw123', 'natural', ini)
            c = cnipa_creds.load_creds(ini)
            check(c and c['username'] == '13800001234' and c['password'] == 'pw123',
                  '凭证往返不符')
            check(cnipa_creds.mask_username('13800001234') == '138****34', '脱敏不符')
        finally:
            del os.environ['CNIPA_ACCOUNT_INI']

    # 7) 契约层：login 工具白名单（clean_login 在 cnipa_contract 注册）
    os.environ['CNIPA_EMIT_JSON'] = '1'
    out = cnipa_contract.maybe_emit('login', {
        'ok': True, 'logged_in': True, 'already': False,
        'account_configured': True,
        'username_masked': '138****34',
        'password': 'SHOULD-NEVER-LEAK', 'steps': ['x'], 'run_dir': '/tmp',
    })
    check(out is not None and out.get('success') is True, 'login 契约未发射成功结构')
    check('password' not in json.dumps(out), 'login 契约泄漏密码字段')
    check('steps' not in json.dumps(out) and 'run_dir' not in json.dumps(out),
          'login 契约泄漏遥测')

    # 8) NEED_CREDENTIALS 失败结构
    out2 = cnipa_contract.maybe_emit('login', {
        'ok': False, 'error': '未配置凭证：account.ini 缺少账号密码'})
    check(out2.get('error') == 'NEED_CREDENTIALS', '错误码应为 NEED_CREDENTIALS: %r' % out2)
    check('hint' in out2, 'NEED_CREDENTIALS 应带 hint')
    del os.environ['CNIPA_EMIT_JSON']

    # 9) 锚点法照片定位（合成图、离线）：滑条正上方随机照片×3 检出 +
    #    白底负例不误检。2026-09-02 20:33 用户截图实锤：全局密度法在真实
    #    页面命中的是左侧表单/插图区，改 a11y 滑条锚点 + 行方差向上扫描。
    try:
        import numpy as np
        from PIL import Image
        rng = np.random.default_rng(20260902)

        def synth_anchor(photo=None):
            """700x800 合成：白底 + 滑条灰条(100,510)-(500,550) + 其上照片。
            照片底边贴滑条顶（tysf 版式），返回 (arr, gold_rect|None)。"""
            arr = np.full((700, 800), 255, np.uint8)
            arr[510:550, 100:500] = 120
            if photo is None:
                return arr, None
            ph_, pw_ = photo.shape[:2]
            arr[510 - ph_:510, 100:100 + pw_] = photo
            return arr, (100, 510 - ph_, pw_, ph_)

        for trial in range(3):
            photo = rng.integers(0, 255, (160 + 40 * trial, 400, 3),
                                 dtype=np.uint8)
            photo = photo.mean(axis=2).astype(np.uint8)
            arr, gold = synth_anchor(photo)
            rect = _panel_above_track(Image.fromarray(arr),
                                      (100, 510, 500, 550))
            check(rect is not None, '锚点定位失败 trial%d：照片未检出' % trial)
            if rect:
                check(abs(rect[0] - gold[0]) <= 4
                      and abs(rect[2] - gold[2]) <= 4
                      and abs((rect[1] + rect[3]) - (gold[1] + gold[3])) <= 6,
                      '锚点定位偏差过大 trial%d: rect=%r gold=%r'
                      % (trial, rect, gold))
        flat, _ = synth_anchor(None)
        r0 = _panel_above_track(Image.fromarray(flat), (100, 510, 500, 550))
        check(r0 is None, '白底负例误检出照片: %r' % (r0,))
        print('  [selftest] 锚点法照片定位: 滑条上方随机照片×3 检出 + 白底负例不误检')
    except ImportError:
        print('  [selftest] numpy/PIL 不可用，跳过锚点定位测试')
    except Exception as e:
        import traceback
        traceback.print_exc()
        failures.append('锚点定位测试异常: %r' % e)

    # 10) 微拖差分 + 两次等距微拖标定：合成复现实跑噪声场景
    #     场景：半透明块(W=60≠H=46, α=0.7) 三帧位移 0/+15/+15；
    #     底部滑轨带入的旋钮噪声带(与块同步位移)；左缘全高渲染噪声条。
    #     验证：y 波段分组滤噪 / 并集 bbox / 亚像素 d / W 不依赖方形假设。
    try:
        import numpy as np
        rng2 = np.random.default_rng(42)
        H_, W_, D_ = 46, 60, 15           # 块高/块宽/每步位移（真值）
        PY, PX0 = 80, 8                   # 块初始 y / x
        bg = rng2.integers(100, 200, (180, 400, 3), dtype=np.uint8)
        tex = rng2.integers(50, 150, (H_, W_, 3)).astype(np.float32)
        alpha = 0.7

        def _frame(piece_x, knob_x, noise_seed):
            f = bg.copy().astype(np.float32)
            reg = f[PY:PY + H_, piece_x:piece_x + W_]
            f[PY:PY + H_, piece_x:piece_x + W_] = (
                alpha * tex + (1 - alpha) * reg)
            # 旋钮噪声带（面板底部，与块同步位移 d）
            f[160:176, knob_x:knob_x + 20] = 40.0
            # 左缘全高渲染噪声条（两帧各自随机抖动）
            rng_n = np.random.default_rng(noise_seed)
            f[:, 0:2] = np.clip(f[:, 0:2]
                                + rng_n.integers(-40, 40, (180, 2, 3)), 0, 255)
            return f.astype(np.uint8)

        s1 = _frame(PX0, 20, 1)           # 块x=8,  旋钮x=20
        s2 = _frame(PX0 + D_, 35, 2)      # 块x=23, 旋钮x=35
        s3 = _frame(PX0 + 2 * D_, 50, 3)  # 块x=38, 旋钮x=50

        bbox12 = _extract_piece_from_diff(s1, s2, piece_x_range=(0, 140))
        bbox23 = _extract_piece_from_diff(s2, s3, piece_x_range=(0, 150))
        check(bbox12 is not None, 'diff12 未检出（噪声压制失败）')
        check(bbox23 is not None, 'diff23 未检出（噪声压制失败）')
        if bbox12 and bbox23:
            # 真值：bbox12=(8,80,83,126) bbox23=(23,80,98,126)
            check(abs(bbox12[0] - 8) <= 2 and abs(bbox12[1] - 80) <= 2
                  and abs(bbox12[2] - 83) <= 2 and abs(bbox12[3] - 126) <= 2,
                  'bbox12 偏差过大: %r (期望 ~(8,80,83,126))' % (bbox12,))
            check(abs(bbox23[0] - 23) <= 2 and abs(bbox23[2] - 98) <= 2,
                  'bbox23 偏差过大: %r (期望 ~(23,80,98,126))' % (bbox23,))
            check(bbox12[3] < 150,
                  'y 波段分组未滤除底部旋钮噪声: %r' % (bbox12,))
        # 亚像素 d（列投影互相关）
        diff12 = np.abs(s2.astype(np.int16) - s1.astype(np.int16))
        diff23 = np.abs(s3.astype(np.int16) - s2.astype(np.int16))
        x_win = (max(0, min(bbox12[0], bbox23[0]) - 6),
                 max(bbox12[2], bbox23[2]) + 6)
        y_win = (max(0, bbox12[1] - 2), bbox12[3] + 2)
        d_sub = _profile_shift_subpixel(diff12, diff23, x_win, y_win)
        check(d_sub is not None, '亚像素位移测量失败')
        if d_sub is not None:
            check(abs(d_sub - D_) <= 0.5,
                  'd_sub 精度不足: %.2f (期望 %d±0.5)' % (d_sub, D_))
        if bbox12 and d_sub is not None:
            w12 = bbox12[2] - bbox12[0]
            piece_w = w12 - d_sub
            check(abs(piece_w - W_) <= 2,
                  'piece_w 恢复失败: %.1f (期望 %d±2)' % (piece_w, W_))
            piece_left_now = bbox23[0] + d_sub
            check(abs(piece_left_now - (PX0 + 2 * D_)) <= 1.5,
                  '当前块左缘错误: %.1f (期望 %d)' % (piece_left_now,
                                                       PX0 + 2 * D_))
            # 演示旧 W≈H 法的固有误差（本例 29 vs 15，近 2 倍）
            print('  [selftest] 两次微拖标定: d=%.2f(真值%d) piece_w=%.1f(真值%d)'
                  ' 当前左缘=%.1f(真值%d) | 旧W≈H法 disp=%d(误差%.0f%%)'
                  % (d_sub, D_, piece_w, W_, piece_left_now,
                     PX0 + 2 * D_, w12 - bbox12[3] + bbox12[1],
                     abs((w12 - (bbox12[3] - bbox12[1])) - D_) / D_ * 100))

        # 11) 遮蔽函数：验证遮蔽区域被填充
        masked = _mask_piece_in_image(s2, (26, 80, 92, 126))
        check(not np.array_equal(masked[80:126, 26:92], s2[80:126, 26:92]),
              '遮蔽未生效：区域未变化')
        check(np.array_equal(masked[80:126, 26:92], s2[80:126, 92:158]),
              '遮蔽内容不符：应为右侧邻域')
        print('  [selftest] 遮蔽函数: 区域被右侧邻域填充 ✓')

        # 12) 旋钮定位（暗簇法）：白底轨道 + 左侧暗块
        track_img = np.full((40, 400, 3), 240, dtype=np.uint8)
        track_img[5:35, 8:28] = 60
        knob_result = _find_knob_in_track(
            Image.fromarray(track_img), (0, 0, 400, 40))
        check(knob_result is not None, '旋钮定位返回 None')
        if knob_result:
            check(abs(knob_result[0] - 18) <= 6,
                  '旋钮 cx 偏差过大: %r (期望 ~18)' % (knob_result,))
        print('  [selftest] 旋钮暗簇定位: %r' % (knob_result,))

        # 13) 旋钮几何兜底：无暗簇时返回轨道左端 +6% 偏移
        plain_track = np.full((40, 400, 3), 240, dtype=np.uint8)
        knob_fallback = _find_knob_in_track(
            Image.fromarray(plain_track), (0, 0, 400, 40))
        check(knob_fallback is not None, '旋钮兜底返回 None')
        if knob_fallback:
            expected_x = max(15, int(0.06 * 400))  # = 24
            check(abs(knob_fallback[0] - expected_x) <= 6,
                  '旋钮兜底 cx 偏差: %r (期望 ~%d)' % (knob_fallback, expected_x))
            check(knob_fallback[1] == 20,
                  '旋钮兜底 cy 不符: %r' % (knob_fallback,))
        print('  [selftest] 旋钮几何兜底: %r' % (knob_fallback,))

        # 14) _find_real_track_x：面板列方差扫描修正窄 a11y 锚点
        # 合成 800x700 图：白底 + 照片面板(200,100,600,300) + 滑条(200,410,600,440)
        big = np.full((700, 800), 250, dtype=np.uint8)
        photo = rng2.integers(30, 220, (300, 400), dtype=np.uint8)
        big[100:400, 200:600] = photo
        big[410:440, 200:600] = 180  # 滑条灰带
        # 模拟 a11y 锚点：只覆盖滑条中部一小段（文本元素）
        narrow_anchor = (340, 410, 460, 440)  # 宽 120px，远窄于真实面板 400px
        corrected = _find_real_track_x(Image.fromarray(big), narrow_anchor,
                                       scale=1.0, debug=True)
        check(corrected is not None, '_find_real_track_x 返回 None')
        if corrected:
            cw = corrected[2] - corrected[0]
            check(cw >= 300,
                  '_find_real_track_x 未扩展宽度: %d (期望≥300, 原始=%d)'
                  % (cw, narrow_anchor[2] - narrow_anchor[0]))
            check(corrected[0] <= 220,
                  '_find_real_track_x 左边界偏差: %d (期望≤220)' % corrected[0])
            check(corrected[2] >= 580,
                  '_find_real_track_x 右边界偏差: %d (期望≥580)' % corrected[2])
        print('  [selftest] _find_real_track_x: 窄锚点 %r → 修正 %r'
              % (narrow_anchor, corrected))
    except ImportError:
        print('  [selftest] numpy/PIL 不可用，跳过微拖差分/遮蔽/旋钮测试')
    except Exception as e:
        import traceback
        traceback.print_exc()
        failures.append('微拖差分/遮蔽/旋钮测试异常: %r' % e)

    if failures:
        print('SELFTEST FAIL (%d)' % len(failures))
        for x in failures:
            print(' -', x)
        return 1
    print('SELFTEST OK')
    return 0


def _captcha_replay(target: str, scale_arg: str = None,
                    track_arg: str = None) -> int:
    """离线校准：对 run 目录/截图重跑「照片矩形定位 + 缺口识别」（不点不拖）。

    scale（图px/屏px）可显式给；缺省按窗口截图宽度/1096 估。目录模式取
    最新的 slider_try*.png / captcha_*.png。track_arg 给「x0,y0,x1,y1」
    （图像px角点，来自真跑日志 captcha_anchor_located 的 track_img）→ 用
    锚点法裁照片；缺省密度法兜底（已被实测否定，仅考古）。
    """
    if os.path.isdir(target):
        names = sorted(f for f in os.listdir(target) if f.endswith('.png'))
        prefer = [f for f in names if f.startswith('slider_try')] or \
                 [f for f in names if f.startswith('captcha_')] or names
        print('[replay] 目录内截图: ' + ', '.join(names))
        png = os.path.join(target, prefer[-1]) if prefer else None
    else:
        png = target
    if not png or not os.path.exists(png):
        print('[replay] 找不到截图: %r' % target)
        return 4
    from PIL import Image
    img = Image.open(png)
    scale = float(scale_arg) if scale_arg else (
        img.width / 1096.0 if img.width > 1300 else 1.0)
    print('[replay] %s size=%dx%d scale=%.2f' % (png, img.width, img.height, scale))
    if track_arg:
        try:
            t = [int(v) for v in track_arg.replace(' ', '').split(',')]
            anchor = (t[0], t[1], t[2], t[3])
        except Exception:
            print('[replay] track 参数格式应为 x0,y0,x1,y1（图像px角点）: %r'
                  % track_arg)
            return 4
        rect = _panel_above_track(img, anchor, scale, debug=True)
        print('[replay] 锚点法 photo_rect=%r（track=%r）' % (rect, anchor))
    else:
        print('[replay] 未给 track 锚点 → 密度法兜底（已被实测否定，仅参考）')
        rect, score = _find_photo_rect(img, scale=scale, min_y=int(80 * scale),
                                       debug=True)
        print('[replay] photo_rect=%r score=%.3f' % (rect, score))
    if not rect:
        print('[replay] 未检出照片矩形（锚点法：确认 track 坐标来自同一张截图）')
        return 6
    x, y, w, h = rect
    strip = img.crop((x, y, x + w, y + h))
    outp = os.path.dirname(png) or '.'
    strip_path = os.path.join(outp, 'replay_strip.png')
    strip.save(strip_path)
    if not track_arg:
        track = _find_track_below(img, rect, scale)
        print('[replay] track_rect=%r' % (track,))
        if track:
            img.crop((track[0], track[1], track[2], track[3])).save(
                os.path.join(outp, 'track_template.png'))
            print('[replay] 滑轨模板已存: %s' %
                  os.path.join(outp, 'track_template.png'))
    # 缺口检测：直接调生产引擎 _ncc_gap_detect（与 _solve_slider_once 同源；
    # 无微拖标定，piece_w/min_gap_x 用近似估计，口径同 verify_ncc_pipeline.py）
    try:
        import cv2
    except ImportError:
        print('[replay] cv2 不可用（离线诊断需 numpy/opencv）')
        return 5
    panel_gray = cv2.imread(strip_path, cv2.IMREAD_GRAYSCALE)
    pw_est = max(40, int(panel_gray.shape[1] * 0.14))
    min_gap_x = max(int(panel_gray.shape[1] * 0.30 + pw_est * 0.5),
                    int(pw_est * 1.5), 60)
    print('[replay] 面板 %dx%d piece_w_est=%d min_gap_x=%d（无微拖标定，'
          '近似估计）' % (panel_gray.shape[1], panel_gray.shape[0],
                          pw_est, min_gap_x))
    gap_x, detail = _ncc_gap_detect(panel_gray, pw_est, min_gap_x,
                                    out_dir=outp, attempt=0)
    if gap_x is None:
        print('[replay] NCC 未命中: error=%r z=%r'
              % (detail.get('error'), detail.get('z_left_best')))
        return 5
    print('[replay] NCC gap_x=%d z=%s n=%s'
          % (gap_x, detail.get('z_left_best'), detail.get('cluster_n')))
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == '--selftest':
        return selftest()
    if len(sys.argv) >= 3 and sys.argv[1] == '--captcha-replay':
        return _captcha_replay(sys.argv[2],
                               sys.argv[3] if len(sys.argv) >= 4 else None,
                               sys.argv[4] if len(sys.argv) >= 5 else None)

    inp = {}
    if len(sys.argv) >= 2 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], 'r', encoding='utf-8-sig') as f:
            inp = json.load(f)
    probe_only = bool(inp.get('probe_only') or (len(sys.argv) >= 2 and sys.argv[1] == '--probe'))
    max_slider = int(inp.get('max_slider_attempts') or 6)

    t0 = time.time()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(REPO, 'python', 'output', 'uia_runs', 'login_' + ts)
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        'mode': 'uia_login', 'run_dir': out_dir,
        'input_file': sys.argv[1] if len(sys.argv) >= 2 else '',
        'started_at': datetime.now().isoformat(), 'steps': [],
        'account_configured': False, 'already': False, 'logged_in': False,
    }

    def step(name, **kv):
        payload['steps'].append(dict(step=name, **kv))
        print('[uia] ' + name + ' ' + json.dumps(kv, ensure_ascii=False)[:300], flush=True)

    def fail(code, err) -> int:
        step('fail', code=code, err=err)
        payload['error'] = str(err)
        payload['elapsed_s'] = round(time.time() - t0, 1)
        _finish(payload, out_dir)
        return code

    # 1) 窗口（A11Y/WINDOW 错误由契约层按文本归类）；probe 模式无窗也可返回
    win, win_src = _pick_window(step)
    if win is None:
        if probe_only:
            payload['account_configured'] = cnipa_creds.has_creds()
            payload['ok'] = True
            payload['logged_in'] = False
            payload['elapsed_s'] = round(time.time() - t0, 1)
            step('probe_no_window')
            _finish(payload, out_dir)
            return 0
        return fail(2, '未找到可交互的浏览器窗口。')
    step('window_picked', src=win_src, title=(win.window_text() or '')[:60])
    try:
        win.set_focus()
    except Exception as e:
        return fail(3, '窗口无法聚焦: %s' % e)
    time.sleep(0.3)

    # 2) 已登录？——任一可见文本含 欢迎您 即视为免登录
    texts0 = uia.all_texts(win)
    payload['logged_in_probe'] = looks_logged_in_texts(texts0)
    if probe_only:
        payload['account_configured'] = cnipa_creds.has_creds()
        payload['ok'] = True
        payload['elapsed_s'] = round(time.time() - t0, 1)
        _finish(payload, out_dir)
        return 0
    if payload['logged_in_probe']:
        uia.dismiss_usage_dialog(
            win, step, timeout_s=8.0, absent_giveup_s=2.5)
        payload['ok'] = True
        payload['already'] = True
        payload['logged_in'] = True
        payload['account_configured'] = cnipa_creds.has_creds()
        step('already_logged_in')
        payload['elapsed_s'] = round(time.time() - t0, 1)
        _finish(payload, out_dir)
        return 0

    # 3) 凭证：显式传入 → 存 account.ini；否则读文件；两者皆无 → NEED_CREDENTIALS
    username = str(inp.get('username') or '').strip()
    password = str(inp.get('password') or '')
    login_type = str(inp.get('login_type') or 'natural')
    if username and password:
        try:
            cnipa_creds.save_creds(username, password, login_type)
            step('creds_saved', username=cnipa_creds.mask_username(username),
                 login_type=login_type)
        except ValueError as e:
            return fail(4, '凭证保存失败: %s' % e)
        creds = {'username': username, 'password': password, 'login_type': login_type}
    else:
        creds = cnipa_creds.load_creds()
        if creds is None:
            return fail(4, '未配置凭证：account.ini 缺少账号密码。'
                           '请通过 cnipa_login 工具传入 username/password（首次会保存）。')
        step('creds_loaded', username=cnipa_creds.mask_username(creds['username']))
    payload['account_configured'] = True

    # 4) 若当前已是登录表单则直接填；否则打开 CAS SSO（service=cpquery）
    fields0 = classify_login_fields(_login_edits(win))
    if 'username' in fields0 and 'password' in fields0:
        step('login_form_already_present')
    else:
        _open_url_tab(win, sso_login_url_for_cpquery(), step)

    # 5) 等表单（瑞数挑战/加载轮询，最多 40s）——必须等出带 placeholder 的
    #    账号+密码框；几何兜底在 SPA 半展开时会把密码框当成账号框。
    fields = {}
    for _ in range(20):
        time.sleep(2.0)
        fields = classify_login_fields(_login_edits(win))
        if 'username' in fields and 'password' in fields:
            un = (fields['username'][0] or '')
            pn = (fields['password'][0] or '')
            if any(m in un for m in USERNAME_NAME_MARKS) and any(
                    m in pn for m in PASSWORD_NAME_MARKS):
                break
        err = login_error_in_texts(uia.all_texts(win))
        if err:
            return fail(5, '登录页错误: %s' % err)
    else:
        try:
            uia.screenshot_window(win, os.path.join(out_dir, 'no_form.png'))
        except Exception as e:
            step('no_form_screenshot_fail', err=str(e)[:120])
        flat = os.path.join(out_dir, 'no_form_tree.txt')
        try:
            with open(flat, 'w', encoding='utf-8') as f:
                f.write('\n'.join(uia.all_texts(win)))
        except Exception:
            pass
        return fail(5, '登录表单未出现（40s）：页面结构可能变化或无障碍未暴露输入框，'
                       '证据见 %s' % out_dir)
    step('form_located', username_name=(fields['username'][0] or '')[:40],
         password_name=(fields['password'][0] or '')[:40])

    # 6) 填表（密码只经 SendInput 进输入框，绝不进日志）
    readback = uia.clear_and_type(win, fields['username'][2], creds['username'])
    if not readback or readback.strip() != creds['username'].strip():
        return fail(6, '用户名回读不一致: %r' % cnipa_creds.mask_username(readback or ''))
    step('username_typed', readback_ok=True)
    uia.clear_and_type(win, fields['password'][2], creds['password'])
    step('password_typed')                     # 无内容、无长度——零泄漏
    # 密码框回填会触发页面 JS 激活登录钮，稍候再点
    time.sleep(0.5)

    # 7) 提交 + 滑块（PW handle_cnipa_verify_captcha 同构）
    if not _click_login_button(win, step):
        uia.screenshot_window(win, os.path.join(out_dir, 'no_login_btn.png'))
        return fail(7, '未找到登录按钮（页面结构可能变化）')
    if not _handle_captcha(win, out_dir, step, max_attempts=max_slider):
        return fail(8, '滑块验证未通过（尝试 %d 次）。证据见 %s；'
                       '可先跑 --captcha-replay 校准' % (max_slider, out_dir))

    # 8) 等登录生效（含门户落地兜底）
    state = _wait_post_login(win, step, timeout_s=45.0)
    step('post_login_state', state=state)
    if state.startswith('auth_error'):
        return fail(9, '登录被拒: %s（凭证可能失效，请用 cnipa_login 更新账号密码）'
                  % state.split(':', 1)[1])
    if state == 'timeout':
        uia.screenshot_window(win, os.path.join(out_dir, 'post_timeout.png'))
        return fail(10, '登录后 45s 未检测到登录态（挑战/网络超时），证据见 %s' % out_dir)

    # 9) 「使用说明」：短等；弹窗常晚到，检索阶段还会再关，这里不卡死 30s
    if state in ('cpquery_form', 'logged_in', 'portal_landed'):
        uia.dismiss_usage_dialog(
            win, step, timeout_s=10.0, absent_giveup_s=3.5)

    payload['ok'] = True
    payload['logged_in'] = True
    payload['elapsed_s'] = round(time.time() - t0, 1)
    step('login_ok')
    _finish(payload, out_dir)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        import traceback
        tb = traceback.format_exc()
        crash_dir = os.path.join(REPO, 'python', 'output', 'uia_runs')
        os.makedirs(crash_dir, exist_ok=True)
        with open(os.path.join(crash_dir, 'last_crash.txt'), 'w', encoding='utf-8') as f:
            f.write(tb)
        print('[uia] 未捕获异常（已写 python/output/uia_runs/last_crash.txt）:\n' + tb)
        sys.exit(1)
