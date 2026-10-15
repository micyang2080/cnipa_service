"""
国知局 cpquery / pss Playwright 公共模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

CNIPA_ROOT = os.path.dirname(os.path.abspath(__file__))
if CNIPA_ROOT not in sys.path:
    sys.path.insert(0, CNIPA_ROOT)

UTILS_SRC = os.path.join(CNIPA_ROOT, 'src')
if os.path.isdir(UTILS_SRC) and UTILS_SRC not in sys.path:
    sys.path.insert(0, UTILS_SRC)

SSO_URL = 'https://sso.cponline.cnipa.gov.cn'
TYSF_LOGIN_URL = 'https://tysf.cponline.cnipa.gov.cn/am/#/user/login'
SSO_PORTAL_URL = 'https://tysfjk.cponline.cnipa.gov.cn/portal/'
CPQUERY_INDEX = 'https://cpquery.cponline.cnipa.gov.cn/chinesepatent/index'
PSS_SEARCH = 'https://pss-system.cponline.cnipa.gov.cn/conventionalSearch'

# 隐藏自动化特征的 JS 脚本（绕过反自动化检测）
# v5: 全面覆盖 cpquery 反爬 JS 检测项（基于 304KB 字节码 VM 逆向分析）
#     增加：window 尺寸、screen 属性、硬件指纹、cdc_ 深度清理、
#     Notification 拦截、Connection/RTT、DOM 属性一致性
_HIDE_AUTOMATION_SCRIPT = """
(function() {
    'use strict';

    // ═══════════════════════════════════════════════════
    // 0. ★ 属性保护机制（最先执行！）
    //    412 挑战 JS 会用 Object.defineProperty 覆盖我们的属性
    //    解决方案：先设置 configurable:false，再拦截 defineProperty
    // ═══════════════════════════════════════════════════
    const __protectedProps = new Set();
    const __blockedProps = new Set();  // ★ 完全删除且禁止重新添加的属性
    const __origDefineProperty = Object.defineProperty;
    const __origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
    const __origGetOwn = Object.getOwnPropertyNames;

    Object.defineProperty = function(obj, prop, descriptor) {
        // ★ 阻止被删除的 Playwright 属性被重新添加
        if (obj === window && __blockedProps.has(prop)) {
            return obj;  // 静默忽略
        }
        // 保护已锁定的属性不被挑战 JS 覆盖
        if (obj === navigator || obj === window || obj === screen) {
            const key = (obj === navigator ? 'n:' : obj === window ? 'w:' : 's:') + prop;
            if (__protectedProps.has(key)) {
                return obj; // 静默忽略
            }
        }
        try { return __origDefineProperty.call(this, obj, prop, descriptor); }
        catch(e) { return obj; }
    };
    // toString 伪装为原生函数
    try { Object.defineProperty(Object.defineProperty, 'toString', {
        value: function() { return 'function defineProperty() { [native code] }'; },
        configurable: false,
    }); } catch(e) {}

    Object.getOwnPropertyDescriptor = function(obj, prop) {
        const desc = __origGetOwnPropertyDescriptor.call(this, obj, prop);
        if (desc && (obj === navigator || obj === window || obj === screen)) {
            const key = (obj === navigator ? 'n:' : obj === window ? 'w:' : 's:') + prop;
            if (__protectedProps.has(key)) {
                // 返回克隆，隐藏 configurable:false 的真实状态
                return {
                    get: desc.get, set: desc.set,
                    enumerable: desc.enumerable !== false,
                    configurable: true,  // 对外显示为 configurable
                };
            }
        }
        return desc;
    };
    try { Object.defineProperty(Object.getOwnPropertyDescriptor, 'toString', {
        value: function() { return 'function getOwnPropertyDescriptor() { [native code] }'; },
        configurable: false,
    }); } catch(e) {}

    // 辅助函数：设置受保护的属性
    function _safeDefine(obj, prop, getter) {
        const key = (obj === navigator ? 'n:' : obj === window ? 'w:' : 's:') + prop;
        try {
            __origDefineProperty.call(Object, obj, prop, {
                get: getter,
                set: function() {},  // 无操作 setter
                enumerable: true,
                configurable: false,  // ★ 关键：不可被重新定义
            });
            __protectedProps.add(key);
        } catch(e) {}
    }

    // 辅助函数：彻底删除属性并禁止重新添加
    function _blockAndDelete(obj, prop) {
        __blockedProps.add(prop);
        try { delete obj[prop]; } catch(e) {}
        // 如果 delete 失败（non-configurable），尝试用原生 defineProperty 覆盖为 undefined
        try {
            if (prop in obj) {
                __origDefineProperty.call(Object, obj, prop, {
                    value: undefined,
                    writable: true,
                    enumerable: false,  // ★ 不可枚举，for...in 和 Object.keys 看不到
                    configurable: true,
                });
            }
        } catch(e) {}
    }

    // ═══════════════════════════════════════════════════
    // 1. navigator.webdriver — 最关键指纹
    //    必须返回 undefined（不是 false），与真实浏览器一致
    // ═══════════════════════════════════════════════════
    _safeDefine(navigator, 'webdriver', () => undefined);
    try { delete navigator.__proto__.webdriver; } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 2. chrome.runtime（Playwright 缺失）
    // ═══════════════════════════════════════════════════
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || {
        id: undefined,
        connect: function() { return { onMessage: { addListener: function(){} }, postMessage: function(){} }; },
        sendMessage: function() {},
        onMessage: { addListener: function(){} },
    };

    // ═══════════════════════════════════════════════════
    // 3. permissions.query — 防止 Notification 权限检测
    // ═══════════════════════════════════════════════════
    if (navigator.permissions) {
        try {
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (parameters) => {
                if (parameters && parameters.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission, onchange: null });
                }
                return origQuery(parameters);
            };
        } catch(e) {}
    }

    // ═══════════════════════════════════════════════════
    // 4. navigator.plugins — Playwright 为空数组，正常浏览器有 5+
    //    始终覆盖（不检查 length），因为 Playwright 可能已有默认插件
    // ═══════════════════════════════════════════════════
    try {
        const fakePlugins = [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        ];
        fakePlugins.refresh = function() {};
        fakePlugins.item = function(i) { return fakePlugins[i] || null; };
        fakePlugins.namedItem = function(name) { return fakePlugins.find(p => p.name === name) || null; };
        _safeDefine(navigator, 'plugins', () => fakePlugins);
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 5. navigator.languages — ★ 被 412 挑战 JS 覆盖的关键属性
    //    使用 _safeDefine（configurable:false）防止被挑战 JS 重写
    // ═══════════════════════════════════════════════════
    const fakeLangs = ['zh-CN', 'zh', 'en-US', 'en'];
    _safeDefine(navigator, 'languages', () => Object.freeze(fakeLangs));

    // ═══════════════════════════════════════════════════
    // 6. window 尺寸一致性（反爬 JS 检测窗口/屏幕矛盾）
    //    检测项：窗口位置与屏幕的矛盾、窗口宽高比异常
    //    ★ v6: 使用 _safeDefine 防止被挑战 JS 覆盖
    // ═══════════════════════════════════════════════════
    try {
        const screenW = screen.width || 1920;
        const screenH = screen.height || 1080;
        const outerW = screenW;
        const outerH = screenH - 40;
        const innerW = outerW;
        const innerH = outerH - 85;

        _safeDefine(window, 'outerWidth', () => outerW);
        _safeDefine(window, 'outerHeight', () => outerH);
        _safeDefine(window, 'innerWidth', () => innerW);
        _safeDefine(window, 'innerHeight', () => innerH);
        _safeDefine(window, 'screenX', () => 0);
        _safeDefine(window, 'screenY', () => 0);
        _safeDefine(window, 'screenLeft', () => 0);
        _safeDefine(window, 'screenTop', () => 0);
        if (window.devicePixelRatio < 1) {
            _safeDefine(window, 'devicePixelRatio', () => 1);
        }
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 7. screen 属性 — 反爬 JS 检测 colorDepth / pixelDepth
    //    ★ v6: 使用 _safeDefine
    // ═══════════════════════════════════════════════════
    _safeDefine(screen, 'colorDepth', () => 24);
    _safeDefine(screen, 'pixelDepth', () => 24);
    _safeDefine(screen, 'availWidth', () => screen.width || 1920);
    _safeDefine(screen, 'availHeight', () => (screen.height || 1080) - 40);

    // ═══════════════════════════════════════════════════
    // 8. 硬件指纹 — 反爬 JS 可能检测异常值
    //    ★ v6: 始终覆盖 + _safeDefine
    // ═══════════════════════════════════════════════════
    _safeDefine(navigator, 'deviceMemory', () => 8);
    _safeDefine(navigator, 'hardwareConcurrency', () => 8);
    _safeDefine(navigator, 'maxTouchPoints', () => 0);

    // ═══════════════════════════════════════════════════
    // 9. 删除 Playwright 注入的 cdc_ 变量（关键指纹！）
    //    ★ v6: 先用原始 getOwnPropertyNames 找到并删除 cdc_ 键
    //    然后拦截 getOwnPropertyNames 隐藏残留
    // ═══════════════════════════════════════════════════
    try {
        // ★ 使用原始函数，因为我们的覆盖可能已经过滤了 cdc_ 键
        const realKeys = __origGetOwn.call(Object, window);
        for (const k of realKeys) {
            if (k.startsWith('cdc_') || k.startsWith('goog_') || k.startsWith('$cdc_')) {
                try { delete window[k]; } catch(e) {}
            }
        }
    } catch(e) {}
    // 持续清理：拦截 Object.getOwnPropertyNames 隐藏 cdc_ 前缀
    try {
        Object.getOwnPropertyNames = function(obj) {
            const names = __origGetOwn.call(this, obj);
            if (obj === window) {
                return names.filter(n => !n.startsWith('cdc_') && !n.startsWith('goog_') && !n.startsWith('$cdc_'));
            }
            return names;
        };
        try { Object.defineProperty(Object.getOwnPropertyNames, 'toString', {
            value: function() { return 'function getOwnPropertyNames() { [native code] }'; },
            configurable: false,
        }); } catch(e) {}
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 10. Notification.permission（直接读可能抛异常）
    //    ★ v6: 使用原生 defineProperty（Notification 不在保护列表中）
    // ═══════════════════════════════════════════════════
    try {
        __origDefineProperty.call(Object, Notification, 'permission', {
            get: () => 'default',
            configurable: false,
        });
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 11. 隐藏 Playwright 特有的 window 属性
    //     ★ v7: 彻底删除 + 禁止重新添加（不再用 _safeDefine）
    //     _safeDefine 的问题：属性仍存在（in 返回 true），挑战 JS 可检测
    // ═══════════════════════════════════════════════════
    try {
        const pwProps = ['__playwright', '__pw_manual', '__PW_inspect', '__pw_app'];
        for (const p of pwProps) {
            _blockAndDelete(window, p);
        }
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 12. navigator.connection — 反爬 JS 可能检测 rtt / downlink
    //     ★ v6: 使用原生 defineProperty + configurable:false
    // ═══════════════════════════════════════════════════
    try {
        if (navigator.connection) {
            __origDefineProperty.call(Object, navigator.connection, 'rtt', { get: () => 50, configurable: false });
            __origDefineProperty.call(Object, navigator.connection, 'downlink', { get: () => 10, configurable: false });
            __origDefineProperty.call(Object, navigator.connection, 'effectiveType', { get: () => '4g', configurable: false });
        }
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 13. document.documentElement 属性一致性
    //     反爬 JS 可能检查 clientWidth/clientHeight 与 window 尺寸的关系
    //     ★ v6: 使用原生 defineProperty + configurable:false
    // ═══════════════════════════════════════════════════
    try {
        const docEl = document.documentElement;
        if (docEl && docEl.clientWidth === 0) {
            __origDefineProperty.call(Object, docEl, 'clientWidth', { get: () => window.innerWidth || 1920, configurable: false });
            __origDefineProperty.call(Object, docEl, 'clientHeight', { get: () => (window.innerHeight || 900) - 20, configurable: false });
        }
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 14. WebGL vendor/renderer — 反爬 JS 可能检测异常 GPU
    // ═══════════════════════════════════════════════════
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
            const getExtension = gl.getExtension.bind(gl);
            gl.getExtension = function(name) {
                const ext = getExtension(name);
                if (name === 'WEBGL_debug_renderer_info' && ext) {
                    const origParam = gl.getParameter.bind(gl);
                    gl.getParameter = function(pname) {
                        // UNMASKED_VENDOR_WEBGL
                        if (pname === 37445) return 'Google Inc. (NVIDIA)';
                        // UNMASKED_RENDERER_WEBGL
                        if (pname === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)';
                        return origParam(pname);
                    };
                }
                return ext;
            };
        }
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 15. 阻止 iframe contentWindow 检测
    //     反爬 JS 可能创建隐藏 iframe 检测自动化特征
    // ═══════════════════════════════════════════════════
    try {
        const origAttachShadow = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function() {
            return origAttachShadow.call(this, { mode: 'open' });
        };
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 16. 时间一致性 — 反爬 JS 检测 Date 异常
    //     某些自动化环境会修改 Date.now()
    // ═══════════════════════════════════════════════════
    try {
        // 确保 performance.now() 和 Date.now() 一致
        const _perfNow = performance.now.bind(performance);
        const _dateNow = Date.now;
        // 不做修改，只确保不被篡改
    } catch(e) {}

    // ═══════════════════════════════════════════════════
    // 17. 隐藏 AutomationControlled 标记
    //     Chrome 启动参数 --disable-blink-features=AutomationControlled
    //     已在 launch 参数中设置，这里确保 DOM 属性也正确
    // ═══════════════════════════════════════════════════
    try {
        // 移除可能的 domAutomationController
        if (window.domAutomationController) {
            delete window.domAutomationController;
        }
        if (window.domAuto) {
            delete window.domAuto;
        }
    } catch(e) {}

})();
"""


def _sso_login_url_for_cpquery() -> str:
    """标准 CAS SSO 登录 URL，service 参数由 SSO 服务端处理回跳。

    与 tysf_login_url_for_cpquery() 的区别：
    - 本 URL 走 sso.cponline 的 CAS 协议，登录后 SSO 服务端直接 302 回 cpquery
    - tysf URL 带 redirectUrl/redirect/goto 查询参数，但 hash 路由前端不读，
      登录后默认落到 tysfjk 门户（错误站）
    """
    from urllib.parse import quote

    q = quote(CPQUERY_INDEX, safe='')
    return f'{SSO_URL}/login?service={q}'


def tysf_login_url_for_cpquery() -> str:
    """带回跳到 cpquery 的登录地址（备用，hash 路由下 redirect* 可能不生效）。"""
    from urllib.parse import quote

    q = quote(CPQUERY_INDEX, safe='')
    return f'{TYSF_LOGIN_URL}?redirectUrl={q}&redirect={q}&goto={q}'


LOGIN_TAB_LABELS = {
    'agency': '代理机构登录',
    'legal': '法人登录',
    'natural': '自然人登录',
}

SSO_LOGIN_COOLDOWN_SEC = int(os.environ.get('CNIPA_SSO_COOLDOWN_SEC', '600'))

FIELD_LABEL_MAP = {
    'application_no': ['申请号', '专利号', '申请号/专利号'],
    'ipc_class': ['分类号', 'IPC分类号'],
    'decision_no': ['决定号'],
    'case_no': ['案件编号'],
    'invention_title': ['发明名称', '发明创造名称'],
    'patent_type': ['专利类型'],
    'patentee': ['专利权人'],
    'petitioner': ['请求人', '复审请求人', '无效宣告请求人'],
    'panel_leader': ['组长', '合议组组长'],
    'panel_examiner': ['主审', '主审员', '主审员姓名'],
    'panel_member': ['参审', '参审员'],
    'agency': ['代理机构'],
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _ensure_stdio_line_buffered() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass


def _parse_json_stdin() -> Dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _parse_json_input(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """argv[1] 为 .json 文件路径则直接读文件（PowerShell 不支持 `<` 重定向）；否则回落 stdin。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    for cand in argv:
        if cand.endswith('.json') and os.path.isfile(cand):
            with open(cand, 'r', encoding='utf-8-sig') as f:
                raw = f.read()
            if not raw.strip():
                return {}
            return json.loads(raw)
    return _parse_json_stdin()


def emit_result(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# ★ 联调调试框架（2026-08-31）：一次运行抓全修选择器所需的一切
#
# 背景：真实环境运行只能由用户执行（防封号，禁止 Agent 反复试探），
# 所以每一步都要落盘三样东西：
#   1) 截图 PNG      —— 看页面当时长什么样
#   2) 完整 HTML     —— 离线分析真实 DOM 结构/类名/层级
#   3) 元素清单 txt  —— 全部可见 label/button/input/select/tab 及类名
# 明天联调若某步选择器没命中，凭这三样离线即可修好，不需要再跑。
# 输出：{CNIPA_ROOT}/output/debug_steps/{script}_{时间戳}/{序号}_{tag}.*
# 开关：CNIPA_DEBUG_DUMP=0 关闭（默认开）
# ---------------------------------------------------------------------------

_ACTIVE_DUMPER: Optional['StepDumper'] = None


def debug_dump_enabled() -> bool:
    return os.environ.get('CNIPA_DEBUG_DUMP', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def set_step_dumper(dumper: Optional['StepDumper']) -> None:
    """脚本入口设置当前 dumper；公共函数内部经 get_step_dumper() 取用。"""
    global _ACTIVE_DUMPER
    _ACTIVE_DUMPER = dumper


def get_step_dumper() -> Optional['StepDumper']:
    return _ACTIVE_DUMPER


class StepDumper:
    """按步骤落盘截图/HTML/元素清单；任何内部异常都不影响主流程。"""

    _MAX_HTML = 4_000_000
    _MAX_ELEMENTS = 400

    def __init__(self, script_name: str) -> None:
        self.enabled = debug_dump_enabled()
        self.dir = ''
        self.seq = 0
        if not self.enabled:
            return
        stamp = time.strftime('%Y%m%d_%H%M%S')
        self.dir = os.path.join(CNIPA_ROOT, 'output', 'debug_steps', f'{script_name}_{stamp}')
        os.makedirs(self.dir, exist_ok=True)
        _log(f'[dump] 调试落盘目录: {self.dir}')

    @staticmethod
    def _describe_el(el: Any) -> str:
        try:
            tag = el.evaluate('e => e.tagName.toLowerCase()')
            cls = (el.get_attribute('class') or '')[:80]
            txt = (el.inner_text() or '').strip().replace('\n', ' ')[:40]
            ph = el.get_attribute('placeholder') or ''
            href = (el.get_attribute('href') or '')[:100]
            name = el.get_attribute('name') or ''
            typ = el.get_attribute('type') or ''
            ro = '[readonly]' if (el.get_attribute('readonly') is not None) else ''
            parts = [f'<{tag}>']
            if cls:
                parts.append(f'class="{cls}"')
            if name:
                parts.append(f'name="{name}"')
            if typ:
                parts.append(f'type="{typ}"')
            if ro:
                parts.append(ro)
            if ph:
                parts.append(f'placeholder="{ph}"')
            if href:
                parts.append(f'href="{href}"')
            if txt:
                parts.append(f'text="{txt}"')
            return ' '.join(parts)
        except Exception:
            return ''

    def _dump_elements(self, page: Any) -> str:
        lines: List[str] = []
        selectors = (
            'label',
            'button',
            'a',
            'input',
            'textarea',
            'select',
            '[class*="el-select"]',
            '[class*="el-radio"]',
            '[class*="el-checkbox"]',
            '[class*="el-date"]',
            '[role="tab"]',
            '[class*="tab"]',
            '[class*="menu-item"]',
            '[class*="expand"]',
            '[class*="search"]',
        )
        seen = set()
        for selector in selectors:
            try:
                els = page.locator(selector)
                count = min(els.count(), self._MAX_ELEMENTS)
                for i in range(count):
                    el = els.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        continue
                    desc = self._describe_el(el)
                    if not desc or desc in seen:
                        continue
                    seen.add(desc)
                    lines.append(desc)
                    if len(lines) >= self._MAX_ELEMENTS:
                        return '\n'.join(lines)
            except Exception:
                continue
        return '\n'.join(lines)

    def dump(self, page: Any, tag: str) -> None:
        """落盘一步：PNG + HTML + elements.txt。失败只记日志，绝不抛出。"""
        if not self.enabled:
            return
        self.seq += 1
        safe_tag = re.sub(r'[^0-9A-Za-z_.-]+', '_', tag)
        base = os.path.join(self.dir, f'{self.seq:02d}_{safe_tag}')
        try:
            page.screenshot(path=base + '.png', full_page=True, timeout=15_000)
        except Exception as exc:
            _log(f'[dump] 截图失败 {tag}: {exc}')
        try:
            html = page.content() or ''
            if len(html) > self._MAX_HTML:
                html = html[: self._MAX_HTML] + f'\n<!-- truncated (total {len(html)} chars) -->'
            with open(base + '.html', 'w', encoding='utf-8') as f:
                f.write(f'<!-- URL: {page.url} -->\n{html}')
        except Exception as exc:
            _log(f'[dump] HTML 失败 {tag}: {exc}')
        try:
            with open(base + '.elements.txt', 'w', encoding='utf-8') as f:
                f.write(f'URL: {page.url}\nTIME: {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n')
                f.write(self._dump_elements(page))
        except Exception as exc:
            _log(f'[dump] 元素清单失败 {tag}: {exc}')
        _log(f'[dump] {self.seq:02d} {tag} → {os.path.basename(base)}.*  url={page.url[:90]}')

    def dump_fail(self, page: Any, tag: str, exc: BaseException) -> None:
        """异常路径：先 dump 页面再写 error.txt，保证一次运行可离线定位。"""
        try:
            self.dump(page, f'FAIL_{tag}')
            err_path = os.path.join(self.dir, f'{self.seq:02d}_FAIL_{tag}.error.txt')
            with open(err_path, 'w', encoding='utf-8') as f:
                f.write(f'{type(exc).__name__}: {exc}\n')
        except Exception:
            pass


def _ensure_page_rendered(page: Any, *, min_len: int = 200, max_wait_ms: int = 15_000) -> bool:
    """等待页面真实渲染（防瑞数 JS 壳：body 文本极短时继续等再判断）。

    pss / cpquery 首跳可能先返回 412 挑战壳（body 几乎无文本），须等 JS
    执行完 reload 出真实内容后再做元素定位。返回 body 文本长度是否达标。
    """
    deadline = time.time() + max_wait_ms / 1000
    while time.time() < deadline:
        try:
            body = page.inner_text('body') or ''
        except Exception:
            body = ''
        if len(body.strip()) >= min_len:
            return True
        page.wait_for_timeout(1000)
    return False


def _should_launch_headless(args: Dict[str, Any]) -> bool:
    """★ v9：默认改为有头（headed）。

    本地实测证据（见 2026-08-31 诊断）：headless 模式下
    - navigator.userAgent 含 "HeadlessChrome"
    - navigator.userAgentData.brands === []（真实浏览器永不为空）
    - 请求头完全没有 sec-ch-ua 系列客户端提示
    三项均为浏览器原生层泄漏，init script 无法修补，瑞数 VM 一查即中。
    有头模式下三项全部自洽（UA=brands=sec-ch-ua 同版本）。
    仅当 CNIPA_HEADLESS 显式设为 1/true/yes 时才用无头。
    """
    if args.get('headless') is False:
        return False
    env = os.environ.get('CNIPA_HEADLESS', '0').strip().lower()
    if env in ('1', 'true', 'yes'):
        return True
    return False


def resolve_credentials_path(args: Dict[str, Any]) -> Optional[str]:
    for key in ('credentials_path', 'credentials_file'):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return os.path.abspath(val.strip())
    env = os.environ.get('CNIPA_CREDENTIALS_PATH', '').strip()
    if env:
        return os.path.abspath(env)
    candidates = [
        os.path.join(CNIPA_ROOT, 'cnipa_credentials.json'),
        os.path.join(CNIPA_ROOT, '..', '..', 'Skills', 'patent-reexam-invalid-report', 'secrets', 'cnipa_credentials.json'),
    ]
    for c in candidates:
        p = os.path.abspath(c)
        if os.path.isfile(p):
            return p
    return None


def load_credentials(args: Dict[str, Any]) -> Dict[str, str]:
    username = str(args.get('username') or '').strip()
    password = str(args.get('password') or '').strip()
    login_type = str(args.get('login_type') or '').strip().lower()

    cred_path = resolve_credentials_path(args)
    file_data: Dict[str, Any] = {}
    if cred_path and os.path.isfile(cred_path):
        with open(cred_path, 'r', encoding='utf-8') as f:
            file_data = json.load(f)

    if not username:
        username = str(file_data.get('username') or '').strip()
    if not password:
        password = str(file_data.get('password') or '').strip()
    if not login_type:
        login_type = str(file_data.get('login_type') or 'natural').strip().lower()

    if login_type not in LOGIN_TAB_LABELS:
        login_type = 'natural'

    if not username or not password:
        raise ValueError(
            'NEED_CREDENTIALS: 缺少国知局账号。请在 GeniusWork 中提供 username/password，'
            '或调用 cnipa_save_credentials 写入配置文件。'
        )

    return {
        'username': username,
        'password': password,
        'login_type': login_type,
        'credentials_path': cred_path or '',
    }


def resolve_session_dir(args: Dict[str, Any]) -> str:
    for key in ('session_dir', 'session_path'):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            p = os.path.abspath(val.strip())
            os.makedirs(p, exist_ok=True)
            return p
    env = os.environ.get('CNIPA_SESSION_DIR', '').strip()
    if env:
        p = os.path.abspath(env)
        os.makedirs(p, exist_ok=True)
        return p
    artifact = os.environ.get('CNIPA_ARTIFACT_ROOT', '').strip()
    if artifact:
        p = os.path.join(os.path.abspath(artifact), 'cnipa_session')
        os.makedirs(p, exist_ok=True)
        return p
    default = os.path.join(CNIPA_ROOT, 'output', 'cnipa_session')
    os.makedirs(default, exist_ok=True)
    return default


def session_state_path(session_dir: str, username: str) -> str:
    digest = hashlib.sha256(username.encode('utf-8')).hexdigest()[:16]
    return os.path.join(session_dir, f'{digest}.json')


def _is_unsafe_url(url: str) -> bool:
    return '/unsafe' in (url or '').lower()


def _sso_cooldown_path(session_dir: str) -> str:
    return os.path.join(session_dir, '.last_sso_attempt')


def _can_attempt_sso_login(session_dir: str) -> Tuple[bool, int]:
    if SSO_LOGIN_COOLDOWN_SEC <= 0:
        return True, 0
    path = _sso_cooldown_path(session_dir)
    if not os.path.isfile(path):
        return True, 0
    elapsed = int(time.time() - os.path.getmtime(path))
    remaining = max(0, SSO_LOGIN_COOLDOWN_SEC - elapsed)
    return remaining <= 0, remaining


def _mark_sso_attempt(session_dir: str) -> None:
    os.makedirs(session_dir, exist_ok=True)
    with open(_sso_cooldown_path(session_dir), 'w', encoding='utf-8') as f:
        f.write(str(int(time.time())))


def _page_has_unsafe(page: Any) -> bool:
    try:
        if _is_unsafe_url(page.url):
            return True
        body = page.inner_text('body') or ''
        return '安全异常' in body and '登录环境安全等级' in body
    except Exception:
        return _is_unsafe_url(getattr(page, 'url', ''))


def _raise_if_unsafe(page: Any) -> None:
    if _page_has_unsafe(page):
        raise RuntimeError(
            '国知局检测到安全异常 (/unsafe)。请关闭浏览器、稍后再试；'
            '勿频繁自动登录，建议手动登录一次后复用 session。'
        )


# 全局 JS 错误收集器（诊断用）
_JS_ERROR_COLLECTOR = """
    window.__cnipa_js_errors = window.__cnipa_js_errors || [];
    window.addEventListener('error', (e) => {
        const msg = (e.message || '') + ' at ' + (e.filename || '') + ':' + (e.lineno || '');
        window.__cnipa_js_errors.push(msg);
        if (window.__cnipa_js_errors.length > 100) window.__cnipa_js_errors.shift();
    });
    window.addEventListener('unhandledrejection', (e) => {
        const reason = e.reason ? (e.reason.message || e.reason.toString ? e.reason.toString() : String(e.reason)) : 'unknown';
        window.__cnipa_js_errors.push('unhandledrejection: ' + reason);
        if (window.__cnipa_js_errors.length > 100) window.__cnipa_js_errors.shift();
    });
"""


def _hide_script_enabled() -> bool:
    """★ v10：伪装脚本默认关闭，CNIPA_HIDE_SCRIPT=1 显式开启（供 A/B 对照）。

    本地实测证据链（2026-08-31）：
    1. 不加任何 init script 时，Playwright headed Chromium 页面上
       __playwright/__pw_manual 等泄漏为 0、navigator.webdriver 原生即 False、
       无任何 __ 开头的可疑全局 —— 原生环境本来就是干净的；
    2. add_init_script 机制本身会向页面注入 Playwright 工具绑定（即早前诊断到的
       '泄漏'），也就是说泄漏是伪装脚本自己引入的，v7 又用拦截器去遮；
    3. 伪装脚本的 Object.defineProperty 拦截器改变原生语义（真浏览器对
       不可配置属性重定义会抛 TypeError，拦截器静默吞掉），反爬 VM 可借此
       一票判定环境被篡改 —— 伪装脚本可能反而是败因。
    因此默认回归零注入：让浏览器以原生自洽指纹（headed Chromium 145，
    UA/brands/sec-ch-ua 三者一致）迎接挑战。
    """
    return os.environ.get('CNIPA_HIDE_SCRIPT', '0').strip() == '1'


def _apply_hide_automation(page: Any) -> None:
    """隐藏浏览器自动化特征（绕过反自动化检测）。

    ★ v10：默认不注入（见 _hide_script_enabled 的实测依据）。
    """
    if not _hide_script_enabled():
        _log('[login] v10：未注入任何 init script（原生指纹自洽；CNIPA_HIDE_SCRIPT=1 可开旧伪装脚本做 A/B）')
        return
    try:
        page.add_init_script(_HIDE_AUTOMATION_SCRIPT)
        page.add_init_script(_JS_ERROR_COLLECTOR)
        _log('[login] 已注入旧版伪装脚本 v7 + JS 错误收集器（A/B 对照模式）')
    except Exception as exc:
        _log(f'[login] 应用反自动化脚本失败: {exc}')


def _simulate_behavioral_events(page: Any) -> None:
    """在页面上模拟真实用户行为事件（v8：全部改用 CDP 级输入）。

    ★ v8 根因修复：原实现用 page.evaluate + dispatchEvent 派发事件，
    该类事件 isTrusted 全为 false（已本地实测验证），反爬 JS 只要监听
    isTrusted 即可一票否决——大量不可信合成事件比没有事件更是机器人铁证。

    CDP 级输入（page.mouse / page.keyboard）经浏览器 Input 域产生，
    isTrusted=true，与真实用户输入在 JS 层面无法区分（已本地实测验证）。
    """
    try:
        import random as _random

        # 视口中心目标点
        try:
            vw = page.viewport_size or {}
            cx = (vw.get('width') or 1440) // 2
            cy = (vw.get('height') or 900) // 2
        except Exception:
            cx, cy = 720, 450

        # 1. 真实鼠标轨迹（CDP 级）：向中心收敛的随机游走
        x, y = _random.randint(50, 200), _random.randint(50, 150)
        for _ in range(15):
            x += _random.randint(-25, 40) + (cx - x) // 12
            y += _random.randint(-18, 30) + (cy - y) // 12
            x = max(5, min(x, (cx * 2) - 5))
            y = max(5, min(y, (cy * 2) - 5))
            page.mouse.move(x, y)
            page.wait_for_timeout(_random.randint(20, 60))

        # 2. 真实点击（CDP 级）
        page.mouse.click(
            max(5, cx + _random.randint(-80, 80)),
            max(5, cy + _random.randint(-60, 60)),
        )
        page.wait_for_timeout(_random.randint(100, 300))

        # 3. 真实键盘输入（CDP 级）
        page.keyboard.type('hello', delay=_random.randint(60, 120))
        page.wait_for_timeout(_random.randint(100, 300))
        for _ in range(3):
            page.keyboard.press('ArrowDown')
            page.wait_for_timeout(_random.randint(80, 200))

        # 4. 真实滚动（CDP 级 wheel，isTrusted=true）
        page.mouse.wheel(0, _random.randint(150, 350))
        page.wait_for_timeout(_random.randint(200, 500))
        page.mouse.wheel(0, -_random.randint(80, 200))

        _log('[login] 已模拟 CDP 级行为事件（isTrusted=true：鼠标轨迹/点击/键盘/滚动）')
    except Exception as exc:
        _log(f'[login] CDP 行为事件模拟失败（非致命）: {exc}')


def _diagnose_page_state(page: Any, *, tag: str = '') -> Dict[str, Any]:
    """收集页面结构化诊断信息（反检测状态、DOM、JS 错误、cookie、网络）。

    在关键节点调用，输出到日志 + 写 JSON 文件，便于分析每次测试的成功/失败原因。
    """
    info: Dict[str, Any] = {'tag': tag, 'url': page.url or ''}
    try:
        page.wait_for_timeout(500)  # 等 JS 执行稳定
    except Exception:
        pass

    # 1. 反检测是否生效
    try:
        info['anti_detect'] = page.evaluate("""() => {
            const cdcKeys = Object.keys(window).filter(k => k.startsWith('cdc_') || k.startsWith('goog_'));
            const pwKeys = ['__playwright', '__pw_manual', '__PW_inspect'].filter(k => k in window);
            let pluginsInfo = null;
            if (navigator.plugins) {
                pluginsInfo = {
                    length: navigator.plugins.length,
                    has_refresh: typeof navigator.plugins.refresh === 'function',
                    has_item: typeof navigator.plugins.item === 'function',
                    names: Array.from({length: Math.min(navigator.plugins.length, 10)}, (_, i) => {
                        try { return navigator.plugins[i] && navigator.plugins[i].name; } catch(e) { return null; }
                    }).filter(Boolean),
                };
            }
            return {
                webdriver: navigator.webdriver,
                plugins: pluginsInfo,
                languages: navigator.languages ? Array.from(navigator.languages) : null,
                chrome_runtime: (typeof window.chrome !== 'undefined' && typeof window.chrome.runtime !== 'undefined'),
                notification_perm: (typeof Notification !== 'undefined') ? Notification.permission : 'N/A',
                cdc_leaked: cdcKeys,
                playwright_leaked: pwKeys,
            };
        }""")
    except Exception as exc:
        info['anti_detect_error'] = str(exc)

    # 2. DOM 状态
    try:
        body_text = (page.inner_text('body') or '').strip()
        info['dom'] = {
            'body_text_len': len(body_text),
            'body_preview': body_text[:500],
            'title': page.title() or '',
            'html_len': 0,
        }
        try:
            html = page.content() or ''
            info['dom']['html_len'] = len(html)
            # ★ 当 HTML 很小时，保存完整内容（诊断 39 字节空响应）
            if 0 < len(html) < 500:
                info['dom']['html_raw'] = html
                _log(f'[diag:{tag}] ★ html_raw({len(html)}): {html!r}')
            # 检查关键元素
            info['dom']['has_login_form'] = 'password' in html.lower() and ('login' in html.lower() or '登录' in html)
            info['dom']['has_welcome'] = '欢迎您' in body_text
            info['dom']['has_usage_dialog'] = '使用说明' in body_text
            info['dom']['has_safety_error'] = '安全异常' in body_text
        except Exception:
            pass
    except Exception as exc:
        info['dom_error'] = str(exc)

    # 3. JS 错误
    try:
        js_errors = page.evaluate("""() => {
            if (window.__cnipa_js_errors) return window.__cnipa_js_errors.slice(-30);
            return [];
        }""")
        info['js_errors'] = js_errors or []
        info['js_error_count'] = len(info['js_errors'])
    except Exception:
        info['js_errors'] = []
        info['js_error_count'] = -1  # 收集器未注入

    # 4. Cookie（cpquery 域 + 全域）
    try:
        cookies = page.context.cookies('https://cpquery.cponline.cnipa.gov.cn')
        info['cookies'] = {
            'count': len(cookies),
            'names': [c['name'] for c in cookies[:20]],
            'has_session': any('session' in c['name'].lower() or 'token' in c['name'].lower() or 'JSESSIONID' in c['name'] for c in cookies),
        }
    except Exception as exc:
        info['cookies_error'] = str(exc)

    # 4b. ★ 全域 cookie（所有 cponline 子域，诊断 SSO 票据传播）
    try:
        all_cookies = page.context.cookies()
        # 按域名分组
        by_domain: Dict[str, list] = {}
        for c in all_cookies:
            d = c.get('domain', '?')
            by_domain.setdefault(d, []).append(c['name'])
        info['all_cookies'] = {
            'total': len(all_cookies),
            'by_domain': {d: names for d, names in sorted(by_domain.items())},
            'cponline_domains': {
                d: names for d, names in sorted(by_domain.items())
                if 'cponline' in d
            },
        }
    except Exception as exc:
        info['all_cookies_error'] = str(exc)

    # 5. 网络请求失败（通过 performance API）
    try:
        net_info = page.evaluate("""() => {
            try {
                const entries = performance.getEntriesByType('resource');
                const failed = entries.filter(e => {
                    // 检查 transferSize=0 且非 cache 的请求（可能失败）
                    return e.transferSize === 0 && e.decodedBodySize === 0 && !e.initiatorType.match(/^(css|img|font|script)$/);
                });
                return {
                    total_resources: entries.length,
                    suspicious_zero_transfer: failed.length,
                    sample_failed: failed.slice(0, 5).map(e => ({name: e.name.substring(0, 100), type: e.initiatorType})),
                };
            } catch(e) { return {error: e.message}; }
        }""")
        info['network'] = net_info
    except Exception as exc:
        info['network_error'] = str(exc)

    # 6. 页面可见性 / iframe 检测
    try:
        info['visibility'] = page.evaluate("""() => {
            return {
                hidden: document.hidden,
                visibilityState: document.visibilityState,
                iframe_count: document.querySelectorAll('iframe').length,
                iframe_srcs: Array.from(document.querySelectorAll('iframe')).map(f => f.src).slice(0, 5),
            };
        }""")
    except Exception:
        pass

    return info


def _snapshot_cookies_and_redirects(page: Any, *, tag: str = '') -> Dict[str, Any]:
    """滑块通过后立即抓取三域 cookie + 跳转链，用于诊断 SSO 换票链完整性。

    返回：
    - cookies_by_domain: {sso, tysfjk, cpquery} 各域 cookie 列表
    - redirect_chain: performance navigation 条目（含 startTime, redirectCount, type）
    - current_url: 当前页面 URL
    """
    info: Dict[str, Any] = {'tag': tag, 'current_url': page.url or ''}

    # 1. 三域 cookie 快照
    domains = {
        'sso': 'https://sso.cponline.cnipa.gov.cn',
        'tysfjk': 'https://tysfjk.cponline.cnipa.gov.cn',
        'cpquery': 'https://cpquery.cponline.cnipa.gov.cn',
    }
    cookies_by_domain: Dict[str, list] = {}
    for name, url in domains.items():
        try:
            cookies = page.context.cookies(url)
            cookies_by_domain[name] = [
                {'name': c['name'], 'domain': c.get('domain', ''), 'path': c.get('path', ''),
                 'value_len': len(c.get('value', '')), 'expires': c.get('expires', -1)}
                for c in cookies
            ]
        except Exception as exc:
            cookies_by_domain[name] = [{'error': str(exc)}]
    info['cookies_by_domain'] = cookies_by_domain

    # 2. 跳转链（performance navigation）
    try:
        nav_entries = page.evaluate("""() => {
            const entries = performance.getEntriesByType('navigation');
            return entries.map(e => ({
                name: e.name,
                type: e.type,
                redirectCount: e.redirectCount,
                startTime: e.startTime,
                duration: e.duration,
                transferSize: e.transferSize,
            }));
        }""")
        info['navigation_entries'] = nav_entries or []
    except Exception as exc:
        info['navigation_error'] = str(exc)

    # 3. 当前页面资源数
    try:
        resources = page.evaluate("""() => performance.getEntriesByType('resource').length""")
        info['resource_count'] = resources
    except Exception:
        info['resource_count'] = -1

    return info


def _log_cookie_snapshot(info: Dict[str, Any]) -> None:
    """将 cookie 快照输出到日志。"""
    tag = info.get('tag', '?')
    url = info.get('current_url', '?')
    _log(f'[cookie_snap:{tag}] URL={url}')

    cookies_by_domain = info.get('cookies_by_domain', {})
    for domain_name, cookies in cookies_by_domain.items():
        if isinstance(cookies, list) and cookies and 'error' not in cookies[0]:
            names = [c['name'] for c in cookies]
            _log(f'[cookie_snap:{tag}] {domain_name}: {len(cookies)} cookies → {names}')
        else:
            _log(f'[cookie_snap:{tag}] {domain_name}: 无 cookie 或获取失败')

    nav = info.get('navigation_entries', [])
    if nav:
        for i, entry in enumerate(nav[:5]):
            _log(f'[cookie_snap:{tag}] nav[{i}]: type={entry.get("type")}, redirects={entry.get("redirectCount")}, '
                 f'startTime={entry.get("startTime"):.0f}ms, name={entry.get("name", "")[:80]}')

    res_count = info.get('resource_count', -1)
    _log(f'[cookie_snap:{tag}] 资源数: {res_count}')


def _log_diagnostic(info: Dict[str, Any]) -> None:
    """将诊断信息输出到日志 + 写 JSON 文件。"""
    tag = info.get('tag', '?')
    url = info.get('url', '?')

    # 日志摘要
    _log(f'[diag:{tag}] URL={url}')

    ad = info.get('anti_detect', {})
    if ad:
        wd = ad.get('webdriver')
        pl = ad.get('plugins', {})
        _log(f'[diag:{tag}] 反检测: webdriver={wd}, plugins.length={pl.get("length") if pl else "?"}, '
             f'chrome_runtime={ad.get("chrome_runtime")}, cdc_leaked={ad.get("cdc_leaked", [])}, '
             f'pw_leaked={ad.get("playwright_leaked", [])}')

    dom = info.get('dom', {})
    if dom:
        _log(f'[diag:{tag}] DOM: body_len={dom.get("body_text_len")}, html_len={dom.get("html_len")}, '
             f'title={dom.get("title", "")!r}, has_login={dom.get("has_login_form")}, '
             f'has_welcome={dom.get("has_welcome")}, has_dialog={dom.get("has_usage_dialog")}')
        preview = dom.get('body_preview', '')
        if preview:
            _log(f'[diag:{tag}] body 前 200 字: {preview[:200]!r}')

    js_err_count = info.get('js_error_count', 0)
    if js_err_count > 0:
        _log(f'[diag:{tag}] JS 错误 ({js_err_count} 条): {info.get("js_errors", [])[:3]}')
    elif js_err_count == -1:
        _log(f'[diag:{tag}] JS 错误收集器未注入')

    ck = info.get('cookies', {})
    if ck:
        _log(f'[diag:{tag}] Cookie(cpquery): {ck.get("count")} 个, has_session={ck.get("has_session")}, names={ck.get("names", [])[:10]}')

    # ★ 全域 cookie 日志
    all_ck = info.get('all_cookies', {})
    if all_ck:
        _log(f'[diag:{tag}] ★ 全域 Cookie: 共 {all_ck.get("total")} 个')
        cp_domains = all_ck.get('cponline_domains', {})
        for d, names in cp_domains.items():
            _log(f'[diag:{tag}] ★   {d}: {names}')

    net = info.get('network', {})
    if net:
        _log(f'[diag:{tag}] 网络: {net.get("total_resources")} 资源, {net.get("suspicious_zero_transfer")} 可疑零传输')

    vis = info.get('visibility', {})
    if vis:
        _log(f'[diag:{tag}] 可见性: hidden={vis.get("hidden")}, state={vis.get("visibilityState")}, iframes={vis.get("iframe_count")}')

    # 写 JSON 文件
    try:
        diag_dir = os.path.join(CNIPA_ROOT, 'output')
        os.makedirs(diag_dir, exist_ok=True)
        diag_path = os.path.join(diag_dir, f'diagnostic_{tag}.json')
        with open(diag_path, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=2, default=str)
        _log(f'[diag:{tag}] 完整诊断已写入: {diag_path}')
    except Exception as exc:
        _log(f'[diag:{tag}] 写诊断文件失败: {exc}')


def _click_sso_login_tab(page: Any, login_type: str) -> None:
    tab_text = LOGIN_TAB_LABELS.get(login_type, '自然人登录')
    try:
        active = page.locator(f'text="{tab_text}"').first
        if active.count() > 0:
            cls = active.evaluate('el => (el.className||"") + (el.parentElement?.className||"")')
            if 'active' in cls or 'is-active' in cls or 'on' in cls:
                return
    except Exception:
        pass
    for sel in (f'.tab-item:has-text("{tab_text}")', f'div:has-text("{tab_text}")'):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=5000)
                time.sleep(0.5)
                return
        except Exception:
            continue
    _click_by_text(page, [tab_text], exact=True)


def _click_sso_submit_button(page: Any) -> None:
    """点击登录页的「登录」按钮。支持 tysf 门户和 cpquery CAS 表单。"""
    selectors = (
        'button.login-btn',
        'button.el-button.login-btn',
        'button.el-button--primary:has-text("登录")',
        'button[type="submit"]',
        'input[type="submit"]',  # cpquery CAS 表单使用 input[type=submit]
    )
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                _log(f'[login] 已点击登录按钮 ({sel})')
                return
        except Exception:
            continue
    # 最终 fallback：在密码框按 Enter
    try:
        pass_input = page.locator('input[type="password"]').first
        if pass_input.count() > 0:
            pass_input.press('Enter')
            _log('[login] 未找到提交按钮，fallback 到 Enter 提交')
            return
    except Exception:
        pass
    raise RuntimeError('未找到登录按钮且无法通过 Enter 提交')


def _is_sso_login_page(page: Any) -> bool:
    url = (page.url or '').lower()
    if any(x in url for x in ('sso.cponline.cnipa.gov.cn', 'tysf.cponline.cnipa.gov.cn')):
        return True
    if '#/user/login' in url or '#/login' in url:
        return True
    try:
        return page.locator('input[type="password"]').count() > 0
    except Exception:
        return False


def _is_cpquery_session_valid(page: Any) -> bool:
    """手工登录成功特征：顶栏「欢迎您」；遮罩「使用说明」也算已进站（非白屏）。"""
    _raise_if_unsafe(page)
    url = (page.url or '').lower()
    if 'cpquery.cponline.cnipa.gov.cn' not in url:
        return False
    if _is_sso_login_page(page):
        return False
    try:
        body = page.inner_text('body') or ''
    except Exception:
        body = ''
    if '安全异常' in body:
        return False
    # 真白屏：URL 对但 body 空 —— 与「使用说明」遮罩不是一回事
    if not body.strip():
        return False
    return any(
        k in body
        for k in (
            '欢迎您',  # 顶栏登录态（用户截图）
            '使用说明',  # 遮罩弹窗（有正文，不是白屏）
            '案件查询',
            '复审无效',
            '中国专利审查信息查询',
            '申请号/专利号',
        )
    )


def _click_by_text(page: Any, texts: List[str], *, exact: bool = False, timeout: int = 3000) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=exact)
            if locator.count() > 0:
                locator.first.click(timeout=timeout)
                return True
        except Exception:
            continue
        try:
            locator = page.locator(f'text={text}')
            if locator.count() > 0:
                locator.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _select_el_option(page: Any, trigger: Any, value: str) -> bool:
    """Element-UI 下拉（el-select）：点触发器 → 弹层中按文本选项 → 校验。

    精确匹配优先，其次包含匹配；未命中则 Esc 收起避免挡住后续操作。
    """
    try:
        trigger.click()
        page.wait_for_timeout(400)
        options = page.locator(
            '.el-select-dropdown:visible .el-select-dropdown__item, '
            '.el-select-dropdown__item:visible, '
            '.el-dropdown-menu:visible li, '
            '.el-popper:visible li'
        )
        n = options.count()
        for i in range(n):
            opt = options.nth(i)
            try:
                if (opt.inner_text() or '').strip() == str(value).strip():
                    opt.click()
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
        for i in range(n):
            opt = options.nth(i)
            try:
                txt = (opt.inner_text() or '').strip()
                if txt and str(value).strip() in txt:
                    opt.click()
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass
        return False
    except Exception:
        return False


def _try_fill_input(page: Any, el: Any, value: str) -> bool:
    """往一个输入框写值；readonly（el-select 触发器）转下拉点选。

    fill 未生效时退化为逐字键入（部分受控组件需要键盘事件）。
    任何失败都不抛异常，返回是否成功。
    """
    try:
        readonly = el.get_attribute('readonly') is not None
    except Exception:
        readonly = False
    if readonly:
        return _select_el_option(page, el, value)
    try:
        el.click()
        el.fill(str(value))
        try:
            filled_now = (el.input_value() or '').strip()
        except Exception:
            filled_now = str(value).strip()
        if filled_now == str(value).strip():
            return True
        try:
            el.click()
            el.fill('')
            typer = getattr(el, 'press_sequentially', None) or getattr(el, 'type', None)
            if typer is None:
                return False
            typer(str(value), delay=60)
            return (el.input_value() or '').strip() == str(value).strip()
        except Exception:
            return False
    except Exception:
        return False


def _type_like_human(page: Any, el: Any, value: str):
    """真实键盘逐字输入 + Tab 失焦，返回输入框最终值（失败返回 None）。

    2026-09-01 手工实测（seach1/result1 截图 OCR 对拍）：站点会在查询时把
    申请号里的小数点自动去掉（200610004173.6 → 2006100041736，其余 13 位不变）。
    fill() 只派发一个 input 事件、不发键盘事件，站点的输入规范化不跑
    （run4 实测 POST body 里还带着小数点）；逐字键入 + Tab 与手工输入等价。
    """
    try:
        el.click()
        el.fill('')
        typer = getattr(el, 'press_sequentially', None) or getattr(el, 'type', None)
        if typer is None:
            return None
        typer(str(value), delay=60)
        page.keyboard.press('Tab')  # 失焦，触发站点 blur/change 规范化
        page.wait_for_timeout(300)
        return (el.input_value() or '').strip()
    except Exception:
        return None


def _fill_by_label(page: Any, labels: List[str], value: str, writer: Any = None) -> str:
    """按标签填值；返回命中方式描述（''=失败，便于联调定位）。

    2026-08-31 联调强化：
    - label → 同容器输入框（Element-UI el-form-item 结构）
    - readonly 输入框（el-select）自动转下拉点选（如「专利类型」）
    - placeholder 兜底（无 label 元素的独立输入框）
    writer(page, el, value) -> bool 可覆盖写入方式（默认 _try_fill_input），
    供 fill_search_filters 做“真实键盘键入”路径复用同一套定位逻辑。
    """
    if not value:
        return ''
    write = writer or _try_fill_input
    for label in labels:
        # 1) label 元素 → 同容器 / 邻近输入框
        try:
            row = page.locator(f'label:has-text("{label}")').first
            if row.count() == 0:
                row = page.get_by_text(label, exact=False).first
            if row.count() > 0:
                container = row.locator('xpath=ancestor::*[self::div or self::li or self::tr][1]')
                inputs = container.locator('input, textarea')
                if inputs.count() > 0:
                    if write(page, inputs.first, value):
                        return f'label-input:{label}'
                sibling = row.locator('xpath=following::input[1]')
                if sibling.count() > 0:
                    if write(page, sibling.first, value):
                        return f'label-following-input:{label}'
        except Exception:
            pass
        # 2) placeholder 兜底
        try:
            el = page.locator(f'input[placeholder*="{label}"]').first
            if el.count() > 0:
                if write(page, el, value):
                    return f'placeholder:{label}'
        except Exception:
            continue
    return ''


def dismiss_cpquery_usage_dialog(page: Any) -> None:
    """关闭登录后「使用说明」遮罩（点确定），否则挡住左侧菜单与检索区。"""
    # 等弹窗出现（登录回跳后常延迟几百 ms～数秒）
    try:
        page.get_by_text('使用说明', exact=False).first.wait_for(state='visible', timeout=10_000)
    except Exception:
        pass

    clicked = False
    for sel in (
        '.el-dialog >> button:has-text("确定")',
        '.el-message-box >> button:has-text("确定")',
        '[class*="dialog"] button:has-text("确定")',
        '[class*="modal"] button:has-text("确定")',
        'button:has-text("确定")',
    ):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=800):
                el.click(timeout=3000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        _click_by_text(page, ['确定', '我知道了', '关闭'], exact=False)

    time.sleep(0.5)
    try:
        page.get_by_text('使用说明', exact=False).first.wait_for(state='hidden', timeout=5000)
    except Exception:
        pass


def expand_more_search_items(page: Any) -> None:
    if _click_by_text(page, ['展开更多搜索项', '展开更多', '更多搜索项'], exact=False):
        time.sleep(0.5)
        return
    for selector in (
        'text=展开更多搜索项',
        '[class*="expand"]',
        'a:has-text("展开")',
        'span:has-text("展开")',
    ):
        try:
            el = page.locator(selector).first
            if el.count() > 0 and el.is_visible():
                el.click()
                time.sleep(0.5)
                return
        except Exception:
            continue


def select_review_invalid_menu(page: Any) -> bool:
    """点击「复审无效审查决定查询」tab，等待并验证决定号/案件编号字段出现。

    返回是否验证到位；未到位时调用方应拒绝提交查询（防止在案件查询 tab 查错库）。
    2026-09-01 联调：该站是 Quasar q-tab（div.q-tab），不是 el-menu 左侧菜单。
    """

    def _try_click() -> bool:
        for selector in (
            '.q-tab:has-text("复审无效")',
            '[role="tab"]:has-text("复审无效")',
            '.el-tabs__item:has-text("复审无效")',
            'a:has-text("复审无效审查决定查询")',
            'li:has-text("复审无效审查决定查询")',
        ):
            try:
                el = page.locator(selector).first
                if el.count() > 0 and el.is_visible(timeout=1500):
                    el.click(timeout=5000)
                    return True
            except Exception:
                continue
        return _click_by_text(page, ['复审无效审查决定查询', '复审无效审查决定', '审查决定查询'], exact=False)

    for attempt in range(2):
        if attempt == 1:
            _log('[menu] 未到位，重试点击一次')
        if not _try_click():
            continue
        for _ in range(12):  # 最多 ~7s 等 SPA 切换
            page.wait_for_timeout(600)
            if _search_form_present(page):
                _log('[menu] 已进入复审无效审查决定查询（已验证：tab 激活 + 决定号/案件编号可见）')
                return True
    _log('[menu] ⚠ 未验证进入复审无效检索页（决定号/案件编号字段未出现）')
    return False


def select_library_type(page: Any, library_type: str) -> bool:
    """选择复审/无效决定库；点击后验证选中态（radio/tab 的 active/checked）。

    2026-09-01 联调实测 DOM：Quasar 单选是
    <div role="radio" aria-label="无效决定库" aria-checked="true|false">。
    """
    label = '复审决定库' if library_type == 'reexam' else '无效决定库'
    clicked = False
    # Quasar：直接点 role=radio 容器（比文本匹配稳，避免点到别处同名文本）
    try:
        radio = page.locator(f'[role="radio"][aria-label="{label}"]').first
        if radio.count() > 0:
            radio.click(timeout=4000)
            clicked = True
    except Exception:
        clicked = False
    if not clicked:
        clicked = _click_by_text(page, [label], exact=False)
    if not clicked:
        try:
            page.locator(f'text={label}').first.click(timeout=3000)
            clicked = True
        except Exception:
            _log(f'[warn] 未能明确选择库类型 {label}，继续尝试查询')
            return False
    time.sleep(0.4)
    active = False
    for sel in (
        # Quasar 真实写法（实测 aria-checked）
        f'[role="radio"][aria-checked="true"][aria-label="{label}"]',
        f'[class*="active"]:has-text("{label}")',
        f'[class*="checked"]:has-text("{label}")',
        f'[aria-selected="true"]:has-text("{label}")',
        'input[type="radio"]:checked',
    ):
        try:
            if page.locator(sel).count() > 0:
                active = True
                break
        except Exception:
            continue
    _log(f'[library] 选库 {label} → {"已验证选中" if active else "点击完成但未验证到选中态"}')
    return active


def fill_search_filters(page: Any, filters: Dict[str, Any]) -> Dict[str, Any]:
    """按 FIELD_LABEL_MAP 逐项填写筛选条件，返回逐项报告（联调定位用）。

    报告形如 {'application_no': {'value': 原样输入, 'final': 站点规范化后输入框值,
    'normalized': 本地规范化值, 'ok': True, 'how': 'label-input:申请号'}}
    how 为空串表示控件根本没定位到（次日凭 elements.txt 修标签别名）。
    申请号走真实键盘逐字键入（让站点自己去小数点），其余字段普通填写。
    """
    report: Dict[str, Any] = {}
    for key, labels in FIELD_LABEL_MAP.items():
        val = filters.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if not text:
            continue
        if key == 'application_no':
            raw_text = text
            norm = normalize_application_no_for_search(text)
            box: Dict[str, str] = {}

            def _app_no_writer(pg: Any, el: Any, val_in: str) -> bool:
                final = _type_like_human(pg, el, val_in)
                if final is None:
                    return False
                box['typed_final'] = final
                if final != norm:
                    # 合成键盘也可能绕过站点 mask：兜底直接改填规范化值
                    _log(f'[filters] application_no 站点规范化结果 {final!r} ≠ 预期 {norm!r}，兜底改填')
                    try:
                        el.click()
                        el.fill('')
                        el.fill(norm)
                        pg.wait_for_timeout(200)
                        box['typed_final'] = (el.input_value() or '').strip() or norm
                    except Exception:
                        box['typed_final'] = norm
                return True

            how = _fill_by_label(page, labels, raw_text, writer=_app_no_writer)
            if how:
                report[key] = {
                    'value': raw_text,
                    'final': box.get('typed_final', ''),
                    'normalized': norm,
                    'ok': True,
                    'how': how,
                }
                _log(
                    f"[filters] application_no 原样键入 {raw_text} → "
                    f"输入框最终 {report[key]['final']}（{how}）"
                )
                continue
            _log(f'[filters] application_no 键入路径失败，退回普通填写规范化值 {norm}')
            text = norm
        how = _fill_by_label(page, labels, text)
        report[key] = {'value': text, 'ok': bool(how), 'how': how}
        _log(f'[filters] {key}={text} → {"OK" if how else "失败"}（{how or "未定位到控件"}）')
    if report:
        failed = [k for k, v in report.items() if not v['ok']]
        _log(f'[filters] 填写完成：{len(report)} 项，失败 {len(failed)} 项' + (f'：{failed}' if failed else ''))
    return report


def wait_search_results(page: Any, timeout_ms: int = 20_000) -> str:
    """点查询后等结果渲染完成。返回 'found' / 'empty' / 'timeout'。

    - 加载遮罩消失为前置条件（Element-UI .el-loading-mask + Quasar q-inner-loading/q-linear-progress）
    - 出现表格行/结果卡片 → found
    - 出现「暂无数据 / 共0条」→ empty（须 ≥1.2s 后才判 empty，避开加载间隙）

    2026-09-01 联调：结果区默认空态就带「共0条」，本函数只能判断“渲染完成后的状态”；
    “查询是否真的发出”由调用方的网络门禁（expect_response）保证。
    """
    start = time.time()
    deadline = start + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if page.locator(
                '.el-loading-mask:visible, .q-inner-loading:visible, '
                '.q-table__progress:visible, .q-linear-progress:visible, '
                'button.q-btn:has(.q-spinner):visible'
            ).count() > 0:
                page.wait_for_timeout(500)
                continue
        except Exception:
            pass
        try:
            rows = page.locator(
                '.el-table__body tr:visible, table tbody tr:visible, '
                '.result-item:visible, [class*="list-item"]:visible'
            )
            if rows.count() > 0:
                return 'found'
        except Exception:
            pass
        # 2026-09-01 手工实测：真结果提示语是「共检索到1条结果」（卡片式布局，
        # 卡片选择器未知前，总数文案是最可靠的 found 信号）；N≥1 才算。
        try:
            body = page.inner_text('body') or ''
            if re.search(r'共检索到\s*[1-9]\d*\s*条', body):
                return 'found'
        except Exception:
            pass
        if time.time() - start > 1.2:
            try:
                body = page.inner_text('body') or ''
                if re.search(r'暂无数据|无数据|没有符合|未查询到|共\s*0\s*条|共检索到\s*0\s*条', body):
                    return 'empty'
            except Exception:
                pass
        page.wait_for_timeout(600)
    return 'timeout'


def visible_table_headers(page: Any) -> List[str]:
    """抓当前结果表的表头列名（联调时核对列名↔12 列映射）。"""
    try:
        tables = page.locator('table')
        for ti in range(tables.count()):
            t = tables.nth(ti)
            try:
                header_cells = t.locator('thead tr th, tr:first-child th')
                if header_cells.count() > 0:
                    headers = [h.strip() for h in header_cells.all_inner_texts() if h.strip()]
                    if headers:
                        return headers
            except Exception:
                continue
    except Exception:
        pass
    return []


def extract_total_text(page: Any) -> str:
    """抓总数提示（核对服务端总数 vs 本地解析条数）。

    两种文案都认（2026-09-01 手工实测）：查询后「共检索到1条结果」；
    未查询时的默认空态是「共0条」——两者共存可区分“响应真被应用”还是
    “页面停在默认空态”。
    """
    try:
        body = page.inner_text('body') or ''
        m = re.search(r'共检索到\s*([0-9,，]+)\s*条', body)
        if m:
            return f'共检索到{m.group(1)}条'
        m = re.search(r'共\s*([0-9,，]+)\s*条', body)
        if m:
            return f'共{m.group(1)}条'
    except Exception:
        pass
    return ''


def _search_form_present(page: Any) -> bool:
    """探测当前是否已在「复审无效审查决定查询」检索页（tab 已激活）。

    2026-09-01 联调教训：/chinesepatent/index 首页（案件查询 tab）的 tab 栏
    文本也含「复审无效审查决定查询」「申请号」「查询」，仅凭 body 文本必误判。
    硬条件改为：
    1) 复审无效特有字段「决定号」与「案件编号」同时可见（案件查询 tab 没有）；
    2) 且「复审/无效」tab 处于激活态（q-tab--active / aria-selected / el-tabs）。
    """
    try:
        if 'cpquery.cponline.cnipa.gov.cn' not in (page.url or '').lower():
            return False
        # 字段判据用完整 DOM（page.content()）：决定号可能在「展开更多」折叠区，
        # inner_text 取不到隐藏节点。错页（案件查询 tab）实测：决定号 0 次。
        html = page.content() or ''
        if '决定号' not in html or '案件编号' not in html:
            return False
        for sel in (
            '.q-tab--active',
            '[role="tab"][aria-selected="true"]',
            '.el-tabs__item.is-active',
            '.ant-tabs-tab-active',
        ):
            try:
                tabs = page.locator(sel)
                for i in range(min(tabs.count(), 30)):
                    tab = tabs.nth(i)
                    text = (tab.inner_text() or '').strip()
                    if not text:
                        try:
                            text = (tab.get_attribute('title') or '').strip()
                        except Exception:
                            text = ''
                    if ('复审' in text or '无效' in text) and '查询' in text:
                        return True
            except Exception:
                continue
        # 兜底：特有字段齐全且出现合议组/代理机构（复审无效展开区字段）也算到位
        return '合议组' in html or '代理机构' in html
    except Exception:
        return False


def build_detail_url(
    *,
    detail_url: Optional[str] = None,
    zhuanlisqh: Optional[str] = None,
    anjianbh: Optional[str] = None,
    search_type: str = '8',
) -> str:
    if detail_url and detail_url.strip():
        return detail_url.strip()
    if zhuanlisqh and anjianbh:
        from urllib.parse import urlencode

        qs = urlencode(
            {
                'zhuanlisqh': zhuanlisqh.strip(),
                'anjianbh': anjianbh.strip(),
                'searchType': search_type.strip() or '8',
            }
        )
        return f'https://cpquery.cponline.cnipa.gov.cn/detail/index?{qs}'
    raise ValueError('缺少 detail_url 或 zhuanlisqh+anjianbh')


def normalize_application_no(application_no: str) -> str:
    raw = (application_no or '').strip()
    raw = raw.upper()
    if raw.startswith('CN'):
        return raw
    return f'CN{raw}'


def normalize_application_no_for_search(application_no: str) -> str:
    """检索输入框格式的申请号：去 CN/ZL 前缀、去点/空格/横线。

    2026-09-01 联调实测：站点「申请号/专利号」placeholder 示例为「例如: 2010101995057」
    （13 位纯数字、无校验位点号）。填 200610004173.6 时服务端按字面匹配返回 共0条。
    （detail URL 的 zhuanlisqh 仍用 normalize_application_no 的 CN 前缀格式，用途不同。）
    """
    raw = (application_no or '').strip().upper()
    raw = re.sub(r'^(CN|ZL)', '', raw)
    raw = re.sub(r'[\s.\-]', '', raw)
    return raw


def parse_label_value_pairs(page: Any) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        body = page.inner_text('body') or ''
    except Exception:
        body = ''

    patterns = [
        r'([^\n：:]{1,20})[：:]\s*([^\n]+)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, body):
            k = m.group(1).strip()
            v = m.group(2).strip()
            if k and v and len(k) <= 20:
                result[k] = v

    for row in page.locator('tr').all():
        try:
            cells = row.locator('td, th').all_inner_texts()
            if len(cells) >= 2:
                k = cells[0].strip().rstrip('：:')
                v = cells[1].strip()
                if k and v:
                    result[k] = v
        except Exception:
            continue

    for dt in page.locator('dt, .label, .field-label').all():
        try:
            k = dt.inner_text().strip().rstrip('：:')
            dd = dt.locator('xpath=following-sibling::*[1]').first
            if dd.count() > 0:
                v = dd.inner_text().strip()
                if k and v:
                    result[k] = v
        except Exception:
            continue

    return result


def map_detail_fields(pairs: Dict[str, str]) -> Dict[str, Any]:
    def pick(*keys: str) -> str:
        for k in keys:
            if k in pairs and pairs[k]:
                return pairs[k]
            for pk, pv in pairs.items():
                if any(x in pk for x in keys):
                    return pv
        return ''

    return {
        'case_no': pick('案件编号'),
        'decision_no': pick('决定号'),
        'decision_date': pick('决定日', '发文日'),
        'invention_title': pick('发明名称', '发明创造名称', '名称'),
        'petitioner': pick('无效宣告请求人', '复审请求人', '请求人'),
        'patentee': pick('专利权人'),
        'application_no': pick('申请号', '专利号', '申请号/专利号'),
        'request_date': pick('无效宣告请求日', '复审请求日', '请求日'),
        'legal_basis': pick('法律依据'),
        'decision_summary': pick('决定要点'),
        'decision_result': pick('审查结论', '决定结果'),
        'panel_members': pick('合议组成员', '合议组'),
        'issue_date': pick('发文日'),
    }


def _extract_detail_link_from_href(href: str) -> str:
    if not href:
        return ''
    if href.startswith('/'):
        return f'https://cpquery.cponline.cnipa.gov.cn{href}'
    if href.startswith('http'):
        return href
    return ''


def _parse_detail_params(detail_url: str) -> Dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    out: Dict[str, str] = {}
    if not detail_url:
        return out
    qs = parse_qs(urlparse(detail_url).query)
    for key in ('zhuanlisqh', 'anjianbh', 'searchType'):
        if key in qs and qs[key]:
            out[key] = qs[key][0]
    return out


def _normalize_list_item(raw: Dict[str, str], *, detail_url: str = '', title: str = '') -> Dict[str, Any]:
    normalized = map_detail_fields(raw)
    if title and not normalized.get('invention_title'):
        normalized['invention_title'] = title
    normalized.update(
        {
            'raw_row': raw,
            'detail_url': detail_url,
            'case_no': normalized.get('case_no') or raw.get('案件编号', ''),
            'decision_no': normalized.get('decision_no') or raw.get('决定号', ''),
            'application_no': normalized.get('application_no')
            or raw.get('申请号', raw.get('专利号', raw.get('申请号/专利号', ''))),
            'patentee': normalized.get('patentee') or raw.get('专利权人', ''),
            'decision_result': normalized.get('decision_result') or raw.get('审查结论', ''),
            'petitioner': normalized.get('petitioner')
            or raw.get('无效宣告请求人', raw.get('复审请求人', raw.get('无效请求人', ''))),
            'issue_date': normalized.get('issue_date') or raw.get('发文日', ''),
            'ipc_class': raw.get('分类号', ''),
            'application_date': raw.get('申请日', ''),
        }
    )
    params = _parse_detail_params(detail_url)
    normalized.update({k: v for k, v in params.items() if v})
    return normalized


def parse_card_results(page: Any, max_items: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    card_selectors = (
        '.result-list .result-item',
        '.result-item',
        '.list-item',
        '[class*="result"] [class*="item"]',
        '.el-card',
    )
    for selector in card_selectors:
        cards = page.locator(selector)
        count = cards.count()
        if count == 0:
            continue
        for i in range(min(count, max_items)):
            card = cards.nth(i)
            try:
                text = card.inner_text() or ''
            except Exception:
                continue
            if len(text.strip()) < 10:
                continue
            raw: Dict[str, str] = {}
            for m in re.finditer(r'([^\n：:]{1,20})[：:]\s*([^\n]+)', text):
                k = m.group(1).strip()
                v = m.group(2).strip()
                if k and v:
                    raw[k] = v

            detail_url = ''
            title = ''
            try:
                a = card.locator('a[href*="detail"], a[href*="zhuanlisqh"], a').first
                if a.count() > 0:
                    href = a.get_attribute('href') or ''
                    detail_url = _extract_detail_link_from_href(href)
                    title = (a.inner_text() or '').strip()
            except Exception:
                pass

            if raw or detail_url:
                items.append(_normalize_list_item(raw, detail_url=detail_url, title=title))
        if items:
            break

    if items:
        return items[:max_items]

    body = ''
    try:
        body = page.inner_text('body') or ''
    except Exception:
        return items

    blocks = re.split(r'(?=案件编号[：:])', body)
    for block in blocks[1:]:
        if len(items) >= max_items:
            break
        raw = {}
        for m in re.finditer(r'([^\n：:]{1,20})[：:]\s*([^\n]+)', block):
            k = m.group(1).strip()
            v = m.group(2).strip()
            if k and v:
                raw[k] = v
        # 2026-09-01 联调：检索页筛选区也有「案件编号：」标签，切分后剩下的全是
        # 筛选字段标签（值形如「发文日：」「主审员：」「请选择」），不是结果行。
        # 防护：案件编号的值必须像编号（含字母/数字），且不得以「：」结尾。
        case_val = (raw.get('案件编号') or '').strip()
        if not re.search(r'[0-9A-Za-z]', case_val) or case_val.endswith(('：', ':')):
            continue
        items.append(_normalize_list_item(raw))
    return items[:max_items]


def parse_list_table(page: Any, max_items: int) -> List[Dict[str, Any]]:
    card_items = parse_card_results(page, max_items)
    if card_items:
        return card_items

    items: List[Dict[str, Any]] = []
    tables = page.locator('table')
    count = tables.count()
    for ti in range(count):
        table = tables.nth(ti)
        headers: List[str] = []
        header_cells = table.locator('thead tr th, tr:first-child th, tr:first-child td')
        if header_cells.count() > 0:
            headers = [h.strip() for h in header_cells.all_inner_texts()]
        rows = table.locator('tbody tr, tr')
        for ri in range(rows.count()):
            if len(items) >= max_items:
                break
            row = rows.nth(ri)
            cells = [c.strip() for c in row.locator('td').all_inner_texts()]
            if len(cells) < 2:
                continue
            if headers and len(headers) == len(cells):
                item = dict(zip(headers, cells))
            else:
                item = {f'col_{i}': v for i, v in enumerate(cells)}

            link = ''
            try:
                a = row.locator('a[href]').first
                if a.count() > 0:
                    href = a.get_attribute('href') or ''
                    link = _extract_detail_link_from_href(href)
            except Exception:
                pass

            normalized = _normalize_list_item(item if isinstance(item, dict) else {}, detail_url=link)
            if any(normalized.get(k) for k in ('case_no', 'application_no', 'detail_url')):
                items.append(normalized)
        if items:
            break
    return items


def save_json_output(data: Dict[str, Any], output_path: Optional[str]) -> str:
    if not output_path:
        out_dir = os.path.join(CNIPA_ROOT, 'output', 'cnipa_cache')
        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.md5(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        output_path = os.path.join(out_dir, f'cnipa_{digest}.json')
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_path


def save_credentials(
    *,
    username: str,
    password: str,
    login_type: str = 'natural',
    credentials_path: Optional[str] = None,
) -> str:
    login_type = (login_type or 'natural').strip().lower()
    if login_type not in LOGIN_TAB_LABELS:
        login_type = 'natural'
    username = username.strip()
    password = password.strip()
    if not username or not password:
        raise ValueError('username 与 password 均不能为空')

    path = credentials_path
    if not path:
        path = resolve_credentials_path({})
    if not path:
        path = os.path.abspath(
            os.path.join(CNIPA_ROOT, '..', '..', 'Skills', 'patent-reexam-invalid-report', 'secrets', 'cnipa_credentials.json')
        )
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'login_type': login_type,
        'username': username,
        'password': password,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _log(f'[info] 凭证已保存: {path}')
    return path


@dataclass
class BrowserSession:
    """Playwright 浏览器会话。detach=True 时（CDP 接管已有 Chrome）退出时不关浏览器。"""

    browser: Any | None
    context: Any
    detach: bool = False
    mode: str = 'ephemeral'


def resolve_cdp_url() -> str:
    return os.environ.get('CNIPA_CDP_URL', '').strip()


def resolve_user_data_dir(session_dir: str) -> str:
    env = os.environ.get('CNIPA_USER_DATA_DIR', '').strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(session_dir, 'chrome_profile')


def get_work_page(context: Any) -> Any:
    """CDP 模式下优先复用已打开的 cpquery/pss 标签页。"""
    try:
        for page in context.pages:
            url = (page.url or '').lower()
            if any(x in url for x in ('cpquery.cponline.cnipa.gov.cn', 'pss-system.cponline.cnipa.gov.cn')):
                return page
    except Exception:
        pass
    return context.new_page()


def close_browser_session(session: BrowserSession) -> None:
    if session.detach:
        return
    try:
        session.context.close()
    except Exception:
        pass
    if session.browser:
        try:
            session.browser.close()
        except Exception:
            pass


def launch_browser_session(
    playwright: Any,
    *,
    headless: bool,
    storage_state: Optional[str],
    session_dir: Optional[str] = None,
) -> BrowserSession:
    cdp_url = resolve_cdp_url()
    if cdp_url:
        _log(f'[info] 接管已有 Chrome (CDP): {cdp_url}')
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return BrowserSession(browser=browser, context=context, detach=True, mode='cdp')

    launch_args = [
        '--disable-blink-features=AutomationControlled',
        '--no-sandbox',
        '--disable-dev-shm-usage',
    ]
    # 去掉 Playwright 默认 --enable-automation，减轻「正受到自动测试软件控制」横幅与风控
    ignore_default_args = ['--enable-automation']
    use_persistent = os.environ.get('CNIPA_PERSISTENT_PROFILE', '1').strip() != '0'
    profile_dir = resolve_user_data_dir(session_dir or os.path.join(CNIPA_ROOT, 'output', 'cnipa_session'))

    if use_persistent:
        os.makedirs(profile_dir, exist_ok=True)
        persistent_kwargs: Dict[str, Any] = {
            'user_data_dir': profile_dir,
            'headless': headless,
            'args': launch_args,
            'ignore_default_args': ignore_default_args,
            'viewport': {'width': 1440, 'height': 900},
            'locale': 'zh-CN',
        }
        # ★ v9: channel=chrome 默认关闭。本机真实 Chrome 主版本实际为 104（135 为
        # 未完成更新的残留目录），过于古老本身可疑；Playwright 自带 Chromium 145
        # headed 实测 UA/brands/sec-ch-ua 三者完全自洽且版本现代。
        # 需要用本机 Chrome 时显式设 CNIPA_USE_CHROME=1。
        if os.environ.get('CNIPA_USE_CHROME', '0') == '1':
            persistent_kwargs['channel'] = 'chrome'
        try:
            context = playwright.chromium.launch_persistent_context(**persistent_kwargs)
        except Exception:
            persistent_kwargs.pop('channel', None)
            context = playwright.chromium.launch_persistent_context(**persistent_kwargs)
        # ★ v10: 默认零注入（原生指纹已实测自洽；伪装脚本反而可能因拦截器语义被识破）
        if _hide_script_enabled():
            context.add_init_script(_HIDE_AUTOMATION_SCRIPT)
            context.add_init_script(_JS_ERROR_COLLECTOR)
        _log(f'[info] 使用持久化 Chrome 配置: {profile_dir}')
        _log(f'[info] v10 启动（持久化）：headless={headless} channel={persistent_kwargs.get("channel", "bundled-chromium")} 零注入={not _hide_script_enabled()}')
        return BrowserSession(browser=None, context=context, detach=False, mode='persistent')

    launch_kwargs: Dict[str, Any] = {
        'headless': headless,
        'args': launch_args,
        'ignore_default_args': ignore_default_args,
    }
    # ★ v9: 同 persistent 路径，默认用自带 Chromium（145，自洽且现代）
    if os.environ.get('CNIPA_USE_CHROME', '0') == '1':
        launch_kwargs['channel'] = 'chrome'
    try:
        browser = playwright.chromium.launch(**launch_kwargs)
    except Exception:
        launch_kwargs.pop('channel', None)
        browser = playwright.chromium.launch(**launch_kwargs)
    context_kwargs: Dict[str, Any] = {
        'viewport': {'width': 1440, 'height': 900},
        'locale': 'zh-CN',
        # ★ v9: 删除硬编码 user_agent 覆写。
        # 实测：UA 覆写使 Playwright 走 Emulation.setUserAgentOverride（不带
        # userAgentMetadata），导致 navigator.userAgentData.brands=[] 且请求头
        # 不再携带 sec-ch-ua —— UA 声称 131 而 brands/引擎真实版本不一致，
        # 属于一查即中的机器人铁证。让浏览器发送原生自洽的 UA 与客户端提示。
    }
    if storage_state and os.path.isfile(storage_state):
        context_kwargs['storage_state'] = storage_state
    context = browser.new_context(**context_kwargs)
    # ★ v10: 默认零注入（同 persistent 路径）
    if _hide_script_enabled():
        context.add_init_script(_HIDE_AUTOMATION_SCRIPT)
        context.add_init_script(_JS_ERROR_COLLECTOR)
    _log(f'[info] v10 启动（临时上下文）：headless={headless} channel={launch_kwargs.get("channel", "bundled-chromium")} 零注入={not _hide_script_enabled()}')
    return BrowserSession(browser=browser, context=context, detach=False, mode='ephemeral')


def launch_browser_context(playwright: Any, *, headless: bool, storage_state: Optional[str]):
    """兼容旧调用；新代码请用 launch_browser_session。"""
    session = launch_browser_session(
        playwright,
        headless=headless,
        storage_state=storage_state,
    )
    return session.browser, session.context


def save_session_state(context: Any, session_file: str, session: BrowserSession) -> None:
    if session.detach:
        return
    try:
        context.storage_state(path=session_file)
    except Exception as exc:
        _log(f'[warn] 保存 session 失败: {exc}')


def _is_cpquery_own_cas_form(page: Any) -> bool:
    """检测当前页面是否为 cpquery 自己的 CAS 登录表单（form action = /chinesepatent/index）。

    与门户 SSO 登录页不同，cpquery 的 CAS 表单直接提交到 cpquery 自身。
    """
    try:
        action = page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const f of forms) {
                if (f.action && f.action.includes('/chinesepatent/')) return f.action;
            }
            // 也检查 input[type=submit] 的存在（cpquery CAS 表单特征）
            const submitInputs = document.querySelectorAll('input[type="submit"]');
            if (submitInputs.length > 0 && location.hostname === 'cpquery.cponline.cnipa.gov.cn') {
                return 'cpquery-own:' + location.href;
            }
            return '';
        }""")
        if action:
            _log(f'[login] 检测到 cpquery 自有 CAS 表单: {action}')
            return True
    except Exception:
        pass
    return False


def _submit_cpquery_cas_form(page: Any) -> None:
    """直接提交 cpquery 自有 CAS 表单（绕过 JS 点击拦截器）。

    使用 form.submit() 绕过按钮 click 事件处理器，
    但仍触发表单的 submit 事件（如果服务器端有验证码等逻辑仍可工作）。
    """
    submitted = page.evaluate("""() => {
        const forms = document.querySelectorAll('form');
        for (const f of forms) {
            if (f.action && f.action.includes('/chinesepatent/')) {
                f.submit();
                return 'form.submit:' + f.action;
            }
        }
        // fallback: 找 input[type=submit] 并 click
        const submitInput = document.querySelector('input[type="submit"]');
        if (submitInput) {
            submitInput.click();
            return 'input.click:' + submitInput.value;
        }
        return '';
    }""")
    if submitted:
        _log(f'[login] cpquery CAS 表单已直接提交: {submitted}')
    else:
        _log('[login] 未找到 cpquery CAS 表单，fallback 到 Enter 提交')
        pass_input = page.locator('input[type="password"]').first
        if pass_input.count() > 0:
            pass_input.press('Enter')


def _cpquery_body_empty(page: Any) -> bool:
    try:
        body = (page.inner_text('body') or '').strip()
    except Exception:
        return True
    return len(body) < 40


def _clear_cpquery_antibot_cookies(page: Any) -> None:
    """清除 cpquery 域的反爬 cookie（dX1xbeyMT58WO/P），让 412 挑战重新运行。

    问题：412 挑战 JS 设置的 cookie 可能编码了「检测失败」状态，
    后续请求直接返回 400 而不再给挑战机会。
    解决：每次 goto cpquery 前清除这些 cookie，确保获得全新的挑战。
    """
    try:
        cookies = page.context.cookies('https://cpquery.cponline.cnipa.gov.cn')
        antibot_names = {'dX1xbeyMT58WO', 'dX1xbeyMT58WP'}
        to_clear = [c for c in cookies if c.get('name') in antibot_names]
        if to_clear:
            for c in to_clear:
                # ★ 用 cookie 自身的 domain 属性过滤（可能是 host-only 也可能是 .cponline.cnipa.gov.cn），
                #   硬编码精确域名会因 domain 属性不匹配而漏删
                try:
                    page.context.clear_cookies(
                        name=c['name'],
                        domain=c.get('domain') or 'cpquery.cponline.cnipa.gov.cn',
                    )
                except Exception:
                    # 兜底：仅按 name 清（dX1xbeyMT58WO/P 是随机名，全局唯一）
                    page.context.clear_cookies(name=c['name'])
            # 复核是否真的清掉了
            remaining = [
                c2['name'] for c2 in page.context.cookies('https://cpquery.cponline.cnipa.gov.cn')
                if c2.get('name') in antibot_names
            ]
            if remaining:
                _log(f'[cookie] ★ 反爬 cookie 清除后仍残留: {remaining}')
            else:
                _log(f'[cookie] ★ 已清除 {len(to_clear)} 个反爬 cookie: {[c["name"] for c in to_clear]}（复核无残留）')
        else:
            _log('[cookie] cpquery 域无反爬 cookie 需要清除')
    except Exception as e:
        _log(f'[cookie] 清除反爬 cookie 失败（非致命）: {e}')


def _login_form_visible(page: Any) -> bool:
    try:
        return page.locator('input[placeholder*="手机"], input[type="password"]').count() > 0
    except Exception:
        return False


def _is_typortal_page(page: Any) -> bool:
    u = (page.url or '').lower()
    return 'tysfjk.cponline.cnipa.gov.cn' in u or '/portal/' in u


def _is_cponline_public_home(page: Any) -> bool:
    """cponline 公开首页（顶栏有登录/注册，非 cpquery 业务态）。"""
    u = (page.url or '').lower()
    if 'cponline.cnipa.gov.cn' not in u:
        return False
    if 'cpquery' in u or 'tysf' in u or 'tysfjk' in u:
        return False
    try:
        body = page.inner_text('body') or ''
        return '登录' in body and '注册' in body
    except Exception:
        return False


def _persist_cpquery_redirect(page: Any) -> None:
    """登录提交前写入 cpquery 回跳地址，供 tysf 前端/SSO 读取。"""
    try:
        page.evaluate(
            """(target) => {
                const keys = ['redirectUrl', 'redirect', 'goto', 'returnUrl', 'service'];
                for (const k of keys) {
                    try { sessionStorage.setItem(k, target); } catch (e) {}
                    try { localStorage.setItem(k, target); } catch (e) {}
                }
            }""",
            CPQUERY_INDEX,
        )
    except Exception as exc:
        _log(f'[login] 写入 redirect 失败: {exc}')


def _click_cponline_cpquery_nav(page: Any) -> bool:
    """专利业务办理系统顶栏「专利审查信息查询」→ cpquery（带换票）。"""
    for text in ('专利审查信息查询', '审查信息查询'):
        try:
            for sel in (
                f'nav a:has-text("{text}")',
                f'a:has-text("{text}")',
                f'[role="tab"]:has-text("{text}")',
            ):
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=1500):
                    _log(f'[login] cponline 导航: {text}')
                    el.click(timeout=8000)
                    page.wait_for_timeout(3000)
                    return True
            el = page.get_by_text(text, exact=False).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                _log(f'[login] cponline 导航: {text}')
                el.click(timeout=8000)
                page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    return False


def _wait_cpquery_valid(page: Any, *, rounds: int = 24, ms: int = 500) -> bool:
    for _ in range(rounds):
        _raise_if_unsafe(page)
        if _is_cpquery_session_valid(page):
            return True
        page.wait_for_timeout(ms)
    return False


def _enter_cpquery_after_sso(page: Any) -> Any:
    """滑块通过后进入 cpquery 有效页（v5 精简版）。

    对齐手工链路：cpquery → SSO → 滑块 → 等回跳 cpquery
    禁止：门户绕行（「返回系统」/ cponline 导航）

    路径：
      1) 等自然回跳 cpquery（40 轮 × 500ms = 20s）
      2) 落到门户 → 直接 goto cpquery → 等验证（12 轮）
      3) cpquery 白屏 → reload 一次 → 等验证（30 轮）
      4) 全失败 → 报错 + 附带 cookie 快照诊断
    """
    try:
        page.wait_for_function(
            """() => {
                const u = location.href || '';
                return !u.includes('/user/login') && !u.includes('#/login');
            }""",
            timeout=45_000,
        )
    except Exception:
        pass
    # SSO 通过后等待，让 session cookie 传播到 cpquery 域
    page.wait_for_timeout(2500)
    _raise_if_unsafe(page)

    if _is_sso_login_page(page):
        raise RuntimeError(f'登录未成功，仍停留在登录页：{page.url}')

    _persist_cpquery_redirect(page)

    # 1) 自然回跳 cpquery
    if _wait_cpquery_valid(page, rounds=40):
        _log(f'[login] 已自然进入 cpquery（{page.url}）')
        return page

    # 2) 落到门户 → 直接 goto cpquery（不再走「返回系统」绕行）
    if _is_typortal_page(page):
        _log(f'[login] 落在统一门户（{page.url}），SSO service 不生效，直接 goto cpquery')
        # ★ 捕获门户页面的导航链接（诊断门户→cpquery 的认证路径）
        try:
            _portal_links = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const relevant = links.filter(a => {
                    const h = a.href || '';
                    return h.includes('cpquery') || h.includes('cponline') ||
                           h.includes('chinesepatent') || h.includes('专利') ||
                           a.textContent.includes('专利') || a.textContent.includes('查询');
                });
                return relevant.map(a => ({
                    text: (a.textContent || '').trim().substring(0, 50),
                    href: a.href,
                    onclick: a.getAttribute('onclick') || '',
                })).slice(0, 20);
            }""")
            _log(f'[portal-nav] ★ 门户中与 cpquery/专利 相关的链接 ({len(_portal_links)} 条):')
            for lnk in (_portal_links or []):
                _log(f'[portal-nav]   text={lnk.get("text")!r}, href={lnk.get("href", "")[:100]}')
            # 写门户导航到文件
            import json as _json
            _nav_path = os.path.join(CNIPA_ROOT, 'output', 'portal_nav_links.json')
            with open(_nav_path, 'w', encoding='utf-8') as _nf:
                _json.dump(_portal_links, _nf, ensure_ascii=False, indent=2)
        except Exception as _pe:
            _log(f'[portal-nav] 捕获门户链接失败: {_pe}')
        try:
            # ★ 注册 HTTP 响应拦截器，捕获 cpquery 的完整响应链
            _cpq_responses: list = []
            _cpq_out_dir = os.path.join(CNIPA_ROOT, 'output')
            os.makedirs(_cpq_out_dir, exist_ok=True)
            def _on_cpq_response(resp):
                try:
                    ct = resp.headers.get('content-type', '')
                    cl = resp.headers.get('content-length', '?')
                    url_short = resp.url.split('?')[0] if '?' in resp.url else resp.url
                    _cpq_responses.append(f'  {resp.status} {resp.url} (type={ct[:40]}, len={cl})')
                    _log(f'[cpq-http] {resp.status} {resp.url} (type={ct[:40]}, len={cl})')
                    # 写入所有 cpquery 域响应到文件（不依赖日志捕获）
                    if 'cpquery' in resp.url:
                        try:
                            body = resp.text()
                            # 写完整响应到文件（PowerShell tee 可能丢 stderr）
                            _fpath = os.path.join(_cpq_out_dir, f'cpquery_resp_{resp.status}.html')
                            with open(_fpath, 'w', encoding='utf-8') as _f:
                                _f.write(f'<!-- URL: {resp.url} -->\n')
                                _f.write(f'<!-- Status: {resp.status} -->\n')
                                _f.write(f'<!-- Content-Type: {ct} -->\n')
                                _f.write(f'<!-- Content-Length: {cl} -->\n')
                                _f.write(f'<!-- Body-Length: {len(body)} -->\n')
                                _f.write(f'<!-- Headers: {dict(resp.headers)} -->\n\n')
                                _f.write(body)
                            _log(f'[cpq-http] 响应已写入: {_fpath} (body={len(body)} chars)')
                        except Exception as _we:
                            _log(f'[cpq-http] 写响应文件失败: {_we}')
                    # 如果是 HTML 且很小，也输出到日志
                    if resp.status == 200 and 'html' in ct:
                        try:
                            body = resp.text()
                            if len(body) < 500:
                                _log(f'[cpq-http] body({len(body)}): {body[:300]}')
                        except Exception:
                            pass
                except Exception:
                    pass
            page.on('response', _on_cpq_response)

            # ★ v7: 清除反爬 cookie，让 412 挑战重新运行（不用缓存的失败 cookie）
            _clear_cpquery_antibot_cookies(page)

            # ★ v5: 用 domcontentloaded + 长等，让 412 挑战 JS 完整执行
            page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=60_000)
            page.wait_for_timeout(8000)  # 等 412 挑战 JS 加载+执行+cookie 设置

            # 输出 cpquery HTTP 响应链
            _log(f'[cpq-http] === cpquery 响应链 ({len(_cpq_responses)} 条) ===')
            for entry in _cpq_responses:
                _log(f'[cpq-http]{entry}')
            # 写响应链汇总到文件
            try:
                _chain_path = os.path.join(_cpq_out_dir, 'cpquery_response_chain.txt')
                with open(_chain_path, 'w', encoding='utf-8') as _cf:
                    _cf.write(f'URL: {page.url}\n')
                    _cf.write(f'Responses: {len(_cpq_responses)}\n\n')
                    for entry in _cpq_responses:
                        _cf.write(entry + '\n')
                _log(f'[cpq-http] 响应链已写入: {_chain_path}')
            except Exception:
                pass

            page.remove_listener('response', _on_cpq_response)
            _raise_if_unsafe(page)

            # 白屏诊断
            if 'cpquery.cponline.cnipa.gov.cn' in (page.url or '').lower():
                body_text = ''
                try:
                    body_text = (page.inner_text('body') or '').strip()
                except Exception:
                    pass
                if not body_text:
                    _log('[login] cpquery 白屏！截图诊断 + 收集 JS 错误')
                    try:
                        import os as _os
                        _out = _os.path.join(CNIPA_ROOT, 'output')
                        _os.makedirs(_out, exist_ok=True)
                        page.screenshot(path=_os.path.join(_out, 'cpquery_blank.png'), full_page=True)
                        _log(f'[login] 白屏截图：{_os.path.join(_out, "cpquery_blank.png")}')
                    except Exception:
                        pass
                    try:
                        js_errors = page.evaluate(
                            """() => {
                                const errors = [];
                                if (window.__cnipa_js_errors) {
                                    return window.__cnipa_js_errors.slice(-20);
                                }
                                return errors;
                            }"""
                        )
                        if js_errors:
                            _log(f'[login] JS 错误：{js_errors[:5]}')
                    except Exception:
                        pass

            if _wait_cpquery_valid(page, rounds=12):
                _log(f'[login] 直接 goto cpquery 成功：{page.url}')
                _log_diagnostic(_diagnose_page_state(page, tag='07a_goto_success'))
                return page
            _log(f'[login] 直接 goto cpquery 后未通过验证，URL={page.url}')
            _log_diagnostic(_diagnose_page_state(page, tag='07a_goto_failed'))
        except Exception as exc:
            _log(f'[login] 直接 goto cpquery 异常：{exc}')

    # 3) 已在 cpquery 域但验证未通过 → reload 一次
    if 'cpquery.cponline.cnipa.gov.cn' in (page.url or '').lower():
        _log('[login] 已在 cpquery 域但验证未通过，reload + 长等')
        try:
            page.reload(wait_until='networkidle', timeout=90_000)
        except Exception:
            try:
                page.reload(wait_until='domcontentloaded', timeout=60_000)
            except Exception as exc:
                _log(f'[login] reload 失败：{exc}')
        page.wait_for_timeout(3000)
        if _wait_cpquery_valid(page, rounds=30):
            _log_diagnostic(_diagnose_page_state(page, tag='08_reload_success'))
            return page
        _log_diagnostic(_diagnose_page_state(page, tag='08_reload_failed'))

    # 4) 全失败 → 报错 + 附带 cookie 快照诊断
    if not _is_cpquery_session_valid(page):
        # 最终诊断：cookie 快照 + 页面状态
        _log_cookie_snapshot(_snapshot_cookies_and_redirects(page, tag='09_final_fail'))
        _log_diagnostic(_diagnose_page_state(page, tag='09_final_fail'))
        try:
            out = os.path.join(CNIPA_ROOT, 'output', 'login_blank.png')
            page.screenshot(path=out, full_page=True)
            _log(f'[login] 疑似白屏，已截图：{out}')
        except Exception:
            pass
        try:
            body = (page.inner_text('body') or '')[:200]
        except Exception:
            body = ''
        raise RuntimeError(
            f'未能进入 cpquery 有效页（应见欢迎您/使用说明）。'
            f' URL={page.url}; body[:200]={body!r}'
            f' 诊断：查看 output/ 下 diagnostic_09_final_fail.json + cookie_snap 日志'
        )
    return page


