# -*- coding: utf-8 -*-
"""国知局凭证管理（account.ini）。

流程（见 docs/接口契约.md §登录）：
1. 登录 runner / MCP 先 load_creds()；没有凭证 → 契约层报 NEED_CREDENTIALS，
   由 AI agent 在会话里向用户索要账号密码。
2. 用户在会话中给出账号密码 → 调 cnipa_login 工具 → save_creds() 落盘
   account.ini → 之后的调用全自动读文件登录。

安全约束：
- 密码只允许出现在 account.ini 与内存中；绝不写 payload.json / 契约 JSON /
  stdout 日志（selftest 里有回归断言）。
- account.ini 已加入 .gitignore。

文件格式（UTF-8 无 BOM，INI）：
    [cnipa]
    username = 13800000000
    password = ****
    login_type = natural

自测（离线）：
    python cnipa_creds.py --selftest
"""

import configparser
import os
import sys
import tempfile

SECTION = 'cnipa'
KEY_USERNAME = 'username'
KEY_PASSWORD = 'password'
KEY_LOGIN_TYPE = 'login_type'

# 登录身份 Tab：natural=自然人（默认）、legal=法人
VALID_LOGIN_TYPES = ('natural', 'legal')

# 登录类型 → 登录页 Tab 文案（瑞数 SSO 页）
LOGIN_TYPE_LABELS = {
    'natural': '自然人登录',
    'legal': '法人登录',
}


def creds_path(explicit=None):
    """account.ini 位置：CNIPA_ACCOUNT_INI > 显式传参 > python 根目录。"""
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get('CNIPA_ACCOUNT_INI')
    if env:
        return os.path.abspath(env)
    root = os.environ.get('CNIPA_CONNECTOR_ROOT')
    if not root:
        root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, 'account.ini')


def load_creds(explicit_path=None):
    """读凭证；不存在或字段残缺返回 None。返回 dict 不含明文密码以外的冗余。"""
    path = creds_path(explicit_path)
    if not os.path.exists(path):
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding='utf-8-sig')
    except (configparser.Error, UnicodeDecodeError):
        return None
    if not cp.has_section(SECTION):
        return None
    username = (cp.get(SECTION, KEY_USERNAME, fallback='') or '').strip()
    password = cp.get(SECTION, KEY_PASSWORD, fallback='') or ''
    if not username or not password:
        return None
    login_type = (cp.get(SECTION, KEY_LOGIN_TYPE, fallback='natural') or 'natural').strip()
    if login_type not in VALID_LOGIN_TYPES:
        login_type = 'natural'
    return {
        'username': username,
        'password': password,
        'login_type': login_type,
        'path': path,
    }


def has_creds(explicit_path=None):
    return load_creds(explicit_path) is not None


def save_creds(username, password, login_type='natural', explicit_path=None):
    """保存凭证到 account.ini。返回写入路径。"""
    username = (username or '').strip()
    password = password or ''
    login_type = login_type if login_type in VALID_LOGIN_TYPES else 'natural'
    if not username or not password:
        raise ValueError('username/password 不能为空')

    path = creds_path(explicit_path)
    cp = configparser.ConfigParser()
    if os.path.exists(path):
        try:
            cp.read(path, encoding='utf-8-sig')
        except configparser.Error:
            cp = configparser.ConfigParser()
    if not cp.has_section(SECTION):
        cp.add_section(SECTION)
    cp.set(SECTION, KEY_USERNAME, username)
    cp.set(SECTION, KEY_PASSWORD, password)
    cp.set(SECTION, KEY_LOGIN_TYPE, login_type)

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('# CNIPA credentials - DO NOT COMMIT\n')
        cp.write(f)
    try:
        os.chmod(path, 0o600)  # POSIX 生效；Windows 无害
    except OSError:
        pass
    return path


def mask_username(username):
    """日志安全展示：手机号保留前 3 后 2，其它保留首尾各 1 字符。"""
    u = (username or '').strip()
    if not u:
        return ''
    if u.isdigit() and len(u) >= 7:
        return u[:3] + '****' + u[-2:]
    if len(u) <= 2:
        return u[0] + '*'
    return u[0] + '*' * (len(u) - 2) + u[-1]


def redact(text):
    """兜底：把已知密码从任意文本里替换为 ***（防日志泄漏）。"""
    creds = load_creds()
    if creds and creds['password'] and creds['password'] in (text or ''):
        text = text.replace(creds['password'], '***')
    return text


# ---------------------------------------------------------------- 自测

def _selftest():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        ini = os.path.join(td, 'account.ini')

        # 1) 未保存前 load → None
        check(load_creds(ini) is None, '空文件应返回 None')

        # 2) save → load 往返
        p = save_creds('13800001234', 'p@ss魔法', 'natural', ini)
        check(p == ini, 'save 应写入指定路径')
        c = load_creds(ini)
        check(c is not None and c['username'] == '13800001234'
              and c['password'] == 'p@ss魔法' and c['login_type'] == 'natural',
              'load 往返不符: %r' % (c,))

        # 3) 非法 login_type 归一为 natural
        save_creds('13800001234', 'x', 'hacker', ini)
        check(load_creds(ini)['login_type'] == 'natural', '非法 login_type 未归一')

        # 4) 残缺文件（只有用户名）→ None
        with open(ini, 'w', encoding='utf-8') as f:
            f.write('[cnipa]\nusername = abc\n')
        check(load_creds(ini) is None, '残缺凭证应返回 None')

        # 5) 覆盖保存后新值生效（模拟用户改密码）
        save_creds('13800001234', 'newpass', 'natural', ini)
        check(load_creds(ini)['password'] == 'newpass', '覆盖保存未生效')

        # 6) mask_username
        check(mask_username('13800001234') == '138****34', '手机号脱敏不符')
        check(mask_username('ab') == 'a*', '短用户名脱敏不符')
        check(mask_username('') == '', '空用户名脱敏不符')

        # 7) redact 兜底（借助环境变量指向临时文件）
        os.environ['CNIPA_ACCOUNT_INI'] = ini
        save_creds('13800001234', 'S3cret!', 'natural', ini)
        check(redact('password=S3cret! in log') == 'password=*** in log', 'redact 未替换密码')
        del os.environ['CNIPA_ACCOUNT_INI']

        # 8) 空用户名/密码保存应抛错
        try:
            save_creds('', 'x', 'natural', ini)
            failures.append('空 username 未抛错')
        except ValueError:
            pass

    if failures:
        print('SELFTEST FAIL (%d)' % len(failures))
        for x in failures:
            print(' -', x)
        return 1
    print('SELFTEST OK')
    return 0


if __name__ == '__main__':
    sys.exit(_selftest())
