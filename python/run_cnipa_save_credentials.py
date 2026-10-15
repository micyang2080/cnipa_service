#!/usr/bin/env python3
"""保存国知局账号到 credentials 文件。"""

from __future__ import annotations

import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cnipa_common import (  # noqa: E402
    _ensure_stdio_line_buffered,
    _log,
    _parse_json_input,
    emit_result,
    save_credentials,
)


def main() -> None:
    _ensure_stdio_line_buffered()
    args = _parse_json_input()
    try:
        username = str(args.get('username') or '').strip()
        password = str(args.get('password') or '').strip()
        login_type = str(args.get('login_type') or 'natural').strip().lower()
        cred_path = args.get('credentials_path')
        path = save_credentials(
            username=username,
            password=password,
            login_type=login_type,
            credentials_path=str(cred_path).strip() if cred_path else None,
        )
        emit_result(
            {
                'success': True,
                'summary': f'国知局凭证已保存',
                'credentials_path': path,
                'login_type': login_type,
                'username': username,
            }
        )
    except Exception as exc:
        _log(f'[cnipa_save_credentials] error: {exc}')
        emit_result({'success': False, 'error': str(exc)})


if __name__ == '__main__':
    main()