def cnipa_sso_login(page: Any, creds: Dict[str, str], *, max_slider_attempts: int = 6) -> None:
    """自然人 SSO 登录，最终必须落到 cpquery（截图3），不能停在 tysfjk 门户（截图1）。

    - 业务目标：https://cpquery.cponline.cnipa.gov.cn/chinesepatent/index
    - 裸开 tysf 登录且无回调时，成功后会落到 tysfjk.../portal/#/home/welcome（错误站）
    """
    from utils.core import handle_cnipa_verify_captcha, human_delay  # type: ignore

    # 0) 隐藏自动化特征（绕过反自动化检测）
    _apply_hide_automation(page)

    # ★ 注册初始 goto 的 HTTP 响应拦截器（捕获 cpquery 原始响应）
    _init_responses: list = []
    _init_out_dir = os.path.join(CNIPA_ROOT, 'output')
    os.makedirs(_init_out_dir, exist_ok=True)

    def _on_init_response(resp):
        try:
            url = resp.url
            status = resp.status
            ct = resp.headers.get('content-type', '')
            cl = resp.headers.get('content-length', '?')
            entry = f'  {status} {url} (type={ct[:40]}, len={cl})'
            _init_responses.append(entry)
            _log(f'[init-http] {entry}')
            # ★ 捕获所有 cpquery 域响应的原始 body
            if 'cpquery' in url:
                try:
                    body = resp.text()
                    _fpath = os.path.join(_init_out_dir, f'cpquery_init_resp_{status}.html')
                    with open(_fpath, 'w', encoding='utf-8') as _f:
                        _f.write(f'<!-- URL: {url} -->\n')
                        _f.write(f'<!-- Status: {status} -->\n')
                        _f.write(f'<!-- Content-Type: {ct} -->\n')
                        _f.write(f'<!-- Content-Length: {cl} -->\n')
                        _f.write(f'<!-- Body-Length: {len(body)} -->\n')
                        _f.write(f'<!-- Headers: {dict(resp.headers)} -->\n\n')
                        _f.write(body)
                    _log(f'[init-http] ★ cpquery 原始响应已写入: {_fpath} (body={len(body)} chars)')
                    # 如果 body 很小，直接输出到日志
                    if len(body) < 1000:
                        _log(f'[init-http] ★ body({len(body)}): {body!r}')
                except Exception as _we:
                    _log(f'[init-http] 写响应文件失败: {_we}')
            # 捕获所有重定向（3xx）
            if 300 <= status < 400:
                loc = resp.headers.get('location', '')
                _log(f'[init-http] ★ 重定向 → {loc}')
        except Exception:
            pass

    page.on('response', _on_init_response)

    # 1) 先试业务站；白屏则等自然跳 SSO，再主动 goto SSO
    #    ★ v5: 先导航到空白页并模拟行为事件，建立事件历史
    #    然后 goto cpquery，让反爬 JS 在有事件历史的环境中运行
    try:
        page.goto('about:blank', wait_until='load', timeout=10_000)
        page.wait_for_timeout(500)
        _simulate_behavioral_events(page)
        page.wait_for_timeout(300)
        _log('[login] 已在空白页模拟行为事件，建立事件历史')
    except Exception as _ev_exc:
        _log(f'[login] 空白页事件模拟失败（非致命）: {_ev_exc}')

    # ★ v7: goto cpquery 前清除反爬 cookie，确保获得全新的 412 挑战
    _clear_cpquery_antibot_cookies(page)

    page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=120_000)

    # ★ v5: 等更长时间让 412 挑战 JS 完整执行
    #    响应链：412(挑战) → 200(304KB JS) → 400(失败) 或页面正常加载
    #    需要等外部 JS 加载 + 字节码 VM 执行 + cookie 设置
    # ★ v8: 挑战评估窗口内注入 CDP 级真实输入（isTrusted=true）
    #    分批移动而非一次性等待，让指纹 JS 在采集期间能读到真实事件流
    try:
        import random as _rnd
        for _ in range(4):
            page.mouse.move(
                _rnd.randint(300, 1100),
                _rnd.randint(200, 600),
                steps=_rnd.randint(3, 8),
            )
            page.wait_for_timeout(1500)
    except Exception as _mv_exc:
        _log(f'[login] 挑战窗口鼠标轨迹注入失败（非致命）: {_mv_exc}')
    page.wait_for_timeout(2000)
    _raise_if_unsafe(page)

    # 写初始响应链到文件
    try:
        _chain_path = os.path.join(_init_out_dir, 'cpquery_init_response_chain.txt')
        with open(_chain_path, 'w', encoding='utf-8') as _cf:
            _cf.write(f'URL: {page.url}\n')
            _cf.write(f'Responses: {len(_init_responses)}\n\n')
            for entry in _init_responses:
                _cf.write(entry + '\n')
        _log(f'[init-http] 初始响应链已写入: {_chain_path}')
    except Exception:
        pass

    page.remove_listener('response', _on_init_response)

    # ★ v5: 检查 412 挑战是否已通过（cookie 数变化 + 页面状态）
    _post_challenge_cookies = []
    try:
        _cq_cookies = page.context.cookies('https://cpquery.cponline.cnipa.gov.cn')
        _post_challenge_cookies = [c['name'] for c in _cq_cookies]
        _log(f'[init-http] 挑战后 cpquery cookie ({len(_cq_cookies)} 个): {_post_challenge_cookies}')
    except Exception:
        pass

    # 诊断：初始 goto cpquery 后状态（反检测是否生效、页面加载情况）
    _log_diagnostic(_diagnose_page_state(page, tag='01_initial_goto'))

    # ★ v5: 如果挑战后页面仍为空白但 cookie 已设置，尝试 reload
    if _cpquery_body_empty(page) and 'cpquery.cponline.cnipa.gov.cn' in (page.url or '').lower():
        if len(_post_challenge_cookies) > 2:
            _log('[login] 挑战后 cookie 增多，尝试 reload 看是否通过')
            try:
                page.reload(wait_until='domcontentloaded', timeout=60_000)
                page.wait_for_timeout(5000)
            except Exception:
                pass

    if _is_cpquery_session_valid(page):
        dismiss_cpquery_usage_dialog(page)
        select_review_invalid_menu(page)
        _log('[login] 已在 cpquery 登录态，已关使用说明并进入复审无效查询')
        return

    # 2) cpquery 未出登录表单 → 主动 goto SSO service URL
    if not _login_form_visible(page):
        if _cpquery_body_empty(page) or 'cpquery.cponline.cnipa.gov.cn' in (page.url or '').lower():
            sso_url = _sso_login_url_for_cpquery()
            _log(f'[login] cpquery 未出登录表单，主动 goto SSO: {sso_url}')
            page.goto(sso_url, wait_until='domcontentloaded', timeout=120_000)
            page.wait_for_timeout(2000)

            # SSO 可能直接回跳 cpquery（已登录态），或落到登录页
            if _is_cpquery_session_valid(page):
                dismiss_cpquery_usage_dialog(page)
                select_review_invalid_menu(page)
                _log('[login] SSO 入口已自然进入 cpquery')
                return

            if not _login_form_visible(page):
                # SSO 仍无表单 → 报错，禁止回退 tysf（会进门户）
                raise RuntimeError(
                    f'SSO 未出登录表单，URL={page.url}。'
                    '禁止回退 tysf（会进门户）。建议：设 CNIPA_CDP_URL 接管已手动登录的 Chrome。'
                )

    page.wait_for_selector('input[placeholder*="手机"]', timeout=30_000)

    _click_sso_login_tab(page, creds.get('login_type', 'natural'))
    human_delay(0.3, 0.6)

    user_input = page.locator('input[placeholder*="手机"]').first
    pass_input = page.locator('input[type="password"]').first
    user_input.click()
    user_input.fill(creds['username'])
    pass_input.click()
    pass_input.fill(creds['password'])
    human_delay(0.3, 0.6)

    _persist_cpquery_redirect(page)

    # ★ 检测是否为 cpquery 自有 CAS 表单（form action = /chinesepatent/index）
    _is_cpquery_cas = _is_cpquery_own_cas_form(page)

    # ★ 注册重定向拦截器（捕获表单提交后的完整 HTTP 响应链）
    _redirect_log: list = []
    _out_dir = os.path.join(CNIPA_ROOT, 'output')
    os.makedirs(_out_dir, exist_ok=True)
    def _on_response(resp):
        try:
            status = resp.status
            url = resp.url
            # 记录所有 3xx 重定向和关键页面加载
            if 300 <= status < 400:
                location = resp.headers.get('location', '')
                _redirect_log.append(f'  {status} {url} → {location}')
                _log(f'[sso-redirect] {status} {url} → {location}')
            elif status == 200 and ('cponline' in url or 'cnipa' in url):
                _redirect_log.append(f'  {status} {url}')
                _log(f'[sso-redirect] {status} {url}')
            # ★ 捕获 cpquery 表单提交后的实际响应（写入文件）
            if status in (200, 400) and 'cpquery' in url and 'html' in resp.headers.get('content-type', ''):
                try:
                    body = resp.text()
                    _fpath = os.path.join(_out_dir, f'cpquery_form_resp_{status}.html')
                    with open(_fpath, 'w', encoding='utf-8') as _f:
                        _f.write(f'<!-- URL: {url} -->\n')
                        _f.write(f'<!-- Status: {status} -->\n')
                        _f.write(f'<!-- Headers: {dict(resp.headers)} -->\n\n')
                        _f.write(body[:5000])
                    _log(f'[sso-redirect] cpquery 表单响应已写入: {_fpath} (body={len(body)} chars)')
                except Exception as _we:
                    _log(f'[sso-redirect] 写 cpquery 响应文件失败: {_we}')
            # ★ 捕获门户 isAuth/getamurl 响应体
            if status == 200 and '/portal/sso/' in url:
                try:
                    body = resp.text()
                    if 'isAuth' in url:
                        _fpath = os.path.join(_out_dir, 'portal_isAuth.json')
                    elif 'getamurl' in url:
                        _fpath = os.path.join(_out_dir, 'portal_getamurl.json')
                    else:
                        _fpath = os.path.join(_out_dir, 'portal_sso_unknown.json')
                    with open(_fpath, 'w', encoding='utf-8') as _f:
                        _f.write(f'<!-- URL: {url} -->\n')
                        _f.write(f'<!-- Status: {status} -->\n')
                        _f.write(f'<!-- Headers: {dict(resp.headers)} -->\n\n')
                        _f.write(body)
                    _log(f'[sso-redirect] 门户响应已写入: {_fpath} (body={len(body)} chars)')
                except Exception as _we:
                    _log(f'[sso-redirect] 写门户响应文件失败: {_we}')
        except Exception:
            pass
    page.on('response', _on_response)

    if _is_cpquery_cas:
        # ★ cpquery 自有 CAS 表单：直接 form.submit() 绕过 JS 点击拦截
        _log('[login] cpquery 自有 CAS 表单 → 使用 form.submit() 直接提交')
        _submit_cpquery_cas_form(page)
    else:
        # 门户 SSO 登录页：使用按钮点击
        _log('[login] 门户 SSO 登录页 → 使用按钮点击提交')
        _click_sso_submit_button(page)

    if not handle_cnipa_verify_captcha(page, max_attempts=max_slider_attempts):
        page.remove_listener('response', _on_response)
        raise RuntimeError('滑块验证失败')

    # 等几秒让重定向链完成
    page.wait_for_timeout(3000)

    # 输出完整重定向链
    _log(f'[sso-redirect] === 滑块通过后完整响应链 ({len(_redirect_log)} 条) ===')
    for entry in _redirect_log:
        _log(f'[sso-redirect]{entry}')
    _log(f'[sso-redirect] === 最终 URL: {page.url} ===')

    # 移除拦截器
    page.remove_listener('response', _on_response)

    # 诊断：滑块通过后立即抓取三域 cookie + 跳转链
    _log_cookie_snapshot(_snapshot_cookies_and_redirects(page, tag='06_slider_passed'))
    _log_diagnostic(_diagnose_page_state(page, tag='06_slider_passed'))

    # ★ 如果是 cpquery CAS 表单提交后直接落在 cpquery 且会话有效 → 直接进入
    if _is_cpquery_cas and _is_cpquery_session_valid(page):
        _log('[login] cpquery CAS 表单直接登录成功！')
        dismiss_cpquery_usage_dialog(page)
        page.wait_for_timeout(800)
        select_review_invalid_menu(page)
        page.wait_for_timeout(800)
        _log('[login] cpquery 登录成功（已关使用说明并进入复审无效查询）')
        return

    page = _enter_cpquery_after_sso(page)

    # 关「使用说明」→ 左侧「复审无效审查决定查询」
    dismiss_cpquery_usage_dialog(page)
    page.wait_for_timeout(800)
    select_review_invalid_menu(page)
    page.wait_for_timeout(800)
    _log('[login] cpquery 登录成功（已关使用说明并进入复审无效查询）')


def ensure_logged_in(
    page: Any,
    creds: Dict[str, str],
    session_file: str,
    *,
    force_login: bool = False,
    session_dir: Optional[str] = None,
    allow_sso_login: bool = False,
    browser_mode: str = 'ephemeral',
) -> bool:
    session_dir = session_dir or os.path.dirname(session_file)

    if browser_mode == 'cdp':
        try:
            if _is_cpquery_session_valid(page):
                return True
            _clear_cpquery_antibot_cookies(page)
            page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=120_000)
            page.wait_for_timeout(8000)  # ★ v5: 等 412 挑战 JS
            if _is_cpquery_session_valid(page):
                return True
        except Exception as exc:
            _log(f'[warn] CDP 会话检查失败: {exc}')
        raise RuntimeError(
            '已连接 Chrome，但当前未处于 cpquery 已登录状态。'
            '请在你接管的浏览器窗口中打开 cpquery 并完成登录，勿再调用 cnipa_login。'
        )

    if os.path.isfile(session_file) or browser_mode == 'persistent':
        try:
            _clear_cpquery_antibot_cookies(page)
            page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=120_000)
            page.wait_for_timeout(8000)  # ★ v5: 等 412 挑战 JS
            if _is_cpquery_session_valid(page):
                return True
        except Exception as exc:
            _log(f'[warn] 复用 session 失败: {exc}')

    if not allow_sso_login and not force_login:
        raise RuntimeError(
            'cpquery session 已失效。请先调用 cnipa_login 重新登录；'
            '勿频繁重试，避免触发国知局安全拦截 (/unsafe)。'
        )

    ok, remaining = _can_attempt_sso_login(session_dir)
    if not ok:
        raise RuntimeError(
            f'距上次 SSO 登录不足 {SSO_LOGIN_COOLDOWN_SEC} 秒（还需等待约 {remaining} 秒）。'
            '请稍后重试，或手动登录后复用 session。'
        )

    _mark_sso_attempt(session_dir)
    cnipa_sso_login(page, creds)
    page.context.storage_state(path=session_file)
    return True


def goto_cpquery_search_page(page: Any) -> None:
    # ★ 联调优化：登录流程通常已进入复审无效检索页，直接复用（少打一次服务器）
    if _search_form_present(page):
        _log('[goto] 已在复审无效检索页，跳过重复导航')
        dismiss_cpquery_usage_dialog(page)
        d = get_step_dumper()
        if d:
            d.dump(page, 'skip_nav_already_on_search_page')
        return
    _clear_cpquery_antibot_cookies(page)
    page.goto(CPQUERY_INDEX, wait_until='domcontentloaded', timeout=120_000)
    # ★ v5: 等更长时间让 412 挑战 JS 完整执行
    page.wait_for_timeout(8000)
    _raise_if_unsafe(page)
    if not _is_cpquery_session_valid(page):
        if _is_cponline_public_home(page):
            _click_cponline_cpquery_nav(page)
        elif _is_typortal_page(page):
            # SSO service 参数不生效，落到门户 → 「返回系统」→ cponline 导航
            _log(f'[goto] 落在门户（{page.url}），尝试「返回系统」→ cponline 导航')
            try:
                for sel in ('a:has-text("返回系统")', 'text=返回系统'):
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=2000):
                        el.click(timeout=5000)
                        page.wait_for_timeout(3000)
                        break
            except Exception:
                pass
            if _is_cponline_public_home(page):
                _click_cponline_cpquery_nav(page)
        else:
            sso_url = _sso_login_url_for_cpquery()
            try:
                page.goto(sso_url, wait_until='domcontentloaded', timeout=20_000)
                page.wait_for_timeout(2000)
            except Exception as exc:
                _log(f'[warn] SSO 入口异常: {exc}')
        for _ in range(20):
            if _is_cpquery_session_valid(page):
                break
            page.wait_for_timeout(500)
        # 已在 cpquery 域但验证仍未通过 → reload 长等（SPA 加载异常兜底）
        if not _is_cpquery_session_valid(page) and 'cpquery.cponline.cnipa.gov.cn' in (page.url or '').lower():
            _log('[goto] cpquery 域但验证未通过，reload + 长等')
            try:
                page.reload(wait_until='networkidle', timeout=90_000)
            except Exception:
                try:
                    page.reload(wait_until='domcontentloaded', timeout=60_000)
                except Exception as exc:
                    _log(f'[goto] reload 失败: {exc}')
            page.wait_for_timeout(3000)
            for _ in range(20):
                if _is_cpquery_session_valid(page):
                    break
                page.wait_for_timeout(500)
            # 诊断：reload 后状态
            _log_diagnostic(_diagnose_page_state(page, tag='goto_reload_done'))
    dismiss_cpquery_usage_dialog(page)
    page.wait_for_timeout(500)
    select_review_invalid_menu(page)
    page.wait_for_timeout(500)
    d = get_step_dumper()
    if d:
        d.dump(page, 'goto_search_ready')


def handle_pss_disclaimer(page: Any) -> None:
    """pss 免责声明：**先勾「记住我的选择」再点「同意**」（REQUIREMENTS §3.3）。

    顺序颠倒会导致每次进 pss 都重弹声明；勾选失败也不阻断（下次重弹而已）。
    """
    # 1) 勾「记住我的选择」（按语义定位，避免误勾其它 checkbox）
    for selector in (
        'label:has-text("记住我的选择")',
        '.el-checkbox:has-text("记住")',
        'label:has-text("记住")',
        '[class*="disclaimer"] input[type="checkbox"]',
    ):
        try:
            box = page.locator(selector).first
            if box.count() > 0 and box.is_visible(timeout=800):
                try:
                    if not box.is_checked():
                        box.check()
                except Exception:
                    box.click()
                page.wait_for_timeout(300)
                _log('[pss] 已尝试勾选「记住我的选择」')
                break
        except Exception:
            continue
    # 2) 点「同意」
    if not _click_by_text(page, ['同意', '接 受', '接受', '确定'], exact=False):
        for sel in (
            'button:has-text("同意")',
            '[class*="disclaimer"] button',
            '[class*="dialog"] button:has-text("同")',
            '[class*="modal"] button:has-text("同")',
        ):
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=800):
                    el.click(timeout=3000)
                    break
            except Exception:
                continue
    time.sleep(0.8)
    d = get_step_dumper()
    if d:
        d.dump(page, 'pss_after_disclaimer')
